import asyncio
import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import BOARD_ID, MATCH_ID
from fastapi.testclient import TestClient
from pydantic import ValidationError
from zeroconf import IPVersion, ServiceStateChange

from ochecore import wled
from ochecore.autodarts.auth import DeviceAuth
from ochecore.autodarts.cloud import CloudConnection
from ochecore.autodarts.errors import ConnectionProblem
from ochecore.cli import ControlError, execute, parser
from ochecore.events import Event, EventBus, EventNormalizer
from ochecore.main import create_app
from ochecore.wled import (
    WLED,
    Appearance,
    Device,
    DeviceWorker,
    Matrix,
    Preview,
    WLEDConfig,
    discover_devices,
    inspect_device,
    matrix_pixels,
    payload,
    request,
    validate_capabilities,
)


def device(**updates):
    return Device.model_validate(
        {
            "id": "board",
            "name": "Board",
            "url": "http://wled.test",
            "targets": [{"id": "ring", "name": "Ring"}],
            **updates,
        }
    )


def capabilities():
    return {
        "info": {"name": "Test WLED", "ver": "0.15.3"},
        "state": {
            "on": True,
            "bri": 255,
            "seg": [
                {"id": 0, "start": 0, "stop": 16, "len": 16},
                {"id": 1, "start": 16, "stop": 144, "len": 128},
            ],
        },
        "effects": ["Solid", "Blink", "RSVD"],
    }


def event(name, **data):
    return Event(
        sequence=1,
        source="core",
        kind="normalized",
        event=name,
        data={"player": {"is_local": True}, **data},
    )


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/lights",
        "http://user:secret@wled.test",
        "http://wled.test/api",
        "http://wled.test?x=1",
        "http://bad host",
        "http://wled.test:99999",
    ],
)
def test_rejects_non_origin_wled_addresses(url):
    with pytest.raises(ValidationError):
        device(url=url)


def test_targets_validate_overlap_and_matrix_limits():
    a = {"id": "first", "name": "First", "mode": "pixels", "start": 0, "count": 5}
    b = {"id": "second", "name": "Second", "mode": "pixels", "start": 4, "count": 2}
    with pytest.raises(ValidationError, match="overlap"):
        device(targets=[a, b])
    valid = device(targets=[a, {**b, "start": 5}])
    assert len(valid.targets) == 2
    with pytest.raises(ValidationError, match="overlap"):
        device(targets=[{**a, "mode": "segment"}, {**b, "start": 5}])
    with pytest.raises(ValidationError):
        Matrix(width=64, height=64)
    with pytest.raises(ValidationError):
        Matrix(width=8, height=8)
    assert Matrix(width=8, height=16, rotation=90).width == 8
    with pytest.raises(ValidationError, match="once"):
        WLEDConfig(devices=[valid, valid.model_copy(update={"id": "other"})])


def test_segment_and_pixel_targets_are_scoped_and_composed():
    board = device(
        targets=[
            {"id": "first", "name": "First", "mode": "pixels", "start": 0, "count": 1},
            {"id": "second", "name": "Second", "mode": "pixels", "start": 2, "count": 2},
            {"id": "matrix", "name": "Score", "mode": "matrix", "segment": 1},
        ]
    )
    body = payload(board, {"phase": "takeout", "remaining": 180}, {})
    assert set(body) == {"seg", "tt"}  # No master power/brightness or segment geometry changes.
    assert body["seg"][0]["i"] == [0, 1, "808000", 2, 4, "808000"]
    assert len(body["seg"]) == 2 and len(body["seg"][1]["i"]) == 129
    changed = payload(board, {"phase": "takeout", "remaining": 40}, {})
    assert changed["seg"][0] == body["seg"][0]
    assert changed["seg"][1] != body["seg"][1]
    native = payload(device(), {"phase": "ready"}, {})["seg"][0]
    assert native["frz"] is False and native["col"] == [[0, 255, 0]]
    assert "i" not in native


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_matrix_rotation_wiring_and_unknown_scores(rotation):
    matrix = Matrix(width=16, height=16, rotation=rotation, serpentine=False)
    color = Appearance(color="#ffffff", brightness=255)
    normal = matrix_pixels(matrix, 180, color)
    assert len(normal) == 256 and sum(p != "000000" for p in normal) == 33
    reference = matrix_pixels(matrix.model_copy(update={"rotation": 0}), 180, color)
    grid = [reference[row * 16 : (row + 1) * 16] for row in range(16)]
    for _ in range(rotation // 90):
        grid = list(zip(*grid[::-1], strict=True))
    assert normal == [pixel for row in grid for pixel in row]
    snake = matrix_pixels(matrix.model_copy(update={"serpentine": True}), 180, color)
    for row in range(16):
        expected = normal[row * 16 : (row + 1) * 16]
        assert snake[row * 16 : (row + 1) * 16] == (expected[::-1] if row % 2 else expected)
    assert matrix_pixels(matrix, None, color) == matrix_pixels(matrix, 10000, color)
    assert matrix_pixels(matrix, 0, color) != matrix_pixels(matrix, None, color)


async def test_capabilities_bounds_reserved_effects_and_malformed_device():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=capabilities()))
    ) as http:
        info = await inspect_device(http, device())
        validate_capabilities(device(), info)
        with pytest.raises(ConnectionProblem, match="exceed"):
            validate_capabilities(
                device(targets=[{"id": "x", "name": "x", "mode": "pixels", "count": 17}]), info
            )
        with pytest.raises(ConnectionProblem, match="does not exist"):
            validate_capabilities(device(targets=[{"id": "x", "name": "x", "segment": 5}]), info)
        invalid = device()
        invalid.targets[0].phases.ready.effect = 2
        with pytest.raises(ConnectionProblem, match="unavailable"):
            validate_capabilities(invalid, info)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
    ) as http:
        with pytest.raises(ConnectionProblem, match="invalid response"):
            await inspect_device(http, device())


