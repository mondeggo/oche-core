"""Headless WLED targets, phase colours and numeric matrix displays over the JSON API."""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ochecore.autodarts.errors import ConnectionProblem
from ochecore.events import Event, EventBus
from ochecore.storage import write_private_json

Phase = Literal["idle", "ready", "takeout", "waiting"]
EffectName = Literal["triple", "bull", "score_180", "bust", "leg_win", "match_win"]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")]
Colour = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
PRIORITY = {"triple": 1, "bull": 2, "score_180": 3, "bust": 4, "leg_win": 5, "match_win": 6}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Appearance(Model):
    color: Colour = "#ffffff"
    brightness: int = Field(default=128, ge=0, le=255, strict=True)
    effect: int = Field(default=0, ge=0, le=255, strict=True)


class Effect(Appearance):
    duration: float = Field(default=2, ge=0.1, le=30)


class PhaseColours(Model):
    idle: Appearance = Field(default_factory=lambda: Appearance(color="#202020"))
    ready: Appearance = Field(default_factory=lambda: Appearance(color="#00ff00"))
    takeout: Appearance = Field(default_factory=lambda: Appearance(color="#ffff00"))
    waiting: Appearance = Field(default_factory=lambda: Appearance(color="#ff0000"))


class Matrix(Model):
    width: int = Field(default=16, ge=5, le=64, strict=True)
    height: int = Field(default=8, ge=5, le=64, strict=True)
    serpentine: bool = True
    rotation: Literal[0, 90, 180, 270] = 0
    source: Literal["remaining", "turn_score", "last_dart"] = "remaining"
    appearance: Appearance = Field(default_factory=Appearance)

    @model_validator(mode="after")
    def valid_dimensions(self):
        width = self.height if self.rotation in {90, 270} else self.width
        if width < 11 or self.width * self.height > 512:
            raise ValueError("Matrix must fit three digits and contain at most 512 pixels")
        return self


def default_effects() -> dict[EffectName, Effect]:
    return {
        "score_180": Effect(color="#ffb000"),
        "bust": Effect(color="#ff0000"),
        "leg_win": Effect(color="#00aaff"),
        "match_win": Effect(color="#a000ff", duration=4),
    }


class Target(Model):
    id: Identifier
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    segment: int = Field(default=0, ge=0, le=255, strict=True)
    mode: Literal["segment", "pixels", "matrix"] = "segment"
    start: int = Field(default=0, ge=0, le=65535, strict=True)
    count: int = Field(default=1, ge=1, le=1024, strict=True)
    phases: PhaseColours = Field(default_factory=PhaseColours)
    effects: dict[EffectName, Effect] = Field(default_factory=default_effects)
    matrix: Matrix = Field(default_factory=Matrix)

    @property
    def stop(self) -> int:
        count = self.matrix.width * self.matrix.height if self.mode == "matrix" else self.count
        return self.start + count

    @model_validator(mode="after")
    def valid_effects(self):
        appearances = [*self.effects.values(), self.matrix.appearance]
        appearances += [getattr(self.phases, key) for key in Phase.__args__]
        if self.mode != "segment" and any(item.effect for item in appearances):
            raise ValueError("Native WLED effects require a whole-segment target")
        return self


class Device(Model):
    id: Identifier
    name: str = Field(min_length=1, max_length=80)
    url: str
    enabled: bool = True
    targets: list[Target] = Field(default_factory=list, max_length=16)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        if any(character.isspace() for character in value):
            raise ValueError("WLED addresses must not contain whitespace")
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise ValueError("Use a WLED HTTP(S) origin, such as http://wled.local")
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError("Invalid WLED port")
        return value.rstrip("/")

    @model_validator(mode="after")
    def separate_targets(self):
        if len({target.id for target in self.targets}) != len(self.targets):
            raise ValueError("Target IDs must be unique within a device")
        active = [target for target in self.targets if target.enabled]
        for index, first in enumerate(active):
            for second in active[index + 1 :]:
                if first.segment == second.segment and (
                    "segment" in {first.mode, second.mode}
                    or max(first.start, second.start) < min(first.stop, second.stop)
                ):
                    raise ValueError("Enabled targets must not overlap within a segment")
        if sum(t.stop - t.start for t in active if t.mode != "segment") > 1024:
            raise ValueError("A device can control at most 1024 individual pixels")
        return self


