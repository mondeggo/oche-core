"""Game announcements and serial audio playback, independent of the web UI."""

import asyncio
import os
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ochecore.autodarts.errors import ConnectionProblem
from ochecore.events import Event, EventBus
from ochecore.storage import write_private_json
from ochecore.voices import VOICES, VoiceLibrary

GAME_MODES = (
    "X01",
    "Cricket",
    "Count Up",
    "ATC",
    "Random Checkout",
    "RTW",
    "Segment Training",
    "Bob's 27",
    "121",
    "Shanghai",
    "Gotcha",
    "Bermuda",
    "Killer",
    "Bull-off",
)
SEGMENT_MODES = {"Cricket", "ATC", "RTW", "Segment Training", "Bob's 27", "Killer", "Bull-off"}


def game_mode(value: str) -> str:
    compact = value.lower().replace(" ", "").replace("-", "").replace("’", "'")
    return next(
        (mode for mode in GAME_MODES if mode.lower().replace(" ", "").replace("-", "") == compact),
        value,
    )


class CallerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    voice: str = ""
    output: Literal["host", "browser", "both"] = "host"
    volume: float = Field(default=0.6, ge=0, le=1)
    darts: Literal["auto", "segment", "score", "off"] = "auto"
    turn_totals: bool = True
    checkouts: bool = True
    players: bool = True
    include_bots: bool = True
    local_only: bool = False


class CallerTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call: Literal["score", "bust", "win", "checkout", "bull"] = "score"
    score: int = Field(default=180, ge=0, le=180, strict=True)


def player_keys(data: dict) -> list[str]:
    player = data.get("player") or {}
    name = player.get("name", "").strip().lower()
    if player.get("is_bot"):
        return ["bot"]
    return [name, name.replace(" ", "_"), f"player{player.get('index', 0) + 1}", "next_player"]


def segment_keys(dart: dict) -> list[str]:
    segment = dart.get("segment") or {}
    number, multiplier = segment.get("number", 0), segment.get("multiplier", 0)
    if not number or not multiplier:
        return ["m", "miss", "outside", "0"]
    if number == 25:
        return ["bullseye", "d25", "50"] if multiplier == 2 else ["bull", "sbull", "25"]
    prefix = {1: "s", 2: "d", 3: "t"}.get(multiplier, "s")
    return [f"{prefix}{number}", str(number * multiplier)]


def can_checkout(score: int, out_mode: str) -> bool:
    """Check reachability in up to three darts, including the required finishing bed."""
    hits = {0, 25, 50} | {n * m for n in range(1, 21) for m in (1, 2, 3)}
    doubles = {50} | set(range(2, 41, 2))
    final = (
        doubles
        if out_mode == "Double"
        else (doubles | set(range(3, 61, 3)) if out_mode == "Master" else hits - {0})
    )
    return 0 < score <= 180 and any(score - a - b in final for a in hits for b in hits)


def call_parts(event: Event, config: CallerConfig) -> list[list[str]]:
    """Ordered clips, each with compatible fallbacks. Scores come from AutoDarts."""
    data, name = event.data, event.event
    mode = game_mode(data.get("variant", ""))
    player = data.get("player") or {}
    if data.get("editing") or (player.get("is_bot") and not config.include_bots):
        return []
    if config.local_only and player and not player.get("is_local"):
        return []
    if name == "match_started":
        return [["matchon", "gameon"]]
    if name in {"match_win", "leg_win"}:
        win = (
            ["matchshot", "gameshot"]
            if name == "match_win"
            else [f"gameshot_l{data.get('leg', 1)}_n", "gameshot", "matchshot"]
        )
        return [win] + ([player_keys(data)] if config.players else [])
    if name == "bust":
        return [["busted", "bust", "0"]]
    if name == "turn_started":
        parts = [player_keys(data)] if config.players else []
        if mode == "Bull-off":
            return [["bulloff", "bulling_start"]] + parts
        if data.get("leg_start"):
            parts.append(["gameon"])
        settings = data.get("settings") or {}
        remaining = data.get("remaining")
        checkout = bool(data.get("checkout_available"))
        if mode == "Gotcha" and isinstance(data.get("game_score"), int):
            target = settings.get("targetScore")
            if isinstance(target, int):
                remaining = target - data["game_score"]
                checkout = can_checkout(remaining, settings.get("outMode", "Straight"))
        if config.checkouts and checkout and isinstance(remaining, int) and 0 < remaining <= 180:
            parts.extend([["you_require"], [f"c_{remaining}", str(remaining)]])
        target = data.get("target") or {}
        if mode in {"ATC", "RTW", "Segment Training"} and isinstance(target.get("number"), int):
            bed = str(target.get("bed", "")).lower()
            prefix = {"double": "d", "triple": "t"}.get(bed, "s")
            number = target["number"]
            parts.append([f"{prefix}{number}", str(number)])
        return parts
    if name == "throw":
        style = config.darts
        if style == "auto":
            style = "segment" if mode in SEGMENT_MODES else "off"
        if style == "segment":
            return [segment_keys(data.get("dart") or {})]
        if style == "score":
            return [[str(data["dart"]["points"])]]
    if name == "turn_end" and config.turn_totals and not data.get("busted"):
        # These games count targets/lives; a summed physical dart score is misleading.
        if mode in {"ATC", "Killer", "Bull-off", "Segment Training"}:
            return []
        score = data.get("turn_total") if mode == "Cricket" else data.get("score")
        if not isinstance(score, int):
            return []
        if mode == "Bermuda" and score < 0:
            return [["ber_minus"], [str(abs(score))]]
        if score < 0:
            return []
        return [[str(score)]]
    return []


