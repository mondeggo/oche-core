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
    DraftPreview,
    Matrix,
    Preview,
    WLEDConfig,
    discover_devices,
    effect_colors,
    inspect_device,
    matrix_pixels,
    parse_outputs,
    payload,
    request,
    validate_capabilities,
)


@pytest.mark.parametrize(
    "metadata, expected",
    [
        ("!;!;;", ["Colour", None, None]),
        ("!,!;;!;1", [None, None, None]),
        ("!;,!;", [None, "Background", None]),
        ("", ["Colour", "Background", "Accent"]),
        (None, ["Colour", "Background", "Accent"]),
    ],
)
def test_effect_color_controls_follow_device_metadata(metadata, expected):
    assert effect_colors(metadata) == expected


def controller_outputs():
    return [
        {"start": 0, "len": 60, "pin": [16], "type": 22},
        {"start": 60, "len": 30, "pin": [2], "type": 22},
    ]


@pytest.mark.parametrize("config_path", ["/json/cfg", "/cfg.json"])
async def test_probe_detects_two_gpio_outputs_with_configuration_fallback(config_path):
    requested = []

    def handler(request):
        requested.append(request)
        if request.url.path == "/json":
            return httpx.Response(200, json=capabilities())
        if request.url.path == "/json/fxdata":
            return httpx.Response(200, json=["!;!;;"])
        if request.url.path == config_path:
            return httpx.Response(
                200,
                json={
                    "hw": {"led": {"ins": controller_outputs()}},
                    "wifi": {"password": "not-for-the-public-api"},
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        info = await inspect_device(http, device(), metadata=True)
    assert [(o["pins"], o["start"], o["stop"]) for o in info["outputs"]] == [
        ([16], 0, 60),
        ([2], 60, 90),
    ]
    assert all(o["type_name"] == "WS281x RGB" for o in info["outputs"])
    assert all(r.method == "GET" for r in requested)
    assert "not-for-the-public-api" not in json.dumps(info)


async def test_output_detection_is_optional_and_never_invents_pins():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, json=capabilities())
                if r.url.path == "/json"
                else httpx.Response(403)
            )
        )
    ) as http:
        info = await inspect_device(http, device(), metadata=True)
    assert info["outputs"] == [] and len(info["segments"]) == 2
    assert parse_outputs([None, {"start": "0", "len": 1}, {"start": 0, "len": 0}]) == []
    outputs = parse_outputs([{"start": 0, "len": 10, "pin": [0, 1, 255, -1, "2"], "type": 30}])
    assert len(outputs) == 1 and outputs[0]["pins"] == [0, 1]  # A two-pin SPI bus is one output.


async def test_worker_keeps_output_metadata_between_probes_and_refreshes_on_request():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return (
            httpx.Response(200, json={"hw": {"led": {"ins": controller_outputs()}}})
            if request.url.path == "/json/cfg"
            else httpx.Response(200, json=capabilities())
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        worker = DeviceWorker(device(), http, EventBus(), lambda: {})
        await worker.probe()
        paths.clear()
        assert len((await worker.probe())["outputs"]) == 2
        assert paths == ["/json"]
        await worker.probe(refresh=True)
        assert "/json/cfg" in paths


def test_second_output_commands_only_address_its_segment():
    configured = device(targets=[{"id": "strip2", "name": "GPIO 2", "segment": 1}])
    command = payload(configured, {"phase": "ready"}, {})
    assert len(command["seg"]) == 1 and command["seg"][0]["id"] == 1
    assert "start" not in command["seg"][0] and "stop" not in command["seg"][0]


def test_unsaved_probe_and_cli_keep_configuration_unchanged(settings, capsys):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json=[";!;;", "!,!;;!;1"] if request.url.path == "/json/fxdata" else capabilities()
        )

    with TestClient(create_app(settings, transport=httpx.MockTransport(handler))) as client:
        before = client.get("/api/wled").json()
        result = client.post("/api/wled/probe", json={"url": "http://draft.test"})
        assert result.status_code == 200
        assert result.json()["effect_colors"][1] == [None, None, None]
        assert all(r.method == "GET" and r.url.host == "draft.test" for r in requests)
        assert client.get("/api/wled").json() == before
        assert not (settings.data_dir / "wled.json").exists()
        args = parser().parse_args(["wled", "probe", "--url", "http://draft.test"])
        assert args.url == "http://127.0.0.1:9180"
        execute(args, client)
        assert json.loads(capsys.readouterr().out)["version"] == "0.15.3"
        assert client.post("/api/wled/probe", json={"url": "file:///tmp"}).status_code == 422
        # Saved devices with no active targets still report a successful connection check.
        config = WLEDConfig(devices=[device(url="http://draft.test", targets=[])]).model_dump(
            mode="json"
        )
        client.put("/api/wled", json=config)
        assert client.post("/api/wled/probe", json={"url": "http://draft.test"}).status_code == 200
        assert client.get("/api/wled/status").json()["devices"][0]["connected"] is True