class WLEDConfig(Model):
    enabled: bool = False
    devices: list[Device] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_devices(self):
        if len({d.id for d in self.devices}) != len(self.devices):
            raise ValueError("Device IDs must be unique")
        if len({d.url.lower() for d in self.devices}) != len(self.devices):
            raise ValueError("Configure each WLED URL once")
        return self


class Preview(Model):
    target_id: Identifier
    phase: Phase = "ready"
    value: int | None = Field(default=None, ge=0, le=9999, strict=True)
    duration: float = Field(default=3, ge=0.5, le=10)


def rgb(appearance: Appearance, scale: bool = False) -> str:
    values = [int(appearance.color[index : index + 2], 16) for index in (1, 3, 5)]
    if scale:
        values = [round(value * appearance.brightness / 255) for value in values]
    return "".join(f"{value:02X}" for value in values)


# Each row is three bits wide; a complete score fits a small 16x8 panel.
DIGITS = {
    "0": (7, 5, 5, 5, 7),
    "1": (2, 6, 2, 2, 7),
    "2": (7, 1, 7, 4, 7),
    "3": (7, 1, 7, 1, 7),
    "4": (5, 5, 7, 1, 1),
    "5": (7, 4, 7, 1, 7),
    "6": (7, 4, 7, 5, 7),
    "7": (7, 1, 1, 1, 1),
    "8": (7, 5, 7, 5, 7),
    "9": (7, 5, 7, 1, 7),
    "-": (0, 0, 7, 0, 0),
}


def matrix_pixels(matrix: Matrix, value: int | None, appearance: Appearance) -> list[str]:
    width, height = matrix.width, matrix.height
    logical_w, logical_h = (height, width) if matrix.rotation in {90, 270} else (width, height)
    text = str(value) if isinstance(value, int) and 0 <= value <= 9999 else "---"
    if len(text) * 4 - 1 > logical_w:
        text = "---"
    pixels = ["000000"] * (width * height)
    offset_x, offset_y = (logical_w - (len(text) * 4 - 1)) // 2, (logical_h - 5) // 2
    color = rgb(appearance, scale=True)
    for digit, character in enumerate(text):
        for row, bits in enumerate(DIGITS[character]):
            for column in range(3):
                if not bits & (4 >> column):
                    continue
                x, y = offset_x + digit * 4 + column, offset_y + row
                if matrix.rotation == 90:
                    x, y = width - 1 - y, x
                elif matrix.rotation == 180:
                    x, y = width - 1 - x, height - 1 - y
                elif matrix.rotation == 270:
                    x, y = y, height - 1 - x
                if matrix.serpentine and y % 2:
                    x = width - 1 - x
                pixels[y * width + x] = color
    return pixels


def payload(device: Device, view: dict, overlays: dict, preview: Preview | None = None) -> dict:
    """Compose once per segment so independent pixel targets share one frozen frame."""
    segments: dict[int, dict] = {}
    for target in device.targets:
        if not target.enabled:
            continue
        overlay = overlays.get(target.id)
        appearance = overlay or getattr(target.phases, view["phase"])
        segment = segments.setdefault(target.segment, {"id": target.segment, "on": True})
        if target.mode == "segment":
            segment.update(
                frz=False,
                fx=appearance.effect,
                bri=appearance.brightness,
                col=[list(bytes.fromhex(rgb(appearance)))],
            )
        else:
            segment.update(bri=255)
            indices = segment.setdefault("i", [])
            if target.mode == "matrix":
                appearance = overlay or target.matrix.appearance
                value = (
                    preview.value
                    if preview and preview.target_id == target.id
                    else view.get(target.matrix.source)
                )
                indices.extend([target.start, *matrix_pixels(target.matrix, value, appearance)])
            else:
                indices.extend([target.start, target.stop, rgb(appearance, scale=True)])
    return {"seg": list(segments.values()), "tt": 0}


def effect_name(event: Event) -> str | None:
    if not (event.data.get("player") or {}).get("is_local") or event.snapshot:
        return None
    if event.event in {"bust", "leg_win", "match_win"}:
        return event.event
    if (
        event.event == "turn_end"
        and event.data.get("score") == 180
        and not event.data.get("busted")
    ):
        return "score_180"
    if event.event == "throw":
        segment = event.data.get("dart", {}).get("segment", {})
        if segment.get("number") == 25 and segment.get("multiplier") == 2:
            return "bull"
        if segment.get("multiplier") == 3:
            return "triple"
    return None