def plan_batch(events: list[Event], config: CallerConfig) -> list[tuple[Event, list[list[str]]]]:
    """A winning dart calls the win once; a bust suppresses its visit total."""
    priority = {"throw": 0, "turn_end": 1, "bust": 2, "leg_win": 3, "match_win": 4}
    outcomes = {}
    for event in events:
        if event.event in priority:
            key = (
                event.data.get("match_id"),
                event.data.get("set"),
                event.data.get("leg"),
                event.data.get("turn_id"),
            )
            outcomes[key] = max(outcomes.get(key, 0), priority[event.event])
    result = []
    for event in events:
        if event.source != "core" or event.snapshot:
            continue
        key = (
            event.data.get("match_id"),
            event.data.get("set"),
            event.data.get("leg"),
            event.data.get("turn_id"),
        )
        rank = outcomes.get(key, 0)
        if event.event in priority and rank >= 2 and priority[event.event] < rank:
            continue
        parts = call_parts(event, config)
        if parts:
            result.append((event, parts))
    return result


class HostAudio:
    """SDL uses the host's default sound device; no device is opened until a call plays."""

    def __init__(self):
        self.mixer = None

    async def play(self, path: Path, volume: float) -> None:
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        try:
            if self.mixer is None:
                import pygame.mixer as mixer

                mixer.init()
                self.mixer = mixer
            self.mixer.music.load(str(path))
            self.mixer.music.set_volume(volume)
            self.mixer.music.play()
            async with asyncio.timeout(12):
                # SDL has no asyncio completion event; poll its single music channel.
                while self.mixer.music.get_busy():  # noqa: ASYNC110
                    await asyncio.sleep(0.04)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ConnectionProblem(
                "Host audio unavailable. Check the default sound device or choose browser playback."
            ) from exc
        finally:
            self.stop()

    def stop(self) -> None:
        if self.mixer is not None:
            self.mixer.music.stop()
            self.mixer.music.unload()

    def close(self) -> None:
        if self.mixer is not None:
            self.stop()
            self.mixer.quit()
            self.mixer = None


