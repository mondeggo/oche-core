"""Headless WLED targets, phase colours and numeric matrix displays over the JSON API."""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from zeroconf import Error as ZeroconfError
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from ochecore.autodarts.errors import ConnectionProblem
from ochecore.events import Event, EventBus
from ochecore.storage import write_private_json

Phase = Literal["idle", "ready", "takeout", "waiting"]
EffectName = Literal[
    "single",
    "double",
    "triple",
    "outer_bull",
    "bull",
    "miss",
    "throw",
    "score_180",
    "bust",
    "leg_win",
    "match_win",
    "match_started",
    "match_ended",
    "turn_started",
    "takeout_started",
    "takeout_finished",
    "manual_reset",
    "calibration_started",
    "calibration_finished",
]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")]
Colour = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
PRIORITY = {name: 1 for name in EffectName.__args__} | {
    "bull": 2,
    "score_180": 3,
    "bust": 4,
    "leg_win": 5,
    "match_win": 6,
}
PlayerSlot = Annotated[int, Field(ge=1, le=10)]
DISCOVERY_SECONDS = 3
WLED_SERVICE = "_wled._tcp.local."


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Appearance(Model):
    color: Colour = "#ffffff"
    brightness: int = Field(default=128, ge=0, le=255, strict=True)
    effect: int = Field(default=0, ge=0, le=255, strict=True)


class Effect(Appearance):
    duration: float = Field(default=2, ge=0.1, le=30)


class PlayerAppearance(Appearance):
    name_filter: str = Field(default="", max_length=80)


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
    players: dict[PlayerSlot, PlayerAppearance] = Field(default_factory=dict)
    matrix: Matrix = Field(default_factory=Matrix)

    @property
    def stop(self) -> int:
        count = self.matrix.width * self.matrix.height if self.mode == "matrix" else self.count
        return self.start + count

    @model_validator(mode="after")
    def valid_effects(self):
        appearances = [*self.effects.values(), *self.players.values(), self.matrix.appearance]
        appearances += [getattr(self.phases, key) for key in Phase.__args__]
        if self.mode != "segment" and any(item.effect for item in appearances):
            raise ValueError("Native WLED effects require a whole-segment target")
        return self


class DeviceAddress(Model):
    url: str

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


class Device(DeviceAddress):
    id: Identifier
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    targets: list[Target] = Field(default_factory=list, max_length=16)

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


class TargetStyle(Model):
    phases: PhaseColours = Field(default_factory=PhaseColours)
    effects: dict[EffectName, Effect] = Field(default_factory=default_effects)
    players: dict[PlayerSlot, PlayerAppearance] = Field(default_factory=dict)
    matrix: Appearance = Field(default_factory=Appearance)


class LightingProfile(Model):
    id: Identifier
    name: str = Field(min_length=1, max_length=60)
    targets: dict[Identifier, dict[Identifier, TargetStyle]] = Field(default_factory=dict)


class ProfileName(Model):
    name: str = Field(min_length=1, max_length=60)


class ProfileSelection(Model):
    id: Identifier


class WLEDConfig(Model):
    enabled: bool = False
    devices: list[Device] = Field(default_factory=list, max_length=8)
    active_profile: Identifier = "default"
    profiles: list[LightingProfile] = Field(
        default_factory=lambda: [LightingProfile(id="default", name="Default")],
        min_length=1,
        max_length=12,
    )

    @model_validator(mode="after")
    def unique_devices(self):
        if len({d.id for d in self.devices}) != len(self.devices):
            raise ValueError("Device IDs must be unique")
        if len({d.url.lower() for d in self.devices}) != len(self.devices):
            raise ValueError("Configure each WLED URL once")
        if len({p.id for p in self.profiles}) != len(self.profiles):
            raise ValueError("Profile IDs must be unique")
        if len({p.name.casefold() for p in self.profiles}) != len(self.profiles):
            raise ValueError("Profile names must be unique")
        if self.active_profile not in {p.id for p in self.profiles}:
            raise ValueError("The active lighting profile must exist")
        return self


class Preview(Model):
    target_id: Identifier
    phase: Phase = "ready"
    event: EffectName | None = None
    player: PlayerSlot | None = None
    value: int | None = Field(default=None, ge=0, le=9999, strict=True)
    duration: float = Field(default=3, ge=0.5, le=10)


class DraftPreview(Preview):
    device: Device


