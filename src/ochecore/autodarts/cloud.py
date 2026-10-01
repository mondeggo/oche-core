import asyncio
import json
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from ochecore.autodarts.auth import DeviceAuth, safe_error
from ochecore.autodarts.errors import ConnectionProblem, LoginRequired
from ochecore.events import EventBus, event_name, parse_cloud_message, redact


@dataclass
class MatchState:
    """Track the selected match and its latest raw state without scoring darts."""

    match_id: str | None = None
    latest: dict[str, Any] | None = None

    def select(self, match_id: str | None) -> None:
        self.match_id = match_id
        self.latest = None

    def update(self, data: dict[str, Any]) -> None:
        if self.match_id and data.get("id", self.match_id) == self.match_id:
            self.latest = redact(data)


class CloudConnection:
    def __init__(
        self,
        http: httpx.AsyncClient,
        auth: DeviceAuth,
        board_id: str,
        bus: EventBus,
        reconcile_interval: float = 30,
    ):
        self.http = http
        self.auth = auth
        self.board_id = board_id
        self.bus = bus
        self.reconcile_interval = reconcile_interval
        self.ticket_url = "https://play.ws.autodarts.com/ms/v0/tickets"
        self.ws_url = "wss://play.ws.autodarts.com/ms/v0/subscribe"
        self.state = "unconfigured" if not board_id else "waiting_for_login"
        self.error: str | None = None
        self.match = MatchState()
        self.board: dict | None = None
        self.last_message_at: str | None = None
        self.reconnects = 0
        self.invalid_messages = 0

    @property
    def match_id(self) -> str | None:
        return self.match.match_id

    def status(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "board_id": self.board_id or None,
            "board_online": self.board.get("connected") if self.board else None,
            "match_id": self.match_id,
            "last_message_at": self.last_message_at,
            "reconnects": self.reconnects,
            "invalid_messages": self.invalid_messages,
        }

    async def get(self, path: str, missing_ok: bool = False) -> dict | None:
        result = await self._request("GET", f"{self.auth.base_url}{path}", missing_ok)
        if not isinstance(result, dict) and not (result is None and missing_ok):
            raise ConnectionProblem("Unexpected AutoDarts response format.")
        return result

    async def list_boards(self) -> list[dict]:
        """Discover account boards without selecting one or exposing upstream metadata."""
        result = await self._request("GET", f"{self.auth.base_url}/bs/v0/boards")
        if not isinstance(result, list):
            raise ConnectionProblem("AutoDarts returned an invalid board list.")
        boards = []
        for item in result:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ConnectionProblem("AutoDarts returned an invalid board list.")
            try:
                board_id = str(UUID(item["id"]))
            except ValueError as exc:
                raise ConnectionProblem("AutoDarts returned an invalid board ID.") from exc
            name = item.get("name")
            online = item.get("connected", item.get("online"))
            if not isinstance(online, bool):
                state = item.get("state")
                online = state.get("connected") if isinstance(state, dict) else None
            boards.append(
                {
                    "id": board_id,
                    "name": name.strip() if isinstance(name, str) and name.strip() else board_id,
                    "online": online if isinstance(online, bool) else None,
                }
            )
        return boards

    async def request_ticket(self) -> str:
        """Exchange the bearer token for a fresh WebSocket connection ticket."""
        payload = await self._request("POST", self.ticket_url)
        code = payload.get("code") if isinstance(payload, dict) else None
        if not isinstance(code, str) or not code:
            raise ConnectionProblem("AutoDarts returned an invalid WebSocket ticket.")
        return code

    async def _request(self, method: str, url: str, missing_ok: bool = False) -> Any:
        token = await self.auth.access_token()
        for attempt in range(2):
            response = await self.http.request(
                method, url, headers={"Authorization": f"Bearer {token}"}
            )
            if response.status_code == 401 and attempt == 0:
                token = await self.auth.access_token(rejected=token)
                continue
            if missing_ok and response.status_code == 404:
                return None
            if not response.is_success:
                raise ConnectionProblem(
                    f"AutoDarts API: HTTP {response.status_code}. Check the board and permissions."
                )
            try:
                result = response.json()
            except ValueError as exc:
                raise ConnectionProblem("Invalid JSON response from AutoDarts.") from exc
            return result
        raise ConnectionProblem("API authentication was rejected.")

    async def subscription(self, ws, channel: str, topic: str, action: str = "subscribe") -> None:
        await ws.send(json.dumps({"type": action, "channel": channel, "topic": topic}))

    async def set_match(self, ws, match_id: str | None) -> None:
        if match_id == self.match_id:
            return
        if self.match_id:
            await self.subscription(
                ws, "autodarts.matches", f"{self.match_id}.state", "unsubscribe"
            )
        self.match.select(match_id)
        if match_id:
            await self.subscription(ws, "autodarts.matches", f"{match_id}.state")
            state = await self.get(
                f"/gs/v0/matches/{quote(match_id, safe='')}/state", missing_ok=True
            )
            if state is not None:
                self.match.update(state)
                self.bus.publish(
                    "cloud",
                    "match.state",
                    state,
                    channel="autodarts.matches",
                    topic=f"{match_id}.state",
                    snapshot=True,
                )

    async def reconcile(self, ws) -> None:
        self.board = await self.get(f"/bs/v0/boards/{quote(self.board_id, safe='')}")
        assert self.board is not None
        match_id = self.board.get("matchId")
        if match_id is not None and (not isinstance(match_id, str) or not match_id):
            raise ConnectionProblem("Invalid AutoDarts match ID.")
        await self.set_match(ws, match_id)

    async def on_message(self, ws, raw: str | bytes) -> None:
        message = parse_cloud_message(raw)
        if message is None:
            self.invalid_messages += 1
            return
        self.last_message_at = datetime.now(UTC).isoformat()
        event = event_name(message, self.board_id, self.match_id)
        if event is None:
            return
        data = message.data
        if event == "match.state":
            self.match.update(data)
        self.bus.publish("cloud", event, data, channel=message.channel, topic=message.topic)
        if event == "board.matches":
            match_id = data.get("id")
            if data.get("event") == "start" and isinstance(match_id, str) and match_id:
                await self.set_match(ws, match_id)
            elif data.get("event") in {"finish", "delete", "end"} and match_id == self.match_id:
                await self.set_match(ws, None)

    async def run(self) -> None:
        if not self.board_id:
            return
        failures = 0
        while True:
            token = None
            opened_at = None
            try:
                token = await self.auth.access_token()
                self.state = "connecting"
                user = await self.get("/auth/v1/userinfo")
                if not user or not isinstance(user.get("sub"), str):
                    raise ConnectionProblem("The AutoDarts profile does not contain a user ID.")
                ticket = await self.request_ticket()
                token = await self.auth.access_token()
                async with connect(
                    f"{self.ws_url}?{urlencode({'code': ticket})}",
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=3,
                    max_size=2**20,
                    proxy=None,
                ) as ws:
                    opened_at = time.monotonic()
                    self.match.select(None)
                    await self.subscription(ws, "autodarts.boards", f"{self.board_id}.matches")
                    await self.subscription(ws, "autodarts.boards", f"{self.board_id}.events")
                    await self.subscription(ws, "autodarts.users", f"{user['sub']}.events")
                    await self.reconcile(ws)
                    self.state, self.error = "connected", None
                    next_reconcile = time.monotonic() + self.reconcile_interval
                    while True:
                        # This also runs when the socket is continuously busy.
                        if await self.auth.access_token() != token:
                            break  # request a new ticket and restore subscriptions
                        if time.monotonic() >= next_reconcile:
                            await self.reconcile(ws)
                            next_reconcile = time.monotonic() + self.reconcile_interval
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except TimeoutError:
                            continue
                        await self.on_message(ws, raw)
                    failures = 0
                    self.reconnects += 1
                    continue
            except LoginRequired:
                self.state, self.error = "waiting_for_login", self.auth.error
                await asyncio.sleep(1)
                continue
            except asyncio.CancelledError:
                self.state = "stopped"
                raise
            except Exception as exc:
                if isinstance(exc, InvalidStatus) and exc.response.status_code == 401 and token:
                    try:
                        await self.auth.access_token(rejected=token)
                    except (ConnectionProblem, httpx.HTTPError, OSError):
                        pass
                self.state, self.error = "reconnecting", safe_error(exc)
                self.reconnects += 1
            if opened_at and time.monotonic() - opened_at > 30:
                failures = 0
            delay = min(30, 2 ** min(failures, 5)) + random.uniform(0, 0.5)
            failures += 1
            await asyncio.sleep(delay)
