import asyncio
import json

import httpx
import pytest
from conftest import BOARD_ID
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ochecore.config import Settings
from ochecore.main import create_app


def test_unconfigured_startup_and_configuration_persistence(settings):
    def unexpected_request(request):
        pytest.fail(f"Unconfigured app attempted {request.method} {request.url.path}")

    app = create_app(settings, transport=httpx.MockTransport(unexpected_request))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/readyz").status_code == 503
        assert client.get("/api/status").json()["auth"]["state"] == "unconfigured"
        assert client.post("/api/auth/login", json={}).status_code == 409
        config = {"client_id": "my-app", "board_id": BOARD_ID}
        assert client.put("/api/config", json=config).status_code == 200
        assert client.get("/api/status").json()["cloud"]["state"] == "waiting_for_login"
        assert client.get("/api/config").json()["board_id"] == BOARD_ID
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/config").json()["client_id"] == "my-app"


def test_config_validation_origin_guard_and_no_secrets(settings):
    with TestClient(create_app(settings)) as client:
        assert client.put("/api/config", json={"board_id": "invalid"}).status_code == 422
        assert (
            client.put("/api/config", json={"local_board_url": "file:///etc/passwd"}).status_code
            == 422
        )
        assert (
            client.post(
                "/api/auth/logout", json={}, headers={"Origin": "https://other.site"}
            ).status_code
            == 403
        )
        assert client.post("/api/auth/logout", data={}).status_code == 415
        assert client.get("/api/status").headers["cache-control"] == "no-store"
        assert "tokens" not in client.get("/api/config").json()
        assert client.get("/openapi.json").status_code == 200


def test_env_fields_cannot_be_overwritten(settings):
    settings.client_id = "env-client"
    with TestClient(create_app(settings)) as client:
        assert "client_id" in client.get("/api/config").json()["locked_fields"]
        assert client.put("/api/config", json={"client_id": "another"}).status_code == 409
        assert client.put("/api/config", json={"client_id": "env-client"}).status_code == 200


@pytest.mark.parametrize("ui_enabled", [True, False])
@pytest.mark.parametrize("raw", [True, False])
def test_websocket_live_delivery_and_idle_cleanup(settings, ui_enabled, raw):
    settings.ui_enabled = ui_enabled
    app = create_app(settings)
    with TestClient(app) as client:
        with client.websocket_connect("/events/raw" if raw else "/events") as ws:

            async def publish():
                app.state.runtime.bus.record_raw('{"event":"Takeout finished"}')
                app.state.runtime.bus.publish(
                    "core", "takeout_finished", {"event": "Takeout finished"}, kind="normalized"
                )

            client.portal.call(publish)
            message = ws.receive_json()
            assert (message if raw else message["data"])["event"] == "Takeout finished"

        async def assert_clean():
            async with asyncio.timeout(1):
                # Observe teardown in another task; production code has no completion signal.
                bus = app.state.runtime.bus
                while bus.normalized_queues or bus.raw_queues:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)

        client.portal.call(assert_clean)


def test_raw_api_preserves_cloud_frame_and_normalized_api_is_separate(settings):
    settings.ui_enabled = False
    app = create_app(settings)
    payload = {
        "channel": "autodarts.boards",
        "topic": f"{BOARD_ID}.events",
        "data": {"event": "Takeout started", "new_field": [1, 2]},
        "server_metadata": {"version": 2},
    }
    with TestClient(app) as client:
        cloud = app.state.runtime.cloud
        cloud.board_id = BOARD_ID
        cloud.publish("match.state", {"id": "snapshot"}, snapshot=True)
        assert client.get("/api/events/raw").json() == []
        client.portal.call(cloud.on_message, None, json.dumps(payload))
        assert client.get("/api/events/raw").json() == [payload]
        normalized = client.get("/api/events").json()
        assert [event["event"] for event in normalized] == ["takeout_started"]
        assert all(event["kind"] == "normalized" for event in normalized)
        assert client.get("/api/events/raw").headers["cache-control"] == "no-store"
        assert client.post("/api/auth/logout", json={}).status_code == 200
        assert client.get("/api/events/raw").json() == []
        assert client.get("/api/events").json() == []