def preview_state():
    state = capabilities()
    state["state"]["seg"][0].update(on=True, bri=77, fx=1, col=[[4, 5, 6]], frz=False)
    return state


@pytest.mark.parametrize("mode", ["segment", "pixels", "matrix"])
async def test_draft_preview_restores_and_does_not_save(tmp_path, mode):
    sent = []
    snapshot = preview_state()
    snapshot["state"]["on"] = False
    snapshot["state"]["seg"][0]["stop"] = snapshot["state"]["seg"][0]["len"] = 128

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=snapshot)
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = WLED(tmp_path / "wled.json", http, EventBus(), lambda: {})
        draft_device = device(
            enabled=False, targets=[{"id": "ring", "name": "Ring", "mode": mode, "enabled": False}]
        )
        draft_device.targets[0].phases.ready.color = "#123456"
        draft_device.targets[0].matrix.appearance.color = "#abcdef"
        result = await service.preview_draft(
            DraftPreview(device=draft_device, target_id="ring", value=180, duration=0.5)
        )
        assert result["restored"]
        assert sent[0]["on"] is True
        assert sent[-1]["on"] is False and sent[-1]["seg"][0]["col"] == [[4, 5, 6]]
        if mode == "segment":
            assert sent[1]["seg"][0]["col"] == [[18, 52, 86]]
        else:
            assert "i" in sent[1]["seg"][0]
        assert not service.path.exists() and service.config.devices == []


async def test_draft_preview_restores_on_cancellation(tmp_path):
    sent = []
    applied = asyncio.Event()

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=preview_state())
        sent.append(json.loads(request.content))
        applied.set()
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = WLED(tmp_path / "wled.json", http, EventBus(), lambda: {})
        task = asyncio.create_task(
            service.preview_draft(DraftPreview(device=device(), target_id="ring"))
        )
        await asyncio.wait_for(applied.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(sent) == 2 and sent[-1]["seg"][0]["bri"] == 77


async def test_lights_off_survives_game_events_and_configuration_updates(tmp_path):
    state = preview_state()
    sent = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=state)
        body = json.loads(request.content)
        sent.append(body)
        if "on" in body:
            state["state"]["on"] = body["on"]
        return httpx.Response(200, json={"success": True})

    bus = EventBus()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = WLED(
            tmp_path / "wled.json", http, bus, lambda: {"phase": "ready", "available": True}
        )
        await service.configure(WLEDConfig(enabled=True, devices=[device()]))
        try:
            await until(lambda: bool(sent))
            assert (await service.power(False))["applied"]
            stored = service.path.read_bytes()
            bus.publish("core", "match_win", {"player": {"is_local": True}}, kind="normalized")
            await asyncio.sleep(0.35)
            assert sent[-1] == {"on": False}
            assert service.path.read_bytes() == stored
            await service.configure(service.config)
            await asyncio.sleep(0.35)
            assert sent[-1] == {"on": False}
            assert (await service.power(True, "board"))["applied"]
            await until(lambda: "seg" in sent[-1])
            assert sent[-1]["seg"][0]["col"] == [[0, 255, 0]]
        finally:
            await service.close()


async def test_removing_a_target_clears_its_ready_color(tmp_path):
    sent = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=capabilities())
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = WLED(
            tmp_path / "wled.json", http, EventBus(), lambda: {"phase": "ready", "available": True}
        )
        config = WLEDConfig(
            enabled=True,
            devices=[
                device(
                    targets=[
                        {"id": "ring", "name": "Ring"},
                        {"id": "other", "name": "Other", "segment": 1},
                    ]
                )
            ],
        )
        await service.configure(config)
        try:
            await until(lambda: bool(sent))
            config.devices[0].targets.pop()
            await service.configure(config)
            assert any(
                s.get("id") == 1 and s.get("col") == [[255, 0, 0]]
                for body in sent
                for s in body["seg"]
            )
            await until(lambda: len(sent[-1]["seg"]) == 1)
        finally:
            await service.close()


