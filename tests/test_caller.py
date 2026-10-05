import asyncio
import io
import json
import wave
import zipfile
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import BOARD_ID, MATCH_ID
from fastapi.testclient import TestClient

from ochecore.autodarts.errors import ConnectionProblem
from ochecore.cli import execute, parser
from ochecore.events import Event, EventBus, EventNormalizer
from ochecore.integrations.caller.service import (
    GAME_MODES,
    Caller,
    CallerConfig,
    CallerTest,
    HostAudio,
    call_parts,
    can_checkout,
    game_mode,
    plan_batch,
)
from ochecore.integrations.caller.voices import VOICES, VoiceLibrary, unpack_pack
from ochecore.main import create_app

VOICE = "amazon-fr-fr-remi-male"


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sound:
        sound.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
        sound.writeframes(b"\0\0" * 2205)
    return buffer.getvalue()


def archive_bytes(nested=True, bad_count=False):
    audio = io.BytesIO()
    with zipfile.ZipFile(audio, "w") as sounds:
        # Archive paths are untrusted; the installer assigns its own local filenames.
        sounds.writestr("../../0000.wav", wav_bytes())
        sounds.writestr("../../0001.wav", wav_bytes())
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr("voice.csv", "180!;180+1;\n" + ("" if bad_count else "180!!;180+2;\n"))
        if nested:
            archive.writestr("clips.zip", audio.getvalue())
        else:
            with zipfile.ZipFile(audio) as sounds:
                for name in sounds.namelist():
                    archive.writestr(name, sounds.read(name))
    return result.getvalue()


def install_fixture(root):
    pack = root / "voices" / VOICE
    pack.mkdir(parents=True)
    (pack / "00000.wav").write_bytes(wav_bytes())
    keys = [
        "180",
        "60",
        "40",
        "0",
        "t20",
        "bullseye",
        "busted",
        "matchshot",
        "gameshot",
        "you_require",
        "gameon",
        "player1",
        "player2",
        "bulling_start",
    ]
    (pack / "index.json").write_text(
        json.dumps({"voice_id": VOICE, "clips": 1, "sounds": {key: ["00000.wav"] for key in keys}})
    )


def event(name, variant="X01", **data):
    return Event(
        sequence=1,
        source="core",
        kind="normalized",
        event=name,
        data={
            "variant": variant,
            "match_id": MATCH_ID,
            "turn_id": "visit-1",
            "set": 1,
            "leg": 1,
            "player": {"id": "player", "name": "Alice", "index": 0, "is_local": True},
            **data,
        },
    )


@pytest.mark.parametrize("nested", [False, True])
def test_pack_formats_variants_and_safe_paths(tmp_path, nested):
    archive = tmp_path / "source.zip"
    archive.write_bytes(archive_bytes(nested))
    destination = tmp_path / "pack"
    destination.mkdir()
    manifest = unpack_pack(archive, destination, VOICE)
    assert manifest["sounds"]["180"] == ["00000.wav", "00001.wav"]
    assert (destination / "00000.wav").read_bytes() == wav_bytes()
    assert not (tmp_path.parent / "0000.wav").exists()


def test_invalid_pack_and_expansion_limits(tmp_path, monkeypatch):
    archive = tmp_path / "source.zip"
    archive.write_bytes(archive_bytes(bad_count=True))
    with pytest.raises(ValueError, match="count"):
        unpack_pack(archive, tmp_path, VOICE)
    archive.write_bytes(archive_bytes())
    monkeypatch.setattr("ochecore.integrations.caller.voices.MAX_EXPANDED", 5)
    with pytest.raises(ValueError, match="large"):
        unpack_pack(archive, tmp_path, VOICE)


async def test_install_background_success_failure_and_catalogue(tmp_path):
    response = archive_bytes()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=response))
    ) as http:
        library = VoiceLibrary(tmp_path / "voices", http)
        with pytest.raises(ConnectionProblem):
            library.install("../unknown")
        assert library.install(VOICE)["state"] == "downloading"
        await library.task
        assert library.download["state"] == "installed"
        assert library.resolve(VOICE, ["missing", "180"])[0] == "180"
        assert library.install(VOICE)["state"] == "installed"
        assert not list(library.directory.glob(".install-*"))
        response = b"bad zip"
        other = next(key for key in VOICES if key != VOICE)
        library.install(other)
        await library.task
        assert library.download["state"] == "error"
        assert not library.installed(other)
        assert library.installed(VOICE)
        with pytest.raises(ConnectionProblem):
            library.clip_path(VOICE, "../../tokens.json")