class Caller:
    def __init__(
        self,
        directory: Path,
        http: httpx.AsyncClient,
        bus: EventBus,
        game_state: Callable[[], dict],
    ):
        self.path = directory / "caller.json"
        self.library = VoiceLibrary(directory / "voices", http)
        self.bus, self.game_state = bus, game_state
        self.config = CallerConfig()
        self.error: str | None = None
        try:
            if self.path.is_file():
                self.config = CallerConfig.model_validate_json(
                    self.path.read_text(encoding="utf-8")
                )
        except (ValueError, OSError, ValidationError):
            self.error = "Cannot read caller settings. Save them again to recover."
        self.audio = HostAudio()
        self.tasks: list[asyncio.Task] = []
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self.listeners: set[asyncio.Queue] = set()
        self.history: deque = deque(maxlen=20)
        self.generation = 0
        self.dropped = 0
        self.missing: list[str] = []
        self.current: str | None = None
        self.active_match: str | None = None
        self.win_expires = 0.0

    def start(self) -> None:
        if self.config.enabled and not self.tasks:
            self.tasks = [
                asyncio.create_task(self._listen(), name="caller-events"),
                asyncio.create_task(self._play(), name="caller-audio"),
            ]

    async def configure(self, config: CallerConfig) -> None:
        if config.voice and config.voice not in VOICES:
            raise ConnectionProblem("Choose a voice from the catalogue.")
        write_private_json(self.path, config.model_dump())
        await self._close_tasks()
        self.config, self.error = config, None
        self.start()

    def stop(self) -> None:
        self.generation += 1
        while not self.queue.empty():
            self.queue.get_nowait()
        self.audio.stop()
        self.current = None
        self.active_match = None
        self.win_expires = 0.0
        self._broadcast({"type": "stop"})

    async def _close_tasks(self) -> None:
        self.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.audio.close()

    async def close(self) -> None:
        await self._close_tasks()
        await self.library.close()

    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "voice": self.config.voice,
            "installed": self.library.installed(self.config.voice),
            "output": self.config.output,
            "error": self.error,
            "playing": self.current,
            "queued": self.queue.qsize(),
            "dropped": self.dropped,
            "browser_listeners": len(self.listeners),
            "download": dict(self.library.download),
            "missing_sounds": self.missing,
            "recent_calls": list(self.history),
            "game_modes": GAME_MODES,
        }

    @contextmanager
    def subscribe(self):
        queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self.listeners.add(queue)
        try:
            yield queue
        finally:
            self.listeners.discard(queue)

    def _broadcast(self, message: dict) -> None:
        for queue in self.listeners:
            if message["type"] == "stop" or queue.full():
                while not queue.empty():
                    queue.get_nowait()
            queue.put_nowait(message)

    def enqueue(self, name: str, parts: list[list[str]], match_id: str | None = None) -> dict:
        if not self.config.enabled:
            raise ConnectionProblem("Enable Caller first.")
        clips, missing = [], []
        for alternatives in parts:
            resolved = self.library.resolve(self.config.voice, alternatives)
            if resolved:
                key, filename = resolved
                clips.append(
                    {
                        "key": key,
                        "file": filename,
                        "url": f"/api/caller/audio/{self.config.voice}/{filename}",
                    }
                )
            else:
                missing.append(alternatives[0])
        self.missing = missing
        self.error = None
        result = {"event": name, "clips": clips, "missing": missing}
        if clips:
            if match_id:
                self.active_match = match_id
                if name == "match_win":
                    self.win_expires = time.monotonic() + 8
            self.history.append({"event": name, "sounds": [clip["key"] for clip in clips]})
            if self.config.output in {"browser", "both"}:
                self._broadcast(
                    {
                        "type": "play",
                        "clips": clips,
                        "volume": self.config.volume,
                        "expires_at": time.time() + 8,
                    }
                )
            if self.config.output in {"host", "both"}:
                if self.queue.full():
                    self.queue.get_nowait()
                    self.dropped += 1
                self.queue.put_nowait(
                    (self.generation, time.monotonic(), match_id, name == "match_win", clips)
                )
        return result

    def test(self, sample: CallerTest) -> dict:
        parts = {
            "score": [[str(sample.score)]],
            "bust": [["busted", "bust"]],
            "win": [["matchshot", "gameshot"]],
            "checkout": [["you_require"], [f"c_{sample.score}", str(sample.score)]],
            "bull": [["bullseye", "d25", "50"]],
        }[sample.call]
        return self.enqueue("test", parts)

    async def _listen(self) -> None:
        with self.bus.subscribe() as queue:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 0.5)
                except TimeoutError:
                    state = self.game_state()
                    finishing = (
                        state.get("match_id") is None and time.monotonic() < self.win_expires
                    )
                    if self.active_match and (
                        not state.get("available")
                        or (state.get("match_id") != self.active_match and not finishing)
                    ):
                        self.stop()
                    continue
                events = [event]
                await asyncio.sleep(0.03)
                while not queue.empty():
                    events.append(queue.get_nowait())
                if any(
                    e.event in {"throw_corrected", "throw_removed", "match_editing"} for e in events
                ):
                    self.stop()
                    continue
                if any(e.event == "match_started" for e in events):
                    self.stop()
                if any(
                    e.event == "match_ended" and e.data.get("reason") == "delete" for e in events
                ):
                    self.stop()
                    continue
                for event, parts in plan_batch(events, self.config):
                    if (time.time() - event.received_at.timestamp()) > 8:
                        continue
                    try:
                        self.enqueue(event.event, parts, event.data.get("match_id"))
                    except ConnectionProblem as exc:
                        self.error = str(exc)

    async def _play(self) -> None:
        while True:
            generation, created, match_id, won, clips = await self.queue.get()
            for clip in clips:
                state = self.game_state()
                if generation != self.generation or time.monotonic() - created > 8:
                    break
                finishing = won and state.get("match_id") is None
                if match_id and (
                    not state.get("available")
                    or (state.get("match_id") != match_id and not finishing)
                ):
                    break
                try:
                    self.current = clip["key"]
                    path = self.library.clip_path(self.config.voice, clip["file"])
                    await self.audio.play(path, self.config.volume)
                except ConnectionProblem as exc:
                    self.error = str(exc)
                    break
                finally:
                    self.current = None