async def test_priority_expiry_corrections_and_target_preview(monkeypatch):
    async with httpx.AsyncClient() as http:
        worker = DeviceWorker(device(), http, EventBus(), lambda: {})
        view = {"phase": "ready", "available": True}
        worker.accept(event("turn_end", score=180, busted=False))
        worker.accept(event("leg_win"))
        worker.accept(event("match_win"))
        worker.accept(event("turn_end", score=180, busted=False))
        assert worker.desired(view)["seg"][0]["col"] == [[160, 0, 255]]
        future = time.monotonic() + 5
        monkeypatch.setattr("ochecore.wled.time.monotonic", lambda: future)
        assert worker.desired({**view, "phase": "takeout"})["seg"][0]["col"] == [[255, 255, 0]]
        worker.accept(event("bust", player={"is_local": False}))
        assert not worker.overlays
        worker.accept(event("bust"))
        worker.accept(event("throw_corrected"))
        assert not worker.overlays
        old = event("match_win")
        old.received_at = datetime.now(UTC) - timedelta(seconds=10)
        worker.accept(old)
        assert not worker.overlays
        worker.preview = (future + 1, Preview(target_id="ring", phase="ready"))
        assert worker.desired({"phase": "waiting", "available": False})["seg"][0]["col"] == [
            [0, 255, 0]
        ]
        worker.preview = None
        assert worker.desired({"phase": "waiting", "available": False})["seg"][0]["col"] == [
            [255, 0, 0]
        ]


async def test_worker_isolation_preview_expiry_and_shutdown(tmp_path):
    sent = []
    blocked = asyncio.Event()

    async def transport(request):
        assert "authorization" not in request.headers
        if request.url.host == "offline.test":
            await blocked.wait()
        if request.method == "GET":
            return httpx.Response(200, json=capabilities())
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    view = {"phase": "waiting", "available": True}
    bus = EventBus()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        service = WLED(tmp_path / "wled.json", http, bus, lambda: view)
        await service.configure(
            WLEDConfig(
                enabled=True, devices=[device(), device(id="offline", url="http://offline.test")]
            )
        )
        try:
            await until(lambda: bool(sent))
            assert sent[-1]["seg"][0]["col"] == [[255, 0, 0]]
            result = await service.test("board", Preview(target_id="ring", duration=0.5))
            assert result["applied"] and sent[-1]["seg"][0]["col"] == [[0, 255, 0]]
            view["phase"] = "takeout"
            await until(lambda: sent[-1]["seg"][0]["col"] == [[255, 255, 0]])
            bus.publish("core", "match_win", {"player": {"is_local": True}}, kind="normalized")
            await until(lambda: sent[-1]["seg"][0]["col"] == [[160, 0, 255]])
        finally:
            await service.close()
        assert sent[-1]["seg"][0]["col"] == [[255, 0, 0]]
        assert not bus.normalized_queues and not service.tasks