class Power(Model):
    on: bool = Field(strict=True)


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


def player_appearance(target: Target, view: dict) -> Appearance | None:
    """Player colours apply only while the local board is confirmed ready."""
    player = view.get("player") or {}
    if view["phase"] != "ready" or not player.get("is_local"):
        return None
    for slot, style in sorted(target.players.items()):
        if (
            style.name_filter.casefold() in player.get("name", "").casefold()
            if style.name_filter
            else player.get("index") == slot - 1
        ):
            return style
    return None


def payload(device: Device, view: dict, overlays: dict, preview: Preview | None = None) -> dict:
    """Compose once per segment so independent pixel targets share one frozen frame."""
    segments: dict[int, dict] = {}
    for target in device.targets:
        if not target.enabled:
            continue
        overlay = overlays.get(target.id)
        appearance = (
            overlay or player_appearance(target, view) or getattr(target.phases, view["phase"])
        )
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
    if event.kind != "normalized" or event.snapshot:
        return None
    if event.event in {
        "match_started",
        "match_ended",
        "manual_reset",
        "calibration_started",
        "calibration_finished",
    }:
        return event.event
    if not (event.data.get("player") or {}).get("is_local"):
        return None
    if event.event in {
        "bust",
        "leg_win",
        "match_win",
        "turn_started",
        "takeout_started",
        "takeout_finished",
    }:
        return event.event
    if (
        event.event == "turn_end"
        and event.data.get("score") == 180
        and not event.data.get("busted")
    ):
        return "score_180"
    if event.event == "throw":
        segment = event.data.get("dart", {}).get("segment", {})
        number, multiplier = segment.get("number"), segment.get("multiplier")
        if number == 0 or multiplier == 0:
            return "miss"
        if number == 25:
            return "bull" if multiplier == 2 else "outer_bull"
        return {1: "single", 2: "double", 3: "triple"}.get(multiplier, "throw")
    return None


async def request(
    http: httpx.AsyncClient, device: DeviceAddress, path: str, body=None, *, array: bool = False
):
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
        if not isinstance(data, list if array else dict):
            raise ConnectionProblem("WLED returned an invalid response.")
        if not array and (data.get("error") or data.get("success") is False):
            raise ConnectionProblem("WLED rejected the command.")
        return data
    except (httpx.HTTPError, TimeoutError) as exc:
        raise ConnectionProblem("Cannot reach WLED. Check its address and power.") from exc
    except ValueError as exc:
        raise ConnectionProblem("WLED returned invalid JSON.") from exc