def test_readiness_reports_rejected_required_subscription(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        cloud = app.state.runtime.cloud
        cloud.state = "connected"
        cloud.subscriptions.add(("autodarts.boards", f"{BOARD_ID}.events"))
        client.portal.call(
            cloud.on_message,
            None,
            json.dumps(
                {
                    "type": "error",
                    "channel": "autodarts.boards",
                    "topic": f"{BOARD_ID}.events",
                    "error": "unauthorized client",
                }
            ),
        )
        assert client.get("/readyz").status_code == 503
        assert client.get("/api/status").json()["cloud"]["state"] == "degraded"


def test_logout_forgets_persisted_token(settings):
    settings.client_id = "test"
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.runtime.auth._save(
            {
                "access_token": "private",
                "refresh_token": "private-r",
                "expires_in": 900,
            }
        )
        assert "private" not in json.dumps(client.get("/api/status").json())
        assert client.post("/api/auth/logout", json={}).status_code == 200
        assert not (settings.data_dir / "tokens.json").exists()
        assert client.get("/api/status").json()["auth"]["state"] == "disconnected"


@pytest.mark.parametrize(
    "url", ["https://user:pass@api.test", "http://api.test", "https://api.test/path"]
)
def test_cloud_requires_https_origin(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_base_url=url)


def test_headless_mode_serves_api_without_static_files(settings, monkeypatch):
    settings.ui_enabled = False
    monkeypatch.setattr("ochecore.main.STATIC", settings.data_dir / "missing-static-directory")
    with TestClient(create_app(settings)) as client:
        for path in ("/", "/static/app.js", "/docs", "/redoc"):
            assert client.get(path).status_code == 404
        assert client.get("/healthz").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert "local" not in client.get("/api/status").json()
        assert "local_board_url" not in client.get("/api/config").json()
        assert client.put("/api/config", json={"local_board_url": ""}).status_code == 422
        assert client.get("/readyz").status_code == 503


@pytest.mark.parametrize("ui_enabled", [True, False])
def test_account_board_discovery_without_configured_board(settings, ui_enabled):
    settings.client_id = "test-client"
    settings.ui_enabled = ui_enabled
    other_board = "33333333-3333-4333-8333-333333333333"

    def upstream(request):
        assert request.method == "GET"
        assert request.url.path == "/bs/v0/boards"
        assert request.headers["authorization"] == "Bearer private-access"
        return httpx.Response(
            200,
            json=[
                {
                    "id": BOARD_ID,
                    "name": "Home",
                    "connected": True,
                    "access_token": "upstream-secret",
                },
                {"id": other_board, "name": "Club", "online": False, "owner": {"email": "private"}},
            ],
        )

    app = create_app(settings, transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        assert client.get("/api/boards").status_code == 409
        app.state.runtime.auth._save(
            {
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_in": 900,
            }
        )
        response = client.get("/api/boards")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "boards": [
                {"id": BOARD_ID, "name": "Home", "online": True},
                {"id": other_board, "name": "Club", "online": False},
            ],
            "selected_board_id": None,
        }
        assert "private" not in response.text and "upstream-secret" not in response.text
        assert client.get("/api/config").json()["board_id"] == ""
        assert not (settings.data_dir / "connection.json").exists()


@pytest.mark.parametrize(
    "payload,status", [([], 200), ({}, 409), ([{"id": "bad"}], 409), ([None], 409)]
)
def test_discovery_distinguishes_empty_and_invalid_responses(settings, payload, status):
    settings.client_id = "test-client"
    app = create_app(
        settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    with TestClient(app) as client:
        app.state.runtime.auth._save(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 900,
            }
        )
        response = client.get("/api/boards")
        assert response.status_code == status
        if status == 200:
            assert response.json()["boards"] == []
        else:
            assert "invalid board" in response.json()["detail"]