def test_headless_api_config_probe_cli_and_restart(settings, capsys, tmp_path):
    settings.ui_enabled = False
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=capabilities())

    app = create_app(settings, transport=httpx.MockTransport(handler))
    values = WLEDConfig(devices=[device()]).model_dump(mode="json")
    with TestClient(app) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/game").json()["phase"] == "waiting"
        assert client.put("/api/wled", json=values).status_code == 200
        assert requests == []  # Saving disabled configuration never contacts hardware.
        result = client.post("/api/wled/board/probe", json={})
        assert result.status_code == 200 and result.json()["segments"][1]["length"] == 128
        assert client.post("/api/wled/board/test", json={"target_id": "ring"}).status_code == 409
        assert (
            client.put(
                "/api/wled", json=values, headers={"Origin": "https://other.test"}
            ).status_code
            == 403
        )
        assert client.get("/api/wled").headers["cache-control"] == "no-store"
        broken = deepcopy(values)
        broken["devices"][0]["url"] = "file:///test"
        assert client.put("/api/wled", json=broken).status_code == 422
        assert client.get("/api/wled").json() == values
        path = tmp_path / "input.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        execute(parser().parse_args(["wled", "config", "--file", str(path)]), client)
        assert json.loads(capsys.readouterr().out) == values
        execute(parser().parse_args(["wled", "disable", "board"]), client)
        assert json.loads(capsys.readouterr().out)["saved"]
        with pytest.raises(ControlError, match="Unknown"):
            execute(parser().parse_args(["wled", "enable", "missing"]), client)
    with TestClient(create_app(settings)) as client:
        restored = client.get("/api/wled").json()
        assert restored["devices"][0]["enabled"] is False
        assert restored["devices"][0]["targets"][0]["phases"]["takeout"]["color"] == "#ffff00"


def test_bad_saved_wled_configuration_does_not_stop_core(settings):
    (settings.data_dir / "wled.json").write_text("broken", encoding="utf-8")
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/wled/status").json()["error"]
        assert client.put("/api/wled", json={}).status_code == 200
        assert client.get("/api/wled/status").json()["error"] is None


def test_game_phases_require_readiness_and_corrected_scores():
    bus = EventBus()
    normalizer = EventNormalizer(BOARD_ID, bus)
    normalizer.select(MATCH_ID)
    frame = json.loads((Path(__file__).parent / "fixtures/x01-state.json").read_text())

    def feed(data, name="match.state", **kwargs):
        normalizer.consume(bus.publish("cloud", name, deepcopy(data), **kwargs))

    feed(frame, snapshot=True)
    assert not bus.normalized_history
    assert normalizer.current_state(True)["phase"] == "waiting"
    feed({"status": "Ready for throw"}, "board.state")
    assert normalizer.current_state(True)["phase"] == "ready"
    frame["turns"][0]["throws"] = [
        {"id": "one", "segment": {"number": 20, "multiplier": 3, "name": "T20"}}
    ]
    frame["turns"][0]["points"] = 60
    frame["gameScores"][0] = 441
    feed(frame)
    assert normalizer.current_state(True)["remaining"] == 441
    frame["turns"][0]["throws"][0]["segment"] = {"number": 20, "multiplier": 1, "name": "S20"}
    frame["gameScores"][0] = 481
    frame["turns"][0]["points"] = 20
    feed(frame)
    assert normalizer.current_state(True)["last_dart"] == 20
    assert normalizer.current_state(True)["remaining"] == 481
    feed({"event": "Takeout started"}, "board.events")
    assert normalizer.current_state(True)["phase"] == "takeout"
    feed({"event": "Takeout finished"}, "board.events")
    assert normalizer.current_state(True)["phase"] == "waiting"
    feed({"status": "Ready"}, "board.state")
    frame["player"] = 1
    feed(frame)
    assert normalizer.current_state(True)["phase"] == "waiting"
    assert normalizer.current_state(True)["turn_score"] == 0
    assert normalizer.current_state(False)["remaining"] is None
    normalizer.resync()
    assert normalizer.current_state(True)["remaining"] is None


