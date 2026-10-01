import asyncio
import json
import time

import httpx
import pytest
from conftest import BOARD_ID
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

from ochecore.cli import ControlError, execute, parser, run
from ochecore.main import create_app


def test_cli_configuration_uses_headless_api_and_preserves_locked_fields(settings, capsys):
    settings.client_id = "registered-client"
    settings.ui_enabled = False
    with TestClient(create_app(settings)) as client:
        args = parser().parse_args(["config", "--board-id", BOARD_ID])
        assert execute(args, client) == 0
        configured = json.loads(capsys.readouterr().out)
        assert configured["board_id"] == BOARD_ID
        assert configured["client_id"] == "registered-client"
        assert configured["locked_fields"] == ["client_id"]
        assert (
            json.loads((settings.data_dir / "connection.json").read_text())["board_id"] == BOARD_ID
        )
        with pytest.raises(ControlError, match="managed"):
            execute(parser().parse_args(["config", "--client-id", "another"]), client)
        with pytest.raises(ControlError, match="422"):
            execute(parser().parse_args(["config", "--board-id", "invalid"]), client)
        assert client.get("/api/config").json()["board_id"] == BOARD_ID
        execute(parser().parse_args(["config", "--board-id="]), client)
        assert client.get("/api/config").json()["board_id"] == ""


def device_response():
    return {
        "device_code": "private-device-code",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://auth.autodarts.com/link",
        "expires_in": 60,
        "interval": 0.01,
    }


@pytest.mark.parametrize("result", ["approved", "access_denied"])
def test_cli_login_and_session_lifecycle_without_ui(settings, monkeypatch, capsys, result):
    settings.client_id = "registered-client"
    settings.ui_enabled = False
    sleep = time.sleep
    monkeypatch.setattr("ochecore.cli.time.sleep", lambda _: sleep(0.02))

    def upstream(request):
        assert json.loads(request.content)["client_id"] == "registered-client"
        if request.url.path.endswith("device/code"):
            return httpx.Response(200, json=device_response())
        assert request.url.path.endswith("device/token")
        if result == "access_denied":
            return httpx.Response(400, json={"error": "access_denied"})
        return httpx.Response(
            200,
            json={
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_in": 900,
            },
        )

    with TestClient(create_app(settings, transport=httpx.MockTransport(upstream))) as client:
        args = parser().parse_args(["login"])
        if result == "access_denied":
            with pytest.raises(ControlError):
                execute(args, client)
        else:
            assert execute(args, client) == 0
            assert client.get("/api/status").json()["auth"]["state"] == "authenticated"
        output = capsys.readouterr().out
        assert "ABCD-EFGH" in output
        assert "https://auth.autodarts.com/link" in output
        assert "private-" not in output

    if result == "approved":
        with TestClient(create_app(settings)) as client:
            execute(parser().parse_args(["status"]), client)
            status = json.loads(capsys.readouterr().out)
            assert status["auth"]["state"] == "authenticated"
            assert "private-" not in json.dumps(status)
            execute(parser().parse_args(["logout"]), client)
            assert client.get("/api/status").json()["auth"]["state"] == "disconnected"
            assert not (settings.data_dir / "tokens.json").exists()


def test_cli_no_wait_leaves_approval_in_service(settings, capsys):
    settings.client_id = "registered-client"
    settings.ui_enabled = False

    def upstream(request):
        assert request.url.path.endswith("device/code")
        return httpx.Response(200, json={**device_response(), "interval": 30})

    app = create_app(settings, transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        assert execute(parser().parse_args(["login", "--no-wait", "--json"]), client) == 0
        output = capsys.readouterr().out
        assert json.loads(output)["state"] == "awaiting_authorization"
        assert "private-device-code" not in output
        assert not app.state.runtime.auth.poll_task.done()


@pytest.mark.parametrize("raw", [True, False])
def test_cli_event_history_is_available_without_ui(settings, capsys, raw):
    settings.ui_enabled = False
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.runtime.bus.record_raw('{"score":60}')
        app.state.runtime.bus.publish("core", "throw", {"score": 60}, kind="normalized")
        execute(parser().parse_args(["events"] + (["--raw"] if raw else [])), client)
        event = json.loads(capsys.readouterr().out)[0]
        assert (event if raw else event["data"]) == {"score": 60}


@pytest.mark.parametrize("raw", [True, False])
async def test_cli_follow_streams_json_and_reports_disconnect(capsys, raw):
    async def server(ws):
        assert ws.request.path == ("/events/raw" if raw else "/events")
        await ws.send(json.dumps({"event": "board.events", "data": {"score": 60}}))

    async with serve(server, "127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        result = await asyncio.wait_for(
            asyncio.to_thread(
                run,
                ["--url", f"http://127.0.0.1:{port}", "events", "--follow"]
                + (["--raw"] if raw else []),
            ),
            timeout=5,
        )
    assert result == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["data"]["score"] == 60
    assert "Event stream closed" in output.err


def test_cli_service_mode_disables_ui(settings, monkeypatch):
    monkeypatch.setattr("ochecore.cli.Settings", lambda: settings)
    launches = []
    monkeypatch.setattr("ochecore.cli.uvicorn.run", lambda app, **kwargs: launches.append(app))
    assert run(["serve", "--no-ui"]) == 0
    with TestClient(launches[0]) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/status").status_code == 200


def test_cli_unreachable_service_has_nonzero_exit(monkeypatch, capsys):
    def offline(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.Client, "request", offline)
    assert run(["status"]) == 1
    assert "Cannot reach OcheCore" in capsys.readouterr().err


def test_cli_lists_account_boards_without_ui(settings, capsys):
    settings.client_id = "test-client"
    settings.ui_enabled = False

    def handler(request):
        assert request.url.path == "/bs/v0/boards"
        return httpx.Response(
            200, json=[{"id": BOARD_ID, "name": "Home", "state": {"connected": True}}]
        )

    app = create_app(settings, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        app.state.runtime.auth._save(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 900,
            }
        )
        assert execute(parser().parse_args(["boards"]), client) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["boards"] == [{"id": BOARD_ID, "name": "Home", "online": True}]
        assert result["selected_board_id"] is None