async def test_download_limit_leaves_no_installed_pack(tmp_path, monkeypatch):
    monkeypatch.setattr("ochecore.integrations.caller.voices.MAX_DOWNLOAD", 4)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"too large"))
    ) as http:
        library = VoiceLibrary(tmp_path, http)
        library.install(VOICE)
        await library.task
        assert library.download["state"] == "error"
        assert not library.installed(VOICE)
        assert list(tmp_path.iterdir()) == []


async def test_select_voice_downloads_only_selection_and_removes_previous_pack(tmp_path):
    install_fixture(tmp_path)
    other = next(key for key in VOICES if key != VOICE)
    requests = []

    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=archive_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        caller = Caller(tmp_path, http, EventBus(), lambda: {})
        caller.start()
        caller.library.catalogue()
        assert not requests and caller.library.task is None
        await caller.configure(CallerConfig(voice=other))
        assert caller.config.voice == other
        assert caller.library.installed(VOICE)  # Kept until its replacement is verified.
        await caller.library.task
        assert requests == [VOICES[other]["url"]]
        assert caller.library.installed(other)
        assert not (tmp_path / "voices" / VOICE).exists()
        assert VOICE not in caller.library.cache
        assert list((tmp_path / "voices").iterdir()) == [tmp_path / "voices" / other]
        await caller.configure(CallerConfig(voice=other, volume=0.4))
        assert len(requests) == 1
        await caller.close()


