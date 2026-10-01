import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError

from ochecore.autodarts.errors import ConnectionProblem, LoginRequired, OAuthProblem
from ochecore.storage import remove_private_file, write_private_json


class TokenSet(BaseModel):
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: float
    client_id: str
    api_base_url: str


class DeviceCode(BaseModel):
    device_code: SecretStr
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    expires_in: int = Field(gt=0)
    interval: float = Field(default=5, gt=0)


class DeviceAuth:
    def __init__(self, http: httpx.AsyncClient, base_url: str, client_id: str, path: Path):
        self.http = http
        self.base_url = base_url
        self.client_id = client_id
        self.path = path
        self.tokens: TokenSet | None = None
        self.state = "disconnected" if client_id else "unconfigured"
        self.error: str | None = None
        self.device: DeviceCode | None = None
        self.device_expires_at = 0.0
        self.lock = asyncio.Lock()
        self.poll_task: asyncio.Task | None = None
        if path.exists():
            try:
                candidate = TokenSet.model_validate_json(path.read_text(encoding="utf-8"))
                if candidate.client_id == client_id and candidate.api_base_url == base_url:
                    self.tokens = candidate
                    self.state = "authenticated"
            except (OSError, ValidationError):
                self.error = "Cannot read the saved session. Start the connection again."

    def status(self) -> dict:
        result: dict[str, Any] = {"state": self.state, "error": self.error}
        if self.device:
            result["device"] = {
                "user_code": self.device.user_code,
                "verification_uri": self.device.verification_uri,
                "verification_uri_complete": self.device.verification_uri_complete,
                "expires_at": self.device_expires_at,
            }
        return result

    async def _post(self, path: str, body: dict) -> dict:
        response = await self.http.post(f"{self.base_url}/auth/v1/{path}", json=body)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectionProblem("Invalid OAuth response.") from exc
        if not isinstance(payload, dict):
            raise ConnectionProblem("Invalid OAuth response.")
        if response.is_error:
            if response.status_code < 500 and isinstance(payload.get("error"), str):
                raise OAuthProblem(payload["error"])
            raise ConnectionProblem(f"OAuth service unavailable (HTTP {response.status_code}).")
        return payload

    def _save(self, payload: dict) -> None:
        try:
            lifetime = float(payload["expires_in"])
            if lifetime <= 0 or not payload["access_token"] or not payload["refresh_token"]:
                raise ValueError("Missing tokens")
            tokens = TokenSet(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                expires_at=time.time() + lifetime,
                client_id=self.client_id,
                api_base_url=self.base_url,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConnectionProblem("AutoDarts returned an incomplete session.") from exc
        # Keep the rotated refresh token even if the disk becomes unavailable.
        self.tokens = tokens
        data = tokens.model_dump(mode="json")
        data["access_token"] = tokens.access_token.get_secret_value()
        data["refresh_token"] = tokens.refresh_token.get_secret_value()
        write_private_json(self.path, data)
        self.state = "authenticated"
        self.error = None

    async def start_login(self) -> dict:
        async with self.lock:
            if not self.client_id:
                raise ConnectionProblem("Enter the OAuth client_id assigned to OcheCore.")
            if self.poll_task and not self.poll_task.done():
                return self.status()
            if self.tokens:
                return self.status()
            payload = await self._post("device/code", {"client_id": self.client_id})
            try:
                self.device = DeviceCode.model_validate(payload)
                # Never put an arbitrary upstream URI into a clickable login link.
                for url in (self.device.verification_uri, self.device.verification_uri_complete):
                    if url and httpx.URL(url).scheme != "https":
                        raise ValueError("HTTPS required")
            except (ValueError, httpx.InvalidURL) as exc:
                self.device = None
                raise ConnectionProblem("Invalid AutoDarts login response.") from exc
            self.device_expires_at = time.time() + self.device.expires_in
            self.state = "awaiting_authorization"
            self.error = None
            self.poll_task = asyncio.create_task(self._poll(), name="autodarts-device-login")
            return self.status()

    async def _poll(self) -> None:
        assert self.device is not None
        device = self.device
        interval = device.interval
        deadline = time.monotonic() + device.expires_in
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(min(interval, max(0, deadline - time.monotonic())))
                if time.monotonic() >= deadline:
                    break
                try:
                    payload = await self._post(
                        "device/token",
                        {
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                            "device_code": device.device_code.get_secret_value(),
                            "client_id": self.client_id,
                        },
                    )
                    async with self.lock:
                        self._save(payload)
                    return
                except OAuthProblem as exc:
                    if exc.code == "authorization_pending":
                        continue
                    if exc.code == "slow_down":
                        interval += 5
                        continue
                    raise
                except httpx.TransportError:
                    self.error = "Network unavailable; retrying approval check."
                    interval = min(interval * 2, 30)
            raise OAuthProblem("expired_token")
        except (ConnectionProblem, OSError, httpx.HTTPError) as exc:
            self.state = "error"
            self.error = safe_error(exc)
        finally:
            self.device = None

    async def access_token(self, rejected: str | None = None) -> str:
        async with self.lock:
            if not self.tokens:
                raise LoginRequired("Connect your AutoDarts account.")
            current = self.tokens.access_token.get_secret_value()
            # Concurrent 401s must never rotate the same refresh token twice.
            if self.tokens.expires_at > time.time() + 60 and rejected != current:
                return current
            try:
                payload = await self._post(
                    "refresh",
                    {
                        "client_id": self.client_id,
                        "refresh_token": self.tokens.refresh_token.get_secret_value(),
                    },
                )
                self._save(payload)
            except OAuthProblem as exc:
                if exc.code in {"invalid_grant", "invalid_client", "unauthorized_client"}:
                    self.tokens = None
                    remove_private_file(self.path)
                    self.state = "disconnected"
                    self.error = str(exc)
                    raise LoginRequired(str(exc)) from exc
                raise
            assert self.tokens is not None
            return self.tokens.access_token.get_secret_value()

    async def close(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
        self.poll_task = None
        self.device = None

    async def forget(self) -> None:
        await self.close()
        async with self.lock:
            self.tokens = None
            remove_private_file(self.path)
            self.state = "disconnected" if self.client_id else "unconfigured"
            self.error = None


def safe_error(exc: BaseException) -> str:
    if isinstance(exc, ConnectionProblem):
        return str(exc)
    if isinstance(exc, OSError):
        return "Network connection failed. Check the network and configured address."
    if isinstance(exc, httpx.HTTPError):
        return "HTTP connection failed. Check the network and configured address."
    if isinstance(exc, json.JSONDecodeError):
        return "Invalid JSON response."
    return "Connection interrupted. Retrying automatically."
