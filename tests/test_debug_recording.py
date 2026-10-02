import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ochecore.autodarts.errors import ConnectionProblem
from ochecore.cli import execute, parser
from ochecore.events import EventBus, RawEventRecorder
from ochecore.main import create_app


async def test_capture_keeps_every_raw_frame_beyond_bounded_history(tmp_path):
    recorder = RawEventRecorder(tmp_path / "debug")
    bus = EventBus(recorder=recorder)
    bus.record_raw('{"before_recording":true}')
    assert not recorder.directory.exists()
    await recorder.configure(True)
    first = recorder.path
    assert (await recorder.configure(True))["file"] == first.name  # Idempotent toggle.
    for number in range(250):
        bus.record_raw(
            json.dumps(
                {
                    "number": number,
                    "access_token": "secret",
                    "unknown": {"password": "secret", "field": [1, 2]},
                }
            )
        )
        bus.publish("core", "throw", {"number": number}, kind="normalized")
    bus.record_raw("not JSON")
    bus.record_raw(b"\xff")
    assert len(bus.raw_history) == len(bus.normalized_history) == 100
    with pytest.raises(ConnectionProblem, match="Stop recording"):
        recorder.download_path()
    await recorder.configure(False)
    lines = [json.loads(line) for line in recorder.download_path().read_text().splitlines()]
    assert len(lines) == recorder.status()["recorded"] == 252
    assert [row["raw"]["number"] for row in lines[:250]] == list(range(250))
    assert lines[0]["sequence"] == 2 and lines[0]["received_at"].endswith("+00:00")
    assert lines[0]["raw"]["unknown"] == {"password": "[redacted]", "field": [1, 2]}
    assert "secret" not in first.read_text()
    assert lines[-2]["raw"] == "not JSON" and lines[-1]["raw"] == "\ufffd"
    size = first.stat().st_size
    assert recorder.status()["bytes"] == size
    bus.record_raw('{"after_recording":true}')
    assert first.stat().st_size == size
    await recorder.configure(True)
    assert recorder.path != first
    bus.record_raw('{"second_session":true}')
    await recorder.stop()
    assert first.stat().st_size == size
    restarted = RawEventRecorder(recorder.directory)
    assert not restarted.enabled
    assert restarted.download_path() == recorder.path


async def test_capture_disk_failure_stops_recording_without_affecting_events(tmp_path, monkeypatch):
    recorder = RawEventRecorder(tmp_path)
    bus = EventBus(recorder=recorder)
    await recorder.configure(True)

    def fail(batch):
        raise OSError("Disk full")

    monkeypatch.setattr(recorder, "_append", fail)
    bus.record_raw('{"type":"ping"}')
    await asyncio.wait_for(recorder.task, 1)
    assert not recorder.enabled and "incomplete" in recorder.error
    assert recorder.queue.empty()
    bus.record_raw('{"type":"pong"}')
    assert len(bus.raw_history) == 2
    assert recorder.recorded == 0


async def test_capture_overflow_is_reported_and_drains_queued_frames(tmp_path):
    recorder = RawEventRecorder(tmp_path)
    await recorder.configure(True)
    recorder.queue = asyncio.Queue(maxsize=2)
    for number in range(3):
        recorder.record({"n": number}, number)
    assert not recorder.enabled and "incomplete" in recorder.error
    await recorder.stop()
    assert recorder.recorded == 2
    assert len(recorder.path.read_text().splitlines()) == 2


async def test_capture_creation_failure_and_no_file(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    recorder = RawEventRecorder(blocked / "debug")
    with pytest.raises(ConnectionProblem, match="Cannot create"):
        await recorder.configure(True)
    assert not recorder.enabled
    with pytest.raises(ConnectionProblem, match="No debug"):
        recorder.download_path()


def test_debug_api_works_headless_survives_connection_reset_and_restarts_off(settings):
    settings.ui_enabled = False
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/events/debug").json()["enabled"] is False
        assert client.get("/api/events/debug/file").status_code == 409
        assert client.put("/api/events/debug", json={}).status_code == 422
        assert (
            client.put(
                "/api/events/debug",
                json={"enabled": True},
                headers={"Origin": "https://example.org"},
            ).status_code
            == 403
        )
        assert client.put("/api/events/debug", content="enabled=true").status_code == 415
        assert client.put("/api/events/debug", json={"enabled": True}).status_code == 200
        assert client.get("/api/events/debug/file").status_code == 409

        async def publish():
            app.state.runtime.bus.record_raw('{"channel":"unknown","data":{"access_token":"x"}}')
            app.state.runtime.bus.record_raw('{"channel":"unknown","data":{"access_token":"x"}}')

        client.portal.call(publish)
        assert client.post("/api/auth/logout", json={}).status_code == 200
        assert client.get("/api/events/debug").json()["enabled"] is True
        assert client.get("/api/events/raw").json() == []
        stopped = client.put("/api/events/debug", json={"enabled": False}).json()
        assert stopped["recorded"] == 2
        capture = client.get(stopped["download_url"])
        assert capture.status_code == 200
        assert capture.headers["content-type"].startswith("application/x-ndjson")
        assert len(capture.text.splitlines()) == 2
        assert (
            json.loads(capture.text.splitlines()[0])["raw"]["data"]["access_token"] == "[redacted]"
        )
        assert client.get("/api/status").json()["events"]["debug"]["file"] == stopped["file"]
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/events/debug").json()["enabled"] is False
        assert client.get("/api/events/debug/file").content == capture.content


def test_shutdown_flushes_pending_recording(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        client.put("/api/events/debug", json={"enabled": True})

        async def publish():
            for n in range(200):
                app.state.runtime.bus.record_raw(json.dumps({"n": n}))

        client.portal.call(publish)
    files = list((settings.data_dir / "debug").glob("*.jsonl"))
    assert len(files) == 1 and len(files[0].read_text().splitlines()) == 200


def test_debug_cli_controls_the_service_without_opening_a_local_file(capsys):
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"enabled": request.method == "PUT"})

    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handle)) as http:
        for value in ("on", "off", "status"):
            execute(parser().parse_args(["events", "--debug", value]), http)
    assert calls == [
        ("PUT", "/api/events/debug", {"enabled": True}),
        ("PUT", "/api/events/debug", {"enabled": False}),
        ("GET", "/api/events/debug", {}),
    ]
    with pytest.raises(SystemExit):
        parser().parse_args(["events", "--follow", "--debug", "on"])
    capsys.readouterr()
