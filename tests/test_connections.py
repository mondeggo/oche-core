import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import BOARD_ID, MATCH_ID
from websockets.asyncio.server import serve

from ochecore.autodarts.auth import DeviceAuth
from ochecore.autodarts.cloud import CloudConnection
from ochecore.autodarts.errors import ConnectionProblem
from ochecore.events import EventBus


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))


def tokens(auth):
    auth._save({"access_token": "access", "refresh_token": "refresh", "expires_in": 900})


async def test_cloud_bootstrap_match_switch_and_stale_events(tmp_path):
    def handler(request):
        assert request.headers["authorization"] == "Bearer access"
        if "/boards/" in request.url.path:
            return httpx.Response(200, json={"id": BOARD_ID, "matchId": MATCH_ID})
        return httpx.Response(200, json={"id": request.url.path.split("/")[-2], "turns": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = DeviceAuth(http, "https://api.autodarts.com", "test", tmp_path / "tokens.json")
        tokens(auth)
        bus, socket = EventBus(), Socket()
        cloud = CloudConnection(http, auth, BOARD_ID, bus)
        await cloud.reconcile(socket)
        assert cloud.match_id == MATCH_ID
        assert bus.history[-1].snapshot
        assert socket.sent[-1] == {
            "channel": "autodarts.matches",
            "topic": f"{MATCH_ID}.state",
            "type": "subscribe",
        }
        for raw in ("{", "[]", '{"data": []}', '{"channel":null}'):
            await cloud.on_message(socket, raw)
        assert cloud.invalid_messages == 4
        new_match = "33333333-3333-4333-8333-333333333333"
        await cloud.on_message(
            socket,
            json.dumps(
                {
                    "channel": "autodarts.boards",
                    "topic": f"{BOARD_ID}.matches",
                    "data": {"event": "start", "id": new_match},
                }
            ),
        )
        assert cloud.match_id == new_match
        assert socket.sent[-2]["type"] == "unsubscribe"
        count = bus.sequence
        await cloud.on_message(
            socket,
            json.dumps(
                {
                    "channel": "autodarts.matches",
                    "topic": f"{MATCH_ID}.state",
                    "data": {"id": MATCH_ID},
                }
            ),
        )
        assert bus.sequence == count
        await cloud.on_message(
            socket,
            json.dumps(
                {
                    "channel": "autodarts.boards",
                    "topic": f"{BOARD_ID}.matches",
                    "data": {"event": "finish", "id": MATCH_ID},
                }
            ),
        )
        assert cloud.match_id == new_match
        await cloud.on_message(
            socket,
            json.dumps(
                {
                    "channel": "autodarts.boards",
                    "topic": f"{BOARD_ID}.matches",
                    "data": {"event": "delete", "id": new_match},
                }
            ),
        )
        assert cloud.match_id is None


async def test_http_401_refreshes_and_retries_once(tmp_path):
    def handler(request):
        if request.url.path.endswith("refresh"):
            return httpx.Response(
                200, json={"access_token": "new", "refresh_token": "new-r", "expires_in": 900}
            )
        if request.headers["authorization"] == "Bearer access":
            return httpx.Response(401)
        return httpx.Response(200, json={"id": BOARD_ID})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = DeviceAuth(http, "https://api.autodarts.com", "test", tmp_path / "tokens.json")
        tokens(auth)
        cloud = CloudConnection(http, auth, BOARD_ID, EventBus())
        assert await cloud.get(f"/bs/v0/boards/{BOARD_ID}") == {"id": BOARD_ID}
        assert auth.tokens.access_token.get_secret_value() == "new"


async def test_discovery_refreshes_rejected_token_and_keeps_unknown_status(tmp_path):
    board_requests = []

    def handler(request):
        if request.url.path.endswith("refresh"):
            return httpx.Response(
                200,
                json={
                    "access_token": "new",
                    "refresh_token": "new-r",
                    "expires_in": 900,
                },
            )
        assert request.url.path == "/bs/v0/boards"
        board_requests.append(request.headers["authorization"])
        if len(board_requests) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json=[{"id": BOARD_ID, "connected": "false"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = DeviceAuth(http, "https://api.autodarts.com", "test", tmp_path / "tokens.json")
        tokens(auth)
        cloud = CloudConnection(http, auth, "", EventBus())
        assert await cloud.list_boards() == [{"id": BOARD_ID, "name": BOARD_ID, "online": None}]
        assert board_requests == ["Bearer access", "Bearer new"]


@pytest.mark.parametrize("status", [403, 503])
async def test_discovery_service_failure_is_not_an_empty_list(tmp_path, status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text="private details"))
    ) as http:
        auth = DeviceAuth(http, "https://api.autodarts.com", "test", tmp_path / "tokens.json")
        tokens(auth)
        cloud = CloudConnection(http, auth, "", EventBus())
        with pytest.raises(ConnectionProblem, match=f"HTTP {status}") as failure:
            await cloud.list_boards()
        assert "private details" not in str(failure.value)


async def test_websocket_reconnect_restores_all_subscriptions(tmp_path):
    connections = []
    tickets = []
    second_received = asyncio.Event()

    async def server(ws):
        assert "authorization" not in ws.request.headers
        assert parse_qs(urlsplit(ws.request.path).query) == {"code": [tickets[-1]]}
        subscriptions = [json.loads(await ws.recv()) for _ in range(4)]
        connections.append(subscriptions)
        if len(connections) == 1:
            await ws.close(code=1012, reason="restart")
        else:
            await ws.send(
                json.dumps(
                    {
                        "channel": "autodarts.matches",
                        "topic": f"{MATCH_ID}.state",
                        "data": {"id": MATCH_ID, "marker": "after-reconnect"},
                    }
                )
            )
            second_received.set()
            await ws.wait_closed()

    def handler(request):
        assert request.headers["authorization"] == "Bearer access"
        if request.url.path.endswith("tickets"):
            assert str(request.url) == "https://play.ws.autodarts.com/ms/v0/tickets"
            assert request.method == "POST"
            tickets.append(f"ticket {len(tickets)}&+/?")
            return httpx.Response(201, json={"code": tickets[-1]})
        if request.url.path.endswith("userinfo"):
            return httpx.Response(200, json={"sub": "user-1"})
        if "/boards/" in request.url.path:
            return httpx.Response(200, json={"matchId": MATCH_ID})
        return httpx.Response(200, json={"id": MATCH_ID, "turns": []})

    async with serve(server, "127.0.0.1", 0) as server_ws:
        port = server_ws.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            auth = DeviceAuth(http, "https://api.autodarts.com", "test", tmp_path / "tokens.json")
            tokens(auth)
            bus = EventBus()
            cloud = CloudConnection(http, auth, BOARD_ID, bus)
            cloud.ws_url = f"ws://127.0.0.1:{port}/ms/v0/subscribe"
            with bus.subscribe() as queue:
                task = asyncio.create_task(cloud.run())
                try:
                    await asyncio.wait_for(second_received.wait(), 8)
                    async with asyncio.timeout(2):
                        while (await queue.get()).data.get("marker") != "after-reconnect":
                            pass
                    assert connections[0] == connections[1]
                    assert len(connections[1]) == 4
                    assert cloud.reconnects == 1
                    assert cloud.state == "connected"
                    assert len(tickets) == 2
                    assert tickets[0] != tickets[1]
                    assert all(ticket not in json.dumps(cloud.status()) for ticket in tickets)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


async def test_ticket_401_refreshes_and_retries_once(tmp_path):
    ticket_headers = []

    def handler(request):
        if request.url.path == "/auth/v1/refresh":
            assert json.loads(request.content)["client_id"] == "ochecore-test"
            return httpx.Response(
                200, json={"access_token": "new", "refresh_token": "new-r", "expires_in": 900}
            )
        assert str(request.url) == "https://play.ws.autodarts.com/ms/v0/tickets"
        assert request.method == "POST"
        ticket_headers.append(request.headers["authorization"])
        if len(ticket_headers) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"code": "fresh-ticket"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = DeviceAuth(
            http, "https://api.autodarts.com", "ochecore-test", tmp_path / "tokens.json"
        )
        tokens(auth)
        cloud = CloudConnection(http, auth, BOARD_ID, EventBus())
        assert await cloud.request_ticket() == "fresh-ticket"
        assert ticket_headers == ["Bearer access", "Bearer new"]


@pytest.mark.parametrize("payload", [{}, {"code": ""}, {"code": 42}, [], None])
async def test_invalid_ticket_response_is_rejected(tmp_path, payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        auth = DeviceAuth(http, "https://api.autodarts.com", "test", tmp_path / "tokens.json")
        tokens(auth)
        cloud = CloudConnection(http, auth, BOARD_ID, EventBus())
        with pytest.raises(ConnectionProblem):
            await cloud.request_ticket()


@pytest.mark.parametrize("status", [302, 403, 503])
async def test_ticket_failure_does_not_expose_response_or_follow_redirect(tmp_path, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://unrelated.example/ticket"},
            text="private upstream details",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = DeviceAuth(http, "https://api.autodarts.com", "test", tmp_path / "tokens.json")
        tokens(auth)
        cloud = CloudConnection(http, auth, BOARD_ID, EventBus())
        with pytest.raises(ConnectionProblem, match=f"HTTP {status}") as failure:
            await cloud.request_ticket()
        assert "private upstream details" not in str(failure.value)
        assert len(requests) == 1


def test_bus_slow_subscriber_is_bounded_and_tokens_redacted():
    bus = EventBus(capacity=2)
    with bus.subscribe(capacity=1) as queue:
        for index in range(3):
            bus.publish("cloud", "test", {"index": index, "nested": {"access_token": "secret"}})
        assert bus.dropped == 2
        event = queue.get_nowait()
        assert event.sequence == 3
        assert event.data["nested"]["access_token"] == "[redacted]"
        assert len(bus.history) == 2
    assert not bus.queues