async def test_game_view_rejects_offline_or_stale_board(tmp_path):
    async with httpx.AsyncClient() as http:
        auth = DeviceAuth(http, "https://api.autodarts.com", "test", tmp_path / "tokens.json")
        cloud = CloudConnection(http, auth, BOARD_ID, EventBus())
        cloud.state = "connected"
        cloud.board = {"state": {"connected": True}}
        cloud.board_checked_at = time.monotonic()
        assert cloud.game_state()["phase"] == "idle"
        cloud.board_checked_at -= 100
        assert cloud.game_state()["available"] is False
        cloud.board_checked_at = time.monotonic()
        cloud.state = "reconnecting"
        assert cloud.game_state()["phase"] == "waiting"


def test_remote_takeout_and_invalid_state_do_not_show_local_readiness():
    bus = EventBus()
    normalizer = EventNormalizer(BOARD_ID, bus)
    normalizer.select(MATCH_ID)
    frame = json.loads((Path(__file__).parent / "fixtures/x01-state.json").read_text())
    frame["player"] = 1
    frame["turns"][0]["playerId"] = "remote-player"
    normalizer.consume(bus.publish("cloud", "match.state", frame, snapshot=True))
    normalizer.board_status({"status": "Ready"})
    normalizer.consume(bus.publish("cloud", "match.events", {"event": "Takeout started"}))
    assert normalizer.current_state(True)["phase"] == "waiting"
    normalizer.consume(bus.publish("cloud", "match.events", {"event": "Takeout finished"}))
    frame["player"] = 0
    frame["turns"][0]["playerId"] = "local-player"
    normalizer.consume(bus.publish("cloud", "match.state", frame))
    assert normalizer.current_state(True)["phase"] == "ready"
    invalid = {**frame, "players": []}
    normalizer.consume(bus.publish("cloud", "match.state", invalid))
    assert normalizer.current_state(True)["phase"] == "waiting"
    assert normalizer.current_state(True)["remaining"] is None
    normalizer.consume(bus.publish("cloud", "match.state", frame))
    assert normalizer.current_state(True)["remaining"] == 501
    assert normalizer.current_state(True)["phase"] == "waiting"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="invalid"),
        httpx.Response(200, json={"error": 9}),
        httpx.Response(200, json={"success": False}),
        httpx.Response(200, text="null"),
        httpx.Response(200, json=[]),
    ],
)
async def test_rejected_device_commands_are_not_successful(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(ConnectionProblem):
            await request(http, device(), "/json/state", {"seg": []})


async def test_disable_during_preview_clears_lights_and_workers(tmp_path):
    sent = []

    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, json=capabilities())
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        bus = EventBus()
        service = WLED(
            tmp_path / "wled.json", http, bus, lambda: {"phase": "idle", "available": True}
        )
        await service.configure(WLEDConfig(enabled=True, devices=[device()]))
        try:
            await until(lambda: bool(sent))
            await service.test("board", Preview(target_id="ring"))
            assert sent[-1]["seg"][0]["col"] == [[0, 255, 0]]
            await service.configure(WLEDConfig(enabled=False, devices=[device()]))
            assert sent[-1]["seg"][0]["col"] == [[255, 0, 0]]
            assert not service.tasks and not bus.normalized_queues
        finally:
            await service.close()


def mock_discovery(monkeypatch, records):
    """Advertise services without opening multicast sockets in the test suite."""
    lifecycle = {"closed": False, "cancelled": False}

    class Zeroconf:
        def __init__(self, *, ip_version):
            assert ip_version is IPVersion.V4Only
            self.zeroconf = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            lifecycle["closed"] = True

    class Browser:
        def __init__(self, zeroconf, service_type, *, handlers):
            assert service_type == wled.WLED_SERVICE
            for name, record in records.items():
                args = dict(zeroconf=zeroconf, service_type=service_type, name=name)
                handlers[0](**args, state_change=ServiceStateChange.Added)
                handlers[0](**args, state_change=ServiceStateChange.Updated)
                if record.get("removed"):
                    handlers[0](**args, state_change=ServiceStateChange.Removed)

        async def async_cancel(self):
            lifecycle["cancelled"] = True

    class Info:
        def __init__(self, service_type, name):
            self.record = records[name]
            self.port = self.record.get("port", 80)
            self.server = self.record.get("hostname", f"{name}.local.")

        async def async_request(self, zeroconf, timeout_ms):
            if "pending" in self.record:
                self.record["pending"].set()
                await asyncio.Future()
            return self.record.get("resolved", True)

        def parsed_addresses(self, version):
            assert version is IPVersion.V4Only
            return self.record.get("addresses", [])

    monkeypatch.setattr(wled, "AsyncZeroconf", Zeroconf)
    monkeypatch.setattr(wled, "AsyncServiceBrowser", Browser)
    monkeypatch.setattr(wled, "AsyncServiceInfo", Info)
    monkeypatch.setattr(wled, "DISCOVERY_SECONDS", 0.01)
    return lifecycle