async def test_voice_switch_during_download_preserves_saved_selection(tmp_path):
    release = asyncio.Event()
    other = next(key for key in VOICES if key != VOICE)

    async def handle(request):
        await release.wait()
        return httpx.Response(200, content=archive_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        caller = Caller(tmp_path, http, EventBus(), lambda: {})
        await caller.configure(CallerConfig(voice=VOICE))
        with pytest.raises(ConnectionProblem, match="Wait"):
            await caller.configure(CallerConfig(voice=other))
        assert caller.config.voice == VOICE
        assert json.loads(caller.path.read_text())["voice"] == VOICE
        release.set()
        await caller.library.task
        await caller.close()


async def test_selecting_cached_voice_and_clearing_selection_prunes_cache(tmp_path):
    install_fixture(tmp_path)
    other = next(key for key in VOICES if key != VOICE)
    unused = tmp_path / "voices" / other
    unused.mkdir()
    (unused / "index.json").write_text("{}")
    async with httpx.AsyncClient() as http:
        caller = Caller(tmp_path, http, EventBus(), lambda: {})
        await caller.configure(CallerConfig(voice=VOICE))
        assert not unused.exists()
        await caller.configure(CallerConfig(voice=""))
        assert not caller.library.installed(VOICE)
        await caller.close()


@pytest.mark.parametrize("variant", GAME_MODES + ("CountUp", "Count-Up"))
def test_all_modes_normalized_replay_baseline_visit_and_winner(variant):
    frame = json.loads((Path(__file__).parent / "fixtures/x01-state.json").read_text())
    frame["variant"] = variant
    frame["gameScores"] = [40, 60]
    frame["settings"] = {"gameMode": "Tactics"} if variant == "Cricket" else {}
    bus = EventBus()
    normalizer = EventNormalizer(BOARD_ID, bus)
    normalizer.select(MATCH_ID)

    def feed(snapshot=False):
        before = bus.sequence
        normalizer.consume(bus.publish("cloud", "match.state", deepcopy(frame), snapshot=snapshot))
        return [e for e in bus.normalized_history if e.sequence > before]

    assert feed(snapshot=True) == []
    turn = frame["turns"][0]
    for n in range(3):
        turn["throws"].append(
            {
                "id": str(n),
                "segment": {"number": 20, "multiplier": 3, "name": "T20", "bed": "Triple"},
            }
        )
        turn["points"] = (n + 1) * 60
        turn["score"] = 120
        messages = feed()
    assert feed() == []
    end = next(e for e in messages if e.event == "turn_end")
    assert end.data["score"] == 180
    assert end.data["game_score"] == 40
    assert end.data["remaining"] == (40 if variant in {"X01", "Random Checkout", "121"} else None)
    if game_mode(variant) == "Cricket":
        assert call_parts(end, CallerConfig()) == [["120"]]
    elif game_mode(variant) not in {"ATC", "Killer", "Bull-off", "Segment Training"}:
        assert call_parts(end, CallerConfig()) == [["180"]]
    else:
        assert call_parts(end, CallerConfig()) == []
        throw = next(e for e in messages if e.event == "throw")
        assert call_parts(throw, CallerConfig()) == [["t20", "60"]]
    frame["gameWinner"] = frame["winner"] = 0
    calls = plan_batch(feed(), CallerConfig())
    assert [item[0].event for item in calls] == ["match_win"]
    assert calls[0][1][0][0] == "matchshot"
    normalizer.resync()
    assert feed() == []


def test_win_and_bust_priority_filters_and_corrections():
    events = [
        event("throw", dart={"points": 60, "segment": {"number": 20, "multiplier": 3}}),
        event("turn_end", score=180),
        event("bust"),
        event("leg_win"),
        event("match_win"),
    ]
    assert [e.event for e, _ in plan_batch(events, CallerConfig(darts="segment"))] == ["match_win"]
    assert [e.event for e, _ in plan_batch(events[:3], CallerConfig(darts="segment"))] == ["bust"]
    assert call_parts(event("throw_corrected"), CallerConfig()) == []
    assert call_parts(event("bust", editing=True), CallerConfig()) == []
    assert (
        call_parts(event("bust", player={"is_local": False}), CallerConfig(local_only=True)) == []
    )
    assert (
        call_parts(event("bust", player={"is_bot": True}), CallerConfig(include_bots=False)) == []
    )


def test_checkouts_targets_and_mode_aliases():
    config = CallerConfig(players=False)
    assert can_checkout(170, "Double") and not can_checkout(169, "Double")
    assert can_checkout(180, "Master") and not can_checkout(1, "Double")
    assert game_mode("CountUp") == game_mode("Count-Up") == "Count Up"
    assert game_mode("Bob’s 27") == "Bob's 27"
    assert call_parts(event("turn_started", remaining=40, checkout_available=True), config) == [
        ["you_require"],
        ["c_40", "40"],
    ]
    assert call_parts(event("turn_started", "CountUp", game_score=40), config) == []
    assert call_parts(
        event(
            "turn_started",
            "Gotcha",
            game_score=61,
            settings={"targetScore": 101, "outMode": "Double"},
        ),
        config,
    ) == [["you_require"], ["c_40", "40"]]
    assert call_parts(
        event("turn_started", "ATC", target={"number": 12, "bed": "Double"}), config
    ) == [["d12", "12"]]
    assert call_parts(event("turn_started", "Bull-off"), config) == [["bulloff", "bulling_start"]]
    assert call_parts(event("turn_end", "Bermuda", score=-20), config) == [["ber_minus"], ["20"]]


async def test_audio_queue_serial_stop_errors_and_shutdown(tmp_path):
    install_fixture(tmp_path)
    state = {"available": True, "match_id": MATCH_ID}
    async with httpx.AsyncClient() as http:
        caller = Caller(tmp_path, http, EventBus(), lambda: state)
        entered, release = asyncio.Event(), asyncio.Event()
        played = []

        async def play(path, volume):
            played.append(path.name)
            entered.set()
            await release.wait()

        caller.audio.play = play
        await caller.configure(CallerConfig(enabled=True, voice=VOICE))
        caller.test(CallerTest())
        caller.test(CallerTest())
        await asyncio.wait_for(entered.wait(), 1)
        assert len(played) == 1 and caller.queue.qsize() == 1
        caller.stop()
        release.set()
        await asyncio.sleep(0.01)
        assert len(played) == 1 and caller.queue.empty()
        await caller.close()
        assert not caller.bus.normalized_queues


async def test_browser_calls_corrections_stale_events_and_disconnect(tmp_path):
    install_fixture(tmp_path)
    state = {"available": True, "match_id": MATCH_ID}
    async with httpx.AsyncClient() as http:
        bus = EventBus()
        caller = Caller(tmp_path, http, bus, lambda: state)
        await caller.configure(CallerConfig(enabled=True, voice=VOICE, output="browser"))
        await asyncio.sleep(0)
        with caller.subscribe() as queue:
            bus.publish("core", "turn_end", event("turn_end", score=180).data, kind="normalized")
            message = await asyncio.wait_for(queue.get(), 1)
            assert message["type"] == "play" and message["clips"][0]["key"] == "180"
            bus.publish("core", "throw_corrected", {}, kind="normalized")
            assert (await asyncio.wait_for(queue.get(), 1))["type"] == "stop"
            bus.publish(
                "core",
                "turn_end",
                event("turn_end", score=180).data,
                kind="normalized",
                received_at=datetime.now(UTC) - timedelta(seconds=20),
            )
            await asyncio.sleep(0.08)
            assert queue.empty()
            caller.enqueue("turn_end", [["180"]], MATCH_ID)
            assert (await queue.get())["type"] == "play"
            state["available"] = False
            assert (await asyncio.wait_for(queue.get(), 1))["type"] == "stop"
        await caller.close()


async def test_host_audio_decoder_with_dummy_device(tmp_path, monkeypatch):
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    path = tmp_path / "sound.wav"
    path.write_bytes(wav_bytes())
    audio = HostAudio()
    try:
        await audio.play(path, 0.1)
    finally:
        audio.close()


async def test_match_win_finishes_after_board_clears_match_and_audio_errors_are_isolated(tmp_path):
    install_fixture(tmp_path)
    async with httpx.AsyncClient() as http:
        caller = Caller(tmp_path, http, EventBus(), lambda: {"available": True, "match_id": None})
        played = asyncio.Event()

        async def play(path, volume):
            played.set()
            raise ConnectionProblem("No audio device")

        caller.audio.play = play
        await caller.configure(CallerConfig(enabled=True, voice=VOICE))
        caller.enqueue("match_win", [["matchshot"]], MATCH_ID)
        await asyncio.wait_for(played.wait(), 1)
        assert caller.error == "No audio device"
        assert all(not task.done() for task in caller.tasks)
        await caller.close()


def test_corrupt_voice_manifest_reports_a_recoverable_error(tmp_path):
    install_fixture(tmp_path)
    (tmp_path / "voices" / VOICE / "index.json").write_text('{"sounds": {"180": "../tokens.json"}}')
    library = VoiceLibrary(tmp_path / "voices", None)
    with pytest.raises(ConnectionProblem, match="index"):
        library.resolve(VOICE, ["180"])


def test_caller_api_persistence_and_browser_audio_without_ui(settings):
    settings.ui_enabled = False
    install_fixture(settings.data_dir)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/caller/status").json()["enabled"] is False
        assert len(client.get("/api/caller/voices").json()) == len(VOICES)
        assert client.post("/api/caller/test", json={}).status_code == 409
        assert client.patch("/api/caller", json={"voice": "../bad"}).status_code == 409
        assert client.patch("/api/caller", json={"volume": 2}).status_code == 422
        assert (
            client.patch(
                "/api/caller", json={"enabled": True, "voice": VOICE, "output": "browser"}
            ).status_code
            == 200
        )
        with client.websocket_connect("/caller/audio") as ws:
            response = client.post("/api/caller/test", json={"score": 180})
            assert response.status_code == 200
            message = ws.receive_json()
            assert message["type"] == "play"
            clip = client.get(message["clips"][0]["url"])
            assert clip.content == wav_bytes()
            assert client.post("/api/caller/stop", json={}).status_code == 200
            assert ws.receive_json()["type"] == "stop"
            client.patch("/api/caller", json={"enabled": False})
            assert ws.receive_json()["type"] == "stop"
        assert client.get("/api/caller").json()["voice"] == VOICE
        assert client.get(f"/api/caller/audio/{VOICE}/index.json").status_code == 409
        assert client.post("/api/caller/voices/invalid/install", json={}).status_code == 409
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/caller").json()["voice"] == VOICE
        assert client.get("/api/caller").json()["enabled"] is False


def test_caller_cli_controls_the_api(capsys):
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"saved": True})

    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handle)) as http:
        execute(
            parser().parse_args(
                ["caller", "config", "--voice", VOICE, "--output", "browser", "--volume", "0.5"]
            ),
            http,
        )
        execute(parser().parse_args(["caller", "disable"]), http)
        execute(
            parser().parse_args(["caller", "test", "--call", "checkout", "--score", "40"]), http
        )
    assert calls[0] == (
        "PATCH",
        "/api/caller",
        {"voice": VOICE, "output": "browser", "volume": 0.5},
    )
    assert calls[2] == ("PATCH", "/api/caller", {"enabled": False})
    assert calls[3] == ("POST", "/api/caller/test", {"call": "checkout", "score": 40})
    capsys.readouterr()