async def request(http: httpx.AsyncClient, device: Device, path: str, body=None):
    try:
        async with asyncio.timeout(3):
            response = await http.request(
                "GET" if body is None else "POST",
                device.url + path,
                **({"json": body} if body is not None else {}),
                timeout=2,
            )
        if not response.is_success:
            raise ConnectionProblem(f"WLED returned HTTP {response.status_code}.")
        data = response.json()
        if not isinstance(data, dict):
            raise ConnectionProblem("WLED returned an invalid response.")
        if data.get("error") or data.get("success") is False:
            raise ConnectionProblem("WLED rejected the command.")
        return data
    except (httpx.HTTPError, TimeoutError) as exc:
        raise ConnectionProblem("Cannot reach WLED. Check its address and power.") from exc
    except ValueError as exc:
        raise ConnectionProblem("WLED returned invalid JSON.") from exc


async def inspect_device(http: httpx.AsyncClient, device: Device) -> dict:
    data = await request(http, device, "/json")
    try:
        info, state = data["info"], data["state"]
        segments = [
            {
                "id": s["id"],
                "name": s.get("n") or f"Segment {s['id']}",
                "length": s.get("len", s["stop"] - s["start"]),
                "start": s["start"],
                "stop": s["stop"],
            }
            for s in state["seg"]
            if s["stop"] > s["start"]
        ]
        if not isinstance(info["ver"], str) or not segments:
            raise ValueError
        for segment in segments:
            if any(
                type(segment[key]) is not int or segment[key] < 0
                for key in ("id", "start", "stop", "length")
            ):
                raise ValueError
        effects = data.get("effects", ["Solid"])
        if not isinstance(effects, list) or not all(isinstance(item, str) for item in effects):
            raise ValueError
        return {
            "name": str(info.get("name", device.name)),
            "version": info["ver"],
            "on": state.get("on"),
            "brightness": state.get("bri"),
            "segments": segments,
            "effects": effects,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ConnectionProblem("The device did not return valid WLED capabilities.") from exc


def validate_capabilities(device: Device, info: dict) -> None:
    segments = {item["id"]: item for item in info["segments"]}
    selected = []
    for target in device.targets:
        if not target.enabled:
            continue
        segment = segments.get(target.segment)
        if segment is None:
            raise ConnectionProblem(f"{target.name}: segment {target.segment} does not exist.")
        if target.mode != "segment" and target.stop > segment["length"]:
            raise ConnectionProblem(f"{target.name}: pixels exceed the segment length.")
        appearances = [getattr(target.phases, phase) for phase in Phase.__args__]
        for appearance in [*appearances, *target.effects.values()]:
            if appearance.effect >= len(info["effects"]) or info["effects"][appearance.effect] in {
                "RSVD",
                "-",
            }:
                raise ConnectionProblem(
                    f"{target.name}: native effect is unavailable on this device."
                )
        for other in selected:
            if other["id"] != segment["id"] and max(other["start"], segment["start"]) < min(
                other["stop"], segment["stop"]
            ):
                raise ConnectionProblem("Selected WLED segments overlap. Use separate segments.")
        selected.append(segment)


class DeviceWorker:
    def __init__(self, device: Device, http: httpx.AsyncClient, bus: EventBus, view: Callable):
        self.device, self.http, self.bus, self.view = device, http, bus, view
        self.lock = asyncio.Lock()
        self.info: dict | None = None
        self.error: str | None = None
        self.connected = False
        self.last_sent: str | None = None
        self.sent = 0
        self.overlays: dict[str, tuple[float, int, Effect]] = {}
        self.preview: tuple[float, Preview] | None = None
        self.last_payload: dict | None = None

    async def probe(self) -> dict:
        self.info = await inspect_device(self.http, self.device)
        validate_capabilities(self.device, self.info)
        return self.info

    def accept(self, event: Event) -> None:
        if event.event in {"throw_corrected", "throw_removed", "match_started"}:
            self.overlays.clear()
        name = effect_name(event)
        if name is None or (datetime.now(UTC) - event.received_at).total_seconds() > 2:
            return
        now = time.monotonic()
        for target in self.device.targets:
            effect = target.effects.get(name)
            old = self.overlays.get(target.id)
            if target.enabled and effect and (not old or old[0] <= now or PRIORITY[name] >= old[1]):
                self.overlays[target.id] = (now + effect.duration, PRIORITY[name], effect)

    def desired(self, view: dict) -> dict:
        now = time.monotonic()
        self.overlays = {key: item for key, item in self.overlays.items() if item[0] > now}
        if not view["available"]:
            self.overlays.clear()
        if self.preview and self.preview[0] <= now:
            self.preview = None
        overlays = {key: item[2] for key, item in self.overlays.items()}
        if self.preview:
            test = self.preview[1]
            target = next(t for t in self.device.targets if t.id == test.target_id)
            overlays[target.id] = getattr(target.phases, test.phase)
        return payload(self.device, view, overlays, self.preview[1] if self.preview else None)

    async def send(self, body: dict) -> None:
        if body["seg"]:
            await request(self.http, self.device, "/json/state", body)
            self.sent += 1
            self.last_sent = datetime.now(UTC).isoformat()
        self.last_payload = body
        self.error, self.connected = None, True

    async def run(self) -> None:
        next_probe, next_retry = 0.0, 0.0
        with self.bus.subscribe(capacity=64) as queue:
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.25)
                        self.accept(event)
                        while not queue.empty():
                            self.accept(queue.get_nowait())
                    except TimeoutError:
                        pass
                    if time.monotonic() < next_retry:
                        continue
                    async with self.lock:
                        try:
                            if time.monotonic() >= next_probe:
                                await self.probe()
                                next_probe = time.monotonic() + 15
                                self.last_payload = None  # Recover after controller power cycles.
                            body = self.desired(self.view())
                            if body != self.last_payload:
                                await self.send(body)
                        except ConnectionProblem as exc:
                            self.error, self.connected = str(exc), False
                            self.overlays.clear()
                            self.preview = None
                            next_probe = 0
                            next_retry = time.monotonic() + 5
            finally:
                # Clear ready indications on shutdown or configuration changes when reachable.
                if self.info and self.last_payload:
                    try:
                        await self.send(payload(self.device, {"phase": "waiting"}, {}))
                    except ConnectionProblem:
                        pass