async def test_discovery_verifies_deduplicates_and_filters_advertisements(monkeypatch):
    records = {
        "ring": {"addresses": ["192.168.1.20"]},
        "alias": {"addresses": ["192.168.1.20"], "hostname": "ring.local."},
        "matrix": {"addresses": ["192.168.1.21"], "port": 8080},
        "offline": {"addresses": ["192.168.1.22"]},
        "wrong-service": {"addresses": ["192.168.1.23"]},
        "removed": {"addresses": ["192.168.1.24"], "removed": True},
        "unresolved": {"resolved": False},
        "invalid": {"addresses": ["not-an-address"]},
        "loopback": {"addresses": ["127.0.0.1", "0.0.0.0"]},
    }
    lifecycle = mock_discovery(monkeypatch, records)
    requests = []

    def transport(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/json"
        assert "authorization" not in request.headers
        if request.url.host == "192.168.1.22":
            raise httpx.ConnectError("offline")
        if request.url.host == "192.168.1.23":
            return httpx.Response(200, json={"product": "not WLED"})
        data = capabilities()
        data["info"]["name"] = "Matrix" if request.url.port == 8080 else "Ring"
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        found = await discover_devices(http)
    assert [item["name"] for item in found] == ["Matrix", "Ring"]
    assert [item["url"] for item in found] == ["http://192.168.1.21:8080", "http://192.168.1.20"]
    assert found[1]["hostname"] == "ring.local"
    assert len(requests) == 6  # Updated announcements do not repeat a probe.
    assert lifecycle == {"closed": True, "cancelled": True}


async def test_discovery_limits_work_and_closes_pending_probes(monkeypatch):
    pending = asyncio.Event()
    records = {str(index): {"pending": pending} for index in range(80)}
    lifecycle = mock_discovery(monkeypatch, records)
    async with httpx.AsyncClient() as http:
        assert await asyncio.wait_for(discover_devices(http), timeout=1) == []
    assert pending.is_set()
    assert lifecycle == {"closed": True, "cancelled": True}


async def test_discovery_cancellation_and_concurrent_scan(tmp_path, monkeypatch):
    pending = asyncio.Event()
    lifecycle = mock_discovery(monkeypatch, {"ring": {"pending": pending}})
    monkeypatch.setattr(wled, "DISCOVERY_SECONDS", 10)
    async with httpx.AsyncClient() as http:
        service = WLED(tmp_path / "wled.json", http, EventBus(), lambda: {})
        scan = asyncio.create_task(service.discover())
        try:
            await asyncio.wait_for(pending.wait(), timeout=1)
            with pytest.raises(ConnectionProblem, match="already running"):
                await service.discover()
            assert service.status()["enabled"] is False
            assert not service.path.exists()
        finally:
            scan.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scan
        assert not service.discovery_lock.locked()
    assert lifecycle == {"closed": True, "cancelled": True}


async def test_discovery_network_failure_is_actionable(monkeypatch):
    def fail(**kwargs):
        raise OSError("network interface unavailable")

    monkeypatch.setattr(wled, "AsyncZeroconf", fail)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConnectionProblem, match="add an address manually"):
            await discover_devices(http)


def test_discovery_api_cli_without_ui_or_enabling_lights(settings, monkeypatch, capsys):
    found = [
        {
            "name": "Ring",
            "url": "http://192.168.1.20",
            "address": "192.168.1.20",
            "hostname": "ring.local",
            "version": "0.15.3",
        }
    ]

    async def discover(http):
        return found

    monkeypatch.setattr(wled, "discover_devices", discover)
    settings.ui_enabled = False
    with TestClient(create_app(settings)) as client:
        execute(parser().parse_args(["wled", "discover"]), client)
        assert json.loads(capsys.readouterr().out) == {"devices": found}
        assert client.get("/api/wled").json() == {"enabled": False, "devices": []}
        assert not (settings.data_dir / "wled.json").exists()
        assert (
            client.post(
                "/api/wled/discover", json={}, headers={"Origin": "https://other.test"}
            ).status_code
            == 403
        )
        assert client.post("/api/wled/discover", content="{}").status_code == 415
        assert client.get("/healthz").status_code == 200