async def inspect_device(
    http: httpx.AsyncClient,
    device: DeviceAddress,
    *,
    metadata: bool = False,
    data: dict | None = None,
) -> dict:
    data = await request(http, device, "/json") if data is None else data
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
        result = {
            "name": str(info.get("name", "WLED")),
            "version": info["ver"],
            "on": state.get("on"),
            "brightness": state.get("bri"),
            "segments": segments,
            "effects": effects,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ConnectionProblem("The device did not return valid WLED capabilities.") from exc
    if metadata:
        try:
            definitions = await request(http, device, "/json/fxdata", array=True)
        except ConnectionProblem:
            definitions = []  # Older firmware can still be probed and used.
        result["effect_colors"] = [effect_colors(value) for value in definitions]
    return result


def effect_colors(metadata: object) -> list[str | None]:
    """Read WLED's color-slot labels; missing metadata keeps the default controls."""
    sections = metadata.split(";") if isinstance(metadata, str) else []
    if len(sections) < 2:
        return ["Colour", "Background", "Accent"]
    labels = sections[1].split(",")
    return [
        (default if labels[index] == "!" else labels[index]) or None
        if index < len(labels)
        else None
        for index, default in enumerate(("Colour", "Background", "Accent"))
    ]


async def discover_devices(http: httpx.AsyncClient) -> list[dict]:
    """Browse WLED's IPv4 advertisements and verify candidates without changing lights."""
    tasks: dict[str, asyncio.Task] = {}
    active: set[str] = set()
    found: dict[str, dict] = {}
    slots = asyncio.Semaphore(4)

    async def resolve(zeroconf, service_type: str, name: str) -> None:
        async with slots:
            try:
                info = AsyncServiceInfo(service_type, name)
                if not await info.async_request(zeroconf, 1000) or not info.port:
                    return
                for address in info.parsed_addresses(IPVersion.V4Only)[:2]:
                    ip = IPv4Address(address)
                    if ip.is_loopback or ip.is_multicast or ip.is_unspecified:
                        continue
                    url = f"http://{ip}" + (f":{info.port}" if info.port != 80 else "")
                    device = Device(id="discovered", name="WLED", url=url)
                    try:
                        details = await inspect_device(http, device)
                    except ConnectionProblem:
                        continue
                    found[name] = {
                        "name": details["name"].strip()[:80] or "WLED",
                        "address": str(ip),
                        "url": url,
                        "hostname": (info.server or "").rstrip("."),
                        "version": details["version"],
                    }
                    return
            except (OSError, ValueError, ZeroconfError):
                return  # An invalid or disappearing advertisement does not stop other devices.

    def changed(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Removed:
            active.discard(name)
            return
        if name not in tasks and len(tasks) < 32:
            tasks[name] = asyncio.create_task(resolve(zeroconf, service_type, name))
        if name in tasks:
            active.add(name)

    try:
        async with AsyncZeroconf(ip_version=IPVersion.V4Only) as zeroconf:
            browser = None
            try:
                browser = AsyncServiceBrowser(zeroconf.zeroconf, WLED_SERVICE, handlers=[changed])
                await asyncio.sleep(DISCOVERY_SECONDS)
                await browser.async_cancel()
                browser = None
                if tasks:
                    await asyncio.wait(tasks.values(), timeout=DISCOVERY_SECONDS)
            finally:
                if browser is not None:
                    await browser.async_cancel()
                for task in tasks.values():
                    task.cancel()
                await asyncio.gather(*tasks.values(), return_exceptions=True)
    except (OSError, ZeroconfError) as exc:
        raise ConnectionProblem(
            "Cannot start local device discovery. Check network access or add an address manually."
        ) from exc
    unique = {device["url"]: device for name, device in found.items() if name in active}
    return sorted(unique.values(), key=lambda device: (device["name"].casefold(), device["url"]))


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
        for appearance in [*appearances, *target.effects.values(), *target.players.values()]:
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
        self.power_off = False
        self.reset_on_close = True
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
        if event.kind != "normalized" or event.snapshot:
            return
        if event.event in {
            "throw_corrected",
            "throw_removed",
            "match_started",
            "turn_started",
            "manual_reset",
            "calibration_started",
        }:
            self.overlays.clear()
        name = effect_name(event)
        if name is None or (datetime.now(UTC) - event.received_at).total_seconds() > 2:
            return
        now = time.monotonic()
        for target in self.device.targets:
            effect = target.effects.get(name)
            priority = PRIORITY[name]
            if effect is None and event.event == "throw":
                effect, priority = target.effects.get("throw"), PRIORITY["throw"]
            old = self.overlays.get(target.id)
            if target.enabled and effect and (not old or old[0] <= now or priority >= old[1]):
                self.overlays[target.id] = (now + effect.duration, priority, effect)

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
            overlays[target.id] = preview_appearance(target, test)
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
                            if self.power_off or (self.info and self.info.get("on") is False):
                                self.connected, self.error = True, None
                                continue
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
                if (
                    self.reset_on_close
                    and not self.power_off
                    and self.info
                    and self.info.get("on") is not False
                    and self.last_payload
                ):
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
        self.discovery_lock = asyncio.Lock()
        try:
            if path.exists():
                self.config = WLEDConfig.model_validate_json(path.read_text(encoding="utf-8"))
            self.sync_profiles(self.config)
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
        config = config.model_copy(deep=True)
        self.sync_profiles(config)
        write_private_json(self.path, config.model_dump(mode="json"))
        active_devices = {
            d.url.lower(): d
            for d in config.devices
            if config.enabled and d.enabled and any(t.enabled for t in d.targets)
        }
        powered_off = {w.device.url.lower() for w in self.workers.values() if w.power_off}
        for worker in self.workers.values():
            updated = active_devices.get(worker.device.url.lower())
            old_targets = {
                (t.id, t.segment, t.mode, t.start, t.stop)
                for t in worker.device.targets
                if t.enabled
            }
            new_targets = (
                {(t.id, t.segment, t.mode, t.start, t.stop) for t in updated.targets if t.enabled}
                if updated
                else set()
            )
            worker.reset_on_close = not old_targets.issubset(new_targets)
        await self.close()
        self.config, self.error = config, None
        self.start()
        for worker in self.workers.values():
            worker.power_off = worker.device.url.lower() in powered_off

    @staticmethod
    def sync_profiles(config: WLEDConfig) -> None:
        """Store active styles and share only device/target geometry between profiles."""
        for profile in config.profiles:
            updated = {}
            for device in config.devices:
                styles = {}
                for target in device.targets:
                    current = TargetStyle(
                        phases=target.phases,
                        effects=target.effects,
                        players=target.players,
                        matrix=target.matrix.appearance,
                    ).model_copy(deep=True)
                    style = (
                        current
                        if profile.id == config.active_profile
                        else profile.targets.get(device.id, {})
                        .get(target.id, current)
                        .model_copy(deep=True)
                    )
                    if target.mode != "segment":
                        for appearance in [
                            *style.effects.values(),
                            *style.players.values(),
                            style.matrix,
                            *(getattr(style.phases, p) for p in Phase.__args__),
                        ]:
                            appearance.effect = 0
                    styles[target.id] = style
                updated[device.id] = styles
            profile.targets = updated

    @staticmethod
    def apply_profile(config: WLEDConfig, profile: LightingProfile) -> None:
        for device in config.devices:
            for target in device.targets:
                style = profile.targets[device.id][target.id]
                target.phases = style.phases.model_copy(deep=True)
                target.players = {
                    key: value.model_copy(deep=True) for key, value in style.players.items()
                }
                target.effects = {
                    key: effect.model_copy(deep=True) for key, effect in style.effects.items()
                }
                target.matrix.appearance = style.matrix.model_copy(deep=True)
        config.active_profile = profile.id

    async def create_profile(self, name: str) -> dict:
        config = self.config.model_copy(deep=True)
        if any(p.name.casefold() == name.casefold() for p in config.profiles):
            raise ConnectionProblem("A lighting profile with that name already exists.")
        if len(config.profiles) >= 12:
            raise ConnectionProblem("Keep at most 12 lighting profiles.")
        active = next(p for p in config.profiles if p.id == config.active_profile)
        profile = active.model_copy(update={"id": uuid4().hex[:12], "name": name}, deep=True)
        config.profiles.append(profile)
        config.active_profile = profile.id
        await self.configure(config)
        return {"active_profile": profile.id}

    async def select_profile(self, profile_id: str) -> dict:
        config = self.config.model_copy(deep=True)
        profile = next((p for p in config.profiles if p.id == profile_id), None)
        if profile is None:
            raise ConnectionProblem("Unknown lighting profile.")
        self.apply_profile(config, profile)
        await self.configure(config)
        return {"active_profile": profile.id}

    async def delete_profile(self, profile_id: str) -> dict:
        config = self.config.model_copy(deep=True)
        if not any(p.id == profile_id for p in config.profiles):
            raise ConnectionProblem("Unknown lighting profile.")
        if len(config.profiles) == 1:
            raise ConnectionProblem("Keep at least one lighting profile.")
        config.profiles = [p for p in config.profiles if p.id != profile_id]
        if config.active_profile == profile_id:
            self.apply_profile(config, config.profiles[0])
        await self.configure(config)
        return {"active_profile": config.active_profile}

    async def discover(self) -> dict:
        if self.discovery_lock.locked():
            raise ConnectionProblem("WLED discovery is already running. Try again shortly.")
        async with self.discovery_lock:
            return {"devices": await discover_devices(self.http)}

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

    async def probe_address(self, address: DeviceAddress) -> dict:
        return await inspect_device(self.http, address, metadata=True)

    async def power(self, on: bool, device_id: str | None = None) -> dict:
        workers = [self.worker(device_id)] if device_id else list(self.workers.values())
        if not workers:
            raise ConnectionProblem("No WLED devices configured.")
        results = []
        for worker in workers:
            async with worker.lock:
                try:
                    await request(self.http, worker.device, "/json/state", {"on": on})
                    if worker.info:
                        worker.info["on"] = on
                    worker.power_off = not on
                    worker.connected, worker.error = True, None
                    worker.overlays.clear()
                    worker.preview = None
                    worker.last_payload = None
                    results.append({"id": worker.device.id, "on": on, "applied": True})
                except ConnectionProblem as exc:
                    worker.connected, worker.error = False, str(exc)
                    results.append({"id": worker.device.id, "applied": False, "error": str(exc)})
        return {"applied": all(item["applied"] for item in results), "devices": results}

    async def preview_draft(self, draft: DraftPreview) -> dict:
        """Preview one unsaved target, then restore settings without persisting the draft."""
        target = next((t for t in draft.device.targets if t.id == draft.target_id), None)
        if target is None:
            raise ConnectionProblem("Unknown WLED target.")
        appearance = preview_appearance(target, draft)
        device = draft.device.model_copy(
            update={"targets": [target.model_copy(update={"enabled": True})]}
        )
        worker = next(
            (w for w in self.workers.values() if w.device.url.lower() == device.url.lower()), None
        )
        async with worker.lock if worker else asyncio.Lock():
            snapshot = await request(self.http, device, "/json")
            info = await inspect_device(self.http, device, data=snapshot)
            validate_capabilities(device, info)
            state = snapshot["state"]
            segment = next(s for s in state["seg"] if s["id"] == target.segment)
            managed = bool(worker and self.config.enabled and worker.device.enabled)
            if segment.get("frz") and not (
                managed
                and worker.last_payload
                and any(
                    t.enabled
                    and (t.segment, t.mode, t.start, t.stop)
                    == (target.segment, target.mode, target.start, target.stop)
                    for t in worker.device.targets
                )
            ):
                raise ConnectionProblem(
                    "This segment has frozen pixels that WLED cannot return for restoration. "
                    "Choose a native effect in WLED before previewing a new target."
                )
            keys = {"id", "on", "bri", "col", "fx", "sx", "ix", "pal", "frz"}
            restore_segment = {key: value for key, value in segment.items() if key in keys}
            restore_segment.setdefault("frz", False)
            restore = {"seg": [restore_segment], "tt": 0}
            restore.update({key: state[key] for key in ("on", "bri") if key in state})
            try:
                # WLED requires power and brightness before individual pixel commands.
                if state.get("on") is False or state.get("bri") == 0:
                    await request(
                        self.http,
                        device,
                        "/json/state",
                        {"on": True, "bri": state.get("bri") or 128, "tt": 0},
                    )
                await request(
                    self.http,
                    device,
                    "/json/state",
                    payload(device, {"phase": draft.phase}, {target.id: appearance}, draft),
                )
                await asyncio.sleep(draft.duration)
            finally:
                try:
                    await request(self.http, device, "/json/state", restore)
                    if managed and state.get("on") is not False:
                        worker.preview = None
                        await worker.send(worker.desired(self.view()))
                except ConnectionProblem as exc:
                    if worker:
                        worker.connected, worker.error = False, str(exc)
                    raise ConnectionProblem(
                        "Preview ended, but WLED could not be restored. Check its connection."
                    ) from exc
        return {"applied": True, "restored": True, "duration": draft.duration}

    async def test(self, device_id: str, preview: Preview) -> dict:
        worker = self.worker(device_id)
        if not self.config.enabled or not worker.device.enabled:
            raise ConnectionProblem("Enable WLED and this device before testing a target.")
        if not any(t.id == preview.target_id and t.enabled for t in worker.device.targets):
            raise ConnectionProblem("Unknown or disabled WLED target.")
        preview_appearance(
            next(t for t in worker.device.targets if t.id == preview.target_id), preview
        )
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
            "active_profile": self.config.active_profile,
            "error": self.error,
            "game": self.view(),
            "devices": [
                {
                    "id": key,
                    "name": worker.device.name,
                    "url": worker.device.url,
                    "enabled": worker.device.enabled,
                    "on": False
                    if worker.power_off
                    else worker.info.get("on")
                    if worker.info
                    else None,
                    "connected": worker.connected,
                    "error": worker.error,
                    "last_sent": worker.last_sent,
                    "commands_sent": worker.sent,
                    "info": worker.info,
                }
                for key, worker in self.workers.items()
            ],
        }


def preview_appearance(target: Target, preview: Preview) -> Appearance:
    if preview.player is not None:
        style = target.players.get(preview.player)
        if style is None:
            raise ConnectionProblem("Enable this player colour before previewing it.")
        return style
    if preview.event:
        effect = target.effects.get(preview.event)
        if effect is None:
            raise ConnectionProblem("Enable this game effect in the draft before previewing it.")
        return effect
    return (
        target.matrix.appearance
        if target.mode == "matrix"
        else getattr(target.phases, preview.phase)
    )