def test_profiles_keep_independent_colors_shared_hardware_and_persist(settings, capsys):
    settings.ui_enabled = False
    with TestClient(create_app(settings)) as client:
        initial = WLEDConfig(devices=[device()]).model_dump(mode="json")
        assert client.put("/api/wled", json=initial).status_code == 200
        execute(parser().parse_args(["wled", "profiles", "create", "Quiet"]), client)
        quiet = json.loads(capsys.readouterr().out)["active_profile"]
        current = client.get("/api/wled").json()
        current["devices"][0]["url"] = "http://shared.test"
        current["devices"][0]["targets"][0]["phases"]["ready"]["color"] = "#123456"
        current["devices"][0]["targets"][0]["players"] = {
            "1": {"color": "#abcdef", "name_filter": "Alice"}
        }
        assert client.patch("/api/wled", json={"devices": current["devices"]}).status_code == 200
        execute(parser().parse_args(["wled", "profiles", "use", "Default"]), client)
        capsys.readouterr()
        current = client.get("/api/wled").json()
        assert current["active_profile"] == "default"
        assert current["devices"][0]["url"] == "http://shared.test"
        assert current["devices"][0]["targets"][0]["phases"]["ready"]["color"] == "#00ff00"
        assert current["devices"][0]["targets"][0]["players"] == {}
        assert client.put("/api/wled/profile", json={"id": quiet}).status_code == 200
        assert client.post("/api/wled/profiles", json={"name": "quiet"}).status_code == 409
        assert client.put("/api/wled/profile", json={"id": "missing"}).status_code == 409
    with TestClient(create_app(settings)) as client:
        current = client.get("/api/wled").json()
        assert current["active_profile"] == quiet
        assert current["devices"][0]["targets"][0]["phases"]["ready"]["color"] == "#123456"
        assert current["devices"][0]["targets"][0]["players"]["1"]["color"] == "#abcdef"
        assert client.delete(f"/api/wled/profiles/{quiet}").status_code == 200
        assert client.get("/api/wled").json()["active_profile"] == "default"
        assert client.delete("/api/wled/profiles/default").status_code == 409


def test_power_api_cli_and_failed_devices(settings, capsys):
    sent = []

    def handler(request):
        if request.url.host == "offline.test":
            raise httpx.ConnectError("offline")
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    with TestClient(create_app(settings, transport=httpx.MockTransport(handler))) as client:
        values = WLEDConfig(
            devices=[device(), device(id="offline", url="http://offline.test")]
        ).model_dump(mode="json")
        client.put("/api/wled", json=values)
        execute(parser().parse_args(["wled", "off", "board"]), client)
        assert json.loads(capsys.readouterr().out)["applied"]
        assert sent == [{"on": False}]
        result = client.post("/api/wled/power", json={"on": False}).json()
        assert not result["applied"] and [d["applied"] for d in result["devices"]] == [True, False]
        assert client.post("/api/wled/board/power", json={"on": "false"}).status_code == 422
        assert (
            client.post(
                "/api/wled/board/power",
                json={"on": False},
                headers={"Origin": "https://other.test"},
            ).status_code
            == 403
        )
        assert client.get("/api/wled").json()["devices"] == values["devices"]


def test_partial_update_cannot_persist_a_missing_active_profile(settings):
    with TestClient(create_app(settings)) as client:
        client.post("/api/wled/profiles", json={"name": "Practice"})
        client.delete("/api/wled/profiles/default")
        before = client.get("/api/wled").json()
        assert client.patch("/api/wled", json={"active_profile": "default"}).status_code == 422
        assert client.get("/api/wled").json() == before


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


@pytest.mark.parametrize(
    "number, multiplier, name",
    [
        (20, 1, "single"),
        (20, 2, "double"),
        (20, 3, "triple"),
        (25, 1, "outer_bull"),
        (25, 2, "bull"),
        (0, 0, "miss"),
    ],
)
async def test_event_matrix_hit_rules_and_fallback_are_isolated(number, multiplier, name):
    board = device(
        targets=[
            {"id": "ring", "name": "Ring", "effects": {name: {"color": "#123456"}}},
            {
                "id": "ambient",
                "name": "Ambient",
                "segment": 1,
                "effects": {"throw": {"color": "#abcdef"}},
            },
            {"id": "white", "name": "White", "segment": 2, "effects": {}},
        ]
    )
    async with httpx.AsyncClient() as http:
        worker = DeviceWorker(board, http, EventBus(), lambda: {})
        hit = event("throw", dart={"segment": {"number": number, "multiplier": multiplier}})
        worker.accept(hit)
        result = worker.desired({"phase": "ready", "available": True})["seg"]
        assert result[0]["col"] == [[18, 52, 86]]
        assert result[1]["col"] == [[171, 205, 239]]
        assert result[2]["col"] == [[0, 255, 0]]
        worker.overlays.clear()
        hit.data["player"]["is_local"] = False
        worker.accept(hit)
        assert not worker.overlays
        hit.data["player"]["is_local"] = True
        hit.snapshot = True
        worker.accept(hit)
        assert not worker.overlays