class WLED:
    def __init__(self, path: Path, http: httpx.AsyncClient, bus: EventBus, view: Callable):
        self.path, self.http, self.bus, self.view = path, http, bus, view
        self.config = WLEDConfig()
        self.error: str | None = None
        self.workers: dict[str, DeviceWorker] = {}
        self.tasks: list[asyncio.Task] = []
        try:
            if path.exists():
                self.config = WLEDConfig.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError):
            self.error = "Cannot load wled.json. Fix or replace the configuration through the API."

    def start(self) -> None:
        self.workers = {
            device.id: DeviceWorker(device, self.http, self.bus, self.view)
            for device in self.config.devices
        }
        self.tasks = [
            asyncio.create_task(worker.run(), name=f"wled-{key}")
            for key, worker in self.workers.items()
            if self.config.enabled
            and worker.device.enabled
            and any(t.enabled for t in worker.device.targets)
        ]

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def configure(self, config: WLEDConfig) -> None:
        write_private_json(self.path, config.model_dump(mode="json"))
        await self.close()
        self.config, self.error = config, None
        self.start()

    def worker(self, device_id: str) -> DeviceWorker:
        if device_id not in self.workers:
            raise ConnectionProblem("Unknown WLED device. Save it first.")
        return self.workers[device_id]

    async def probe(self, device_id: str) -> dict:
        worker = self.worker(device_id)
        async with worker.lock:
            try:
                result = await worker.probe()
                worker.connected, worker.error = True, None
                return result
            except ConnectionProblem as exc:
                worker.connected, worker.error = False, str(exc)
                raise

    async def test(self, device_id: str, preview: Preview) -> dict:
        worker = self.worker(device_id)
        if not self.config.enabled or not worker.device.enabled:
            raise ConnectionProblem("Enable WLED and this device before testing a target.")
        if not any(t.id == preview.target_id and t.enabled for t in worker.device.targets):
            raise ConnectionProblem("Unknown or disabled WLED target.")
        async with worker.lock:
            try:
                await worker.probe()
                worker.preview = (time.monotonic() + preview.duration, preview)
                await worker.send(worker.desired(self.view()))
            except ConnectionProblem as exc:
                worker.preview = None
                worker.error, worker.connected = str(exc), False
                raise
        return {"applied": True, "duration": preview.duration}

    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "error": self.error,
            "game": self.view(),
            "devices": [
                {
                    "id": key,
                    "name": worker.device.name,
                    "enabled": worker.device.enabled,
                    "connected": worker.connected,
                    "error": worker.error,
                    "last_sent": worker.last_sent,
                    "commands_sent": worker.sent,
                    "info": worker.info,
                }
                for key, worker in self.workers.items()
            ],
        }