def test_player_colors_follow_ready_local_player_and_restore_after_takeout():
    board = device(
        targets=[
            {
                "id": "ring",
                "name": "Ring",
                "effects": {},
                "players": {
                    1: {"color": "#123456"},
                    2: {"color": "#abcdef", "name_filter": "Alice"},
                },
            }
        ]
    )
    view = {"phase": "ready", "player": {"index": 0, "name": "Bob", "is_local": True}}
    assert payload(board, view, {})["seg"][0]["col"] == [[18, 52, 86]]
    assert payload(board, {**view, "phase": "takeout"}, {})["seg"][0]["col"] == [[255, 255, 0]]
    view["player"].update(index=3, name="ALICE Smith")
    assert payload(board, view, {})["seg"][0]["col"] == [[171, 205, 239]]
    view["player"]["is_local"] = False
    assert payload(board, view, {})["seg"][0]["col"] == [[0, 255, 0]]
    assert (
        wled.preview_appearance(board.targets[0], Preview(target_id="ring", player=2)).color
        == "#abcdef"
    )
    with pytest.raises(ValidationError):
        Preview(target_id="ring", player=11)


@pytest.mark.parametrize(
    "name",
    ["match_started", "match_ended", "manual_reset", "calibration_started", "calibration_finished"],
)
async def test_board_and_match_flow_events_do_not_require_a_player(name):
    async with httpx.AsyncClient() as http:
        board = device(
            targets=[{"id": "ring", "name": "Ring", "effects": {name: {"color": "#123456"}}}]
        )
        worker = DeviceWorker(board, http, EventBus(), lambda: {})
        worker.accept(event(name, player=None))
        assert worker.overlays["ring"][2].color == "#123456"


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
    config = WLEDConfig(devices=[device()])
    WLED.sync_profiles(config)
    values = config.model_dump(mode="json")
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


def test_integration_switch_preserves_devices_and_device_edits_preserve_switch(settings, capsys):
    settings.ui_enabled = False
    config = WLEDConfig(devices=[device(enabled=False)])
    WLED.sync_profiles(config)
    values = config.model_dump(mode="json")
    with TestClient(create_app(settings)) as client:
        assert client.put("/api/wled", json=values).status_code == 200
        assert client.patch("/api/wled", json={"enabled": True}).status_code == 200
        values["enabled"] = True
        assert client.get("/api/wled").json() == values
        assert client.get("/api/wled/status").json()["enabled"] is True

        execute(parser().parse_args(["wled", "disable"]), client)
        assert json.loads(capsys.readouterr().out)["saved"]
        values["enabled"] = False
        assert client.get("/api/wled").json() == values

        # A settings form opened before disable must not re-enable automation when saved.
        values["devices"][0]["name"] = "Updated controller"
        assert client.patch("/api/wled", json={"devices": values["devices"]}).status_code == 200
        assert client.get("/api/wled").json() == values
        execute(parser().parse_args(["wled", "enable"]), client)
        assert json.loads(capsys.readouterr().out)["saved"]
        values["enabled"] = True

    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/wled").json() == values


def test_wled_partial_updates_validate_and_keep_browser_guards(settings):
    config = WLEDConfig(devices=[device(enabled=False)])
    WLED.sync_profiles(config)
    values = config.model_dump(mode="json")
    with TestClient(create_app(settings)) as client:
        assert client.put("/api/wled", json=values).status_code == 200
        assert client.patch("/api/wled", json={}).status_code == 200
        for invalid in (
            {"enabled": None},
            {"enabled": "invalid"},
            {"devices": None},
            {"devices": values["devices"] * 2},
            {"unknown": True},
        ):
            assert client.patch("/api/wled", json=invalid).status_code == 422
        assert client.patch("/api/wled", content='{"enabled":true}').status_code == 415
        assert (
            client.patch(
                "/api/wled", json={"enabled": True}, headers={"Origin": "https://other.test"}
            ).status_code
            == 403
        )
        assert client.get("/api/wled").json() == values
        assert client.patch("/api/wled", json={"devices": []}).status_code == 200
        assert client.get("/api/wled").json() == WLEDConfig().model_dump(mode="json")


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
        assert client.get("/api/wled").json() == WLEDConfig().model_dump(mode="json")
        assert not (settings.data_dir / "wled.json").exists()
        assert (
            client.post(
                "/api/wled/discover", json={}, headers={"Origin": "https://other.test"}
            ).status_code
            == 403
        )
        assert client.post("/api/wled/discover", content="{}").status_code == 415
        assert client.get("/healthz").status_code == 200
