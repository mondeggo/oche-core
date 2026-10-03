import asyncio
import json
import os
import re
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError, field_validator

from ochecore.autodarts.errors import ConnectionProblem


class Event(BaseModel):
    """Versioned transport envelope; snapshots must not be treated as new throws."""

    schema_version: int = 1
    id: str = Field(default_factory=lambda: str(uuid4()))
    sequence: int
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str
    event: str
    kind: Literal["raw", "normalized"] = "raw"
    channel: str | None = None
    topic: str | None = None
    snapshot: bool = False
    data: dict[str, Any]


class CloudMessage(BaseModel):
    channel: str
    topic: str
    data: dict[str, Any]


def parse_cloud_message(raw: str | bytes) -> CloudMessage | None:
    """Validate the cloud transport envelope before interpreting its contents."""
    try:
        return CloudMessage.model_validate_json(raw)
    except ValidationError:
        return None


def event_name(message: CloudMessage, board_id: str, match_id: str | None) -> str | None:
    if message.channel == "autodarts.boards":
        if message.topic == f"{board_id}.matches":
            return "board.matches"
        if message.topic == f"{board_id}.events":
            return "board.events"
        if message.topic == f"{board_id}.state":
            return "board.state"
    if message.channel == "autodarts.matches" and match_id and message.topic == f"{match_id}.state":
        return "match.state"
    if (
        message.channel == "autodarts.matches"
        and match_id
        and message.topic == f"{match_id}.events"
    ):
        return "match.events"
    return None


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if key.lower().replace("_", "")
            in {"accesstoken", "refreshtoken", "idtoken", "devicecode", "password", "authorization"}
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class DebugRecording(BaseModel):
    enabled: bool


class RawEventRecorder:
    """Opt-in JSONL capture independent of the bounded event history and subscribers."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.enabled = False
        self.path: Path | None = None
        self.recorded: int | None = 0
        self.bytes = 0
        self.pending_bytes = 0
        self.error: str | None = None
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=512)
        self.task: asyncio.Task | None = None
        try:
            files = sorted(directory.glob("autodarts-*.jsonl"))
            if files:
                self.path = files[-1]
                self.bytes = self.path.stat().st_size
                self.recorded = None  # Do not scan old captures just to count their lines.
        except OSError:
            self.error = "Cannot read the debug directory. Check its permissions."

    def status(self) -> dict:
        writing = self.task is not None and not self.task.done()
        return {
            "enabled": self.enabled,
            "file": self.path.name if self.path else None,
            "path": str(self.path.resolve()) if self.path else None,
            "recorded": self.recorded,
            "bytes": self.bytes,
            "error": self.error,
            "pending": self.queue.qsize(),
            "download_url": "/api/events/debug/file"
            if self.path and not self.enabled and not writing
            else None,
        }

    async def configure(self, enabled: bool) -> dict:
        if enabled and not self.enabled:
            await self.stop()
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            path = self.directory / f"autodarts-{timestamp}-{uuid4().hex[:8]}.jsonl"
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                # Exclusive creation and owner-only permissions on POSIX.
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except OSError as exc:
                self.error = "Cannot create a debug file. Check free disk space and permissions."
                raise ConnectionProblem(self.error) from exc
            self.path = path
            self.recorded, self.bytes, self.pending_bytes = 0, 0, 0
            self.error = None
            self.enabled = True
            self.task = asyncio.create_task(self._write(), name="raw-event-recording")
        elif not enabled:
            await self.stop()
        return self.status()

    def record(self, value: Any, sequence: int) -> None:
        if not self.enabled:
            return
        entry = {
            "schema_version": 1,
            "sequence": sequence,
            "received_at": datetime.now(UTC).isoformat(),
            "raw": value,
        }
        line = (json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        if self.queue.full() or self.pending_bytes + len(line) > 8 * 1024 * 1024:
            self.enabled = False
            self.error = (
                "Recording stopped: disk writing could not keep up. This capture is incomplete."
            )
            if not self.queue.full():
                self.queue.put_nowait(None)
            return
        self.pending_bytes += len(line)
        self.queue.put_nowait(line)

    def _append(self, batch: list[bytes]) -> None:
        with self.path.open("ab") as stream:
            stream.writelines(batch)

    async def _write(self) -> None:
        try:
            while self.enabled or not self.queue.empty():
                first = await self.queue.get()
                batch = [first] if first is not None else []
                while len(batch) < 100 and not self.queue.empty():
                    item = self.queue.get_nowait()
                    if item is not None:
                        batch.append(item)
                if batch:
                    await asyncio.to_thread(self._append, batch)
                    size = sum(map(len, batch))
                    self.pending_bytes -= size
                    self.bytes += size
                    self.recorded += len(batch)
        except OSError:
            self.enabled = False
            self.error = (
                "Recording stopped: cannot write the debug file. This capture is incomplete."
            )
        finally:
            while not self.queue.empty():
                self.queue.get_nowait()
            self.pending_bytes = 0

    async def stop(self) -> None:
        self.enabled = False
        if self.task and not self.task.done():
            if not self.queue.full():
                self.queue.put_nowait(None)
            await self.task

    def download_path(self) -> Path:
        if self.enabled or (self.task and not self.task.done()):
            raise ConnectionProblem("Stop recording before downloading the debug file.")
        path = self.path
        if (
            path is None
            or not re.fullmatch(r"autodarts-\d{8}T\d{12}Z-[a-f0-9]{8}\.jsonl", path.name)
            or path.resolve().parent != self.directory.resolve()
            or not path.is_file()
        ):
            raise ConnectionProblem("No debug file is available.")
        return path


class EventBus:
    def __init__(self, capacity: int = 100, recorder: RawEventRecorder | None = None):
        self.recorder = recorder
        self.normalized_history: deque[Event] = deque(maxlen=capacity)
        self.raw_history: deque[Any] = deque(maxlen=capacity)
        self.normalized_queues: set[asyncio.Queue[Event]] = set()
        self.raw_queues: set[asyncio.Queue[Any]] = set()
        self.sequence = 0
        self.dropped = 0
        self.raw_received = 0
        self.raw_dropped = 0

    @staticmethod
    def _deliver(value: Any, queues: set) -> int:
        dropped = 0
        for queue in queues:
            if queue.full():
                queue.get_nowait()
                dropped += 1
            queue.put_nowait(value)
        return dropped

    def record_raw(self, raw: str | bytes) -> Any:
        """Preserve incoming JSON structure, including unknown fields and control frames."""
        try:
            value = redact(json.loads(raw))
        except (ValueError, UnicodeDecodeError):
            value = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        self.raw_received += 1
        if self.recorder is not None:
            self.recorder.record(value, self.raw_received)
        self.raw_history.append(value)
        self.raw_dropped += self._deliver(value, self.raw_queues)
        return value

    def publish(self, source: str, event: str, data: dict, **kwargs: Any) -> Event:
        self.sequence += 1
        envelope = Event(
            sequence=self.sequence, source=source, event=event, data=redact(data), **kwargs
        )
        if envelope.kind == "normalized":
            self.normalized_history.append(envelope)
            self.dropped += self._deliver(envelope, self.normalized_queues)
        return envelope

    @contextmanager
    def subscribe(
        self, capacity: int = 100, mode: Literal["normalized", "raw"] = "normalized"
    ) -> Iterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=capacity)
        queues = {"normalized": self.normalized_queues, "raw": self.raw_queues}[mode]
        queues.add(queue)
        try:
            yield queue
        finally:
            queues.discard(queue)

    def clear(self) -> None:
        self.normalized_history.clear()
        self.raw_history.clear()


class Segment(BaseModel):
    number: int = Field(ge=0, le=25, strict=True)
    multiplier: int = Field(ge=0, le=3, strict=True)
    name: str
    bed: str | None = None


class Dart(BaseModel):
    id: str = Field(min_length=1)
    segment: Segment

    def public(self, position: int) -> dict:
        return {
            "id": self.id,
            "position": position,
            "segment": self.segment.model_dump(),
            "points": self.segment.number * self.segment.multiplier,
        }


class Turn(BaseModel):
    id: str = Field(min_length=1)
    player_id: str | None = Field(default=None, alias="playerId")
    player: int | None = Field(default=None, ge=0, strict=True)
    round: int | None = Field(default=None, ge=0, strict=True)
    points: int | None = Field(default=None, strict=True)
    score: int | None = Field(default=None, strict=True)
    busted: bool = False
    finished_at: str | None = Field(default=None, alias="finishedAt")
    throws: list[Dart] = Field(default_factory=list, max_length=3)

    @field_validator("finished_at")
    @classmethod
    def unfinished_timestamp(cls, value: str | None) -> str | None:
        # Live AutoDarts states use Go's zero timestamp for unfinished visits.
        return None if value and value.startswith("0001-01-01T00:00:00") else value


class Player(BaseModel):
    id: str = Field(min_length=1)
    name: str = ""
    board_id: str | None = Field(default=None, alias="boardId")
    cpu_ppr: float | None = Field(default=None, alias="cpuPPR")

    def public(self, index: int, board_id: str) -> dict:
        return {
            "id": self.id,
            "index": index,
            "name": self.name,
            "board_id": self.board_id,
            "is_local": self.board_id == board_id and self.cpu_ppr is None,
            "is_bot": self.cpu_ppr is not None,
        }


class MatchFrame(BaseModel):
    """Current AutoDarts state: turns are newest first, darts oldest first."""

    id: str = Field(min_length=1)
    variant: str
    players: list[Player] = Field(min_length=1)
    turns: list[Turn]
    set: int = Field(ge=0, strict=True)
    leg: int = Field(ge=0, strict=True)
    round: int = Field(ge=0, strict=True)
    player: int = Field(ge=0, strict=True)
    game_scores: list[int] = Field(default_factory=list, alias="gameScores")
    game_winner: int = Field(default=-1, alias="gameWinner", ge=-1, strict=True)
    winner: int = Field(default=-1, ge=-1, strict=True)
    settings: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    activated: int = -1


GameEventName = Literal[
    "match_started",
    "match_ended",
    "player_changed",
    "turn_started",
    "match_editing",
    "throw",
    "throw_corrected",
    "throw_removed",
    "turn_end",
    "bust",
    "checkout",
    "leg_win",
    "match_win",
    "takeout_started",
    "takeout_finished",
    "manual_reset",
    "calibration_started",
    "calibration_finished",
]


class EventNormalizer:
    """Interpret state transitions; keep raw envelopes available for diagnostics.

    A REST snapshot or the first valid state establishes a silent baseline. Deduplication
    is bounded and scoped to the selected match, not a persistent delivery guarantee.
    """

    def __init__(self, board_id: str, bus: EventBus):
        self.board_id = board_id
        self.bus = bus
        self.match_id: str | None = None
        self.frame: MatchFrame | None = None
        self.turns: OrderedDict[tuple, Turn] = OrderedDict()
        self.seen: OrderedDict[tuple, None] = OrderedDict()
        self.takeout_phase: str | None = None
        self.takeout_context: dict | None = None
        self.synchronized = False
        self.invalid_states = 0
        self.emitted = 0
        self.readiness: str | None = None
        self.calibrating = False
        self.board_event: str | None = None
        self.board_throws: int | None = None
        self.state_valid = False
        self.takeout_is_local = False
        self.editing = False

    def select(self, match_id: str | None) -> None:
        if match_id == self.match_id:
            return
        self.match_id, self.frame = match_id, None
        self.turns.clear()
        self.seen.clear()
        self.takeout_phase = None
        self.takeout_context = None
        self.synchronized = False
        self.readiness = None
        self.calibrating = False
        self.board_event = None
        self.board_throws = None
        self.state_valid = False
        self.takeout_is_local = False
        self.editing = False

    def resync(self) -> None:
        self.synchronized = False
        self.editing = False
        self.state_valid = False
        self.takeout_is_local = False
        self.readiness = None
        self.calibrating = False
        self.board_event = None
        self.board_throws = None
        self.takeout_phase = None
        self.takeout_context = None

    def board_status(self, data: dict) -> None:
        """Recognize explicit reference statuses; absence of readiness never means ready."""
        status = data.get("status")
        action = data.get("event")
        self.board_event = action.lower() if isinstance(action, str) else None
        count = data.get("numThrows")
        self.board_throws = count if type(count) is int and 0 <= count <= 3 else None
        if not isinstance(status, str):
            self.readiness = None
            return
        status = status.lower().replace("_", " ").replace("-", " ").strip()
        self.calibrating = status == "calibrating"
        if status in {"throw", "ready", "ready for throw"}:
            self.readiness = "ready"
        elif status in {"takeout", "takeout in progress", "removing darts"}:
            self.readiness = "takeout"
        else:
            self.readiness = "waiting"

    def current_state(self, available: bool) -> dict:
        """A current display snapshot, including silent baselines and corrected scores."""
        frame = self.frame if available and self.synchronized and self.state_valid else None
        result = {
            "board_id": self.board_id or None,
            "match_id": self.match_id,
            "available": available,
            "phase": "waiting",
            "reason": "Connection unavailable",
            "player": None,
            "remaining": None,
            "turn_score": None,
            "last_dart": None,
        }
        if not available:
            return result
        if not self.match_id:
            if self.calibrating:
                return {**result, "reason": "Board is calibrating"}
            return {**result, "phase": "idle", "reason": "No active match"}
        if frame is None:
            return {**result, "reason": "Waiting for match state"}
        player = frame.players[frame.player]
        turn = frame.turns[0] if frame.turns else None
        owner = self._context(frame, turn).get("player") if turn else None
        active_turn = turn if owner and owner["id"] == player.id else None
        result.update(self._context(frame))
        result["turn_score"] = active_turn.points if active_turn else 0
        result["last_dart"] = (
            active_turn.throws[-1].segment.number * active_turn.throws[-1].segment.multiplier
            if active_turn and active_turn.throws
            else None
        )
        if self.calibrating:
            return {**result, "reason": "Board is calibrating"}
        completed = turn and (
            len(turn.throws) == 3
            or turn.busted
            or turn.finished_at
            or frame.game_winner >= 0
            or frame.winner >= 0
        )
        if (
            (self.takeout_phase == "takeout_started" and self.takeout_is_local)
            or self.readiness == "takeout"
            or (
                completed
                and owner
                and owner["is_local"]
                and not (self.takeout_phase == "takeout_finished" and self.takeout_is_local)
            )
        ):
            result.update(phase="takeout", reason="Remove darts")
        elif frame.game_winner >= 0 or frame.winner >= 0:
            result["reason"] = "Leg or match finished"
        elif not result["player"]["is_local"]:
            result["reason"] = "Waiting for another board or a bot"
        elif self.readiness == "ready":
            result.update(phase="ready", reason="Board reports ready for throw")
        else:
            result["reason"] = "Waiting for explicit board readiness"
        return result

    def _remember(self, key: tuple) -> bool:
        if key in self.seen:
            self.seen.move_to_end(key)
            return False
        self.seen[key] = None
        if len(self.seen) > 4096:
            self.seen.popitem(last=False)
        return True

    def _store(self, key: tuple, turn: Turn) -> None:
        self.turns[key] = turn
        self.turns.move_to_end(key)
        if len(self.turns) > 256:
            self.turns.popitem(last=False)

    @staticmethod
    def _key(frame: MatchFrame, turn: Turn) -> tuple:
        return frame.id, frame.set, frame.leg, turn.id

    def _context(
        self, frame: MatchFrame | None, turn: Turn | None = None, player_index: int | None = None
    ) -> dict:
        result = {"board_id": self.board_id, "match_id": self.match_id}
        if frame is None:
            return result
        index = frame.player if player_index is None else player_index
        if turn is not None and player_index is None:
            if turn.player_id:
                index = next((i for i, p in enumerate(frame.players) if p.id == turn.player_id), -1)
            elif turn.player is not None:
                index = turn.player
        player = frame.players[index] if 0 <= index < len(frame.players) else None
        score = frame.game_scores[index] if 0 <= index < len(frame.game_scores) else None
        targets = frame.state.get("targets")
        target = targets[index] if isinstance(targets, list) and 0 <= index < len(targets) else None
        current_targets = frame.state.get("currentTargets")
        if isinstance(target, list) and isinstance(current_targets, list):
            position = current_targets[index] if 0 <= index < len(current_targets) else None
            target = (
                target[position]
                if isinstance(position, int) and 0 <= position < len(target)
                else None
            )
        guide = frame.state.get("checkoutGuide")
        guides = frame.state.get("checkoutGuides")
        if isinstance(guides, list) and 0 <= index < len(guides):
            guide = guides[index]
        result.update(
            {
                "variant": frame.variant,
                "set": frame.set,
                "leg": frame.leg,
                "round": turn.round if turn and turn.round is not None else frame.round,
                "turn_id": turn.id if turn else None,
                "player": player.public(index, self.board_id) if player else None,
                "game_score": score,
                "remaining": score if frame.variant in {"X01", "Random Checkout", "121"} else None,
                "turn_total": turn.score if turn else None,
                "settings": frame.settings,
                "target": target if isinstance(target, dict) else None,
                "checkout_available": bool(guide),
                "editing": self.editing,
            }
        )
        return result

    def _emit(
        self,
        name: GameEventName,
        raw: Event,
        data: dict,
        key: tuple | None = None,
        silent: bool = False,
    ) -> None:
        if key is not None and not self._remember((name, *key)):
            return
        if not silent:
            self.bus.publish("core", name, data, kind="normalized", received_at=raw.received_at)
            self.emitted += 1

    def consume(self, raw: Event) -> None:
        if raw.event == "match.state":
            self._match_state(raw)
        elif raw.event == "board.matches":
            action, match_id = raw.data.get("event"), raw.data.get("id")
            if action == "start" and isinstance(match_id, str) and match_id:
                self.select(match_id)
                self._emit("match_started", raw, self._context(None), (match_id,))
            elif action in {"finish", "delete", "end"} and match_id == self.match_id:
                self._emit(
                    "match_ended", raw, {**self._context(self.frame), "reason": action}, (match_id,)
                )
        elif raw.event in {"board.events", "board.state", "match.events"}:
            self._board_event(raw)

    def _board_event(self, raw: Event) -> None:
        if raw.event == "board.state":
            self.board_status(raw.data)
        action = raw.data.get("event", raw.data.get("status", ""))
        if not isinstance(action, str) or raw.snapshot:
            return
        action = action.lower().replace("_", " ").replace("-", " ")
        # Board state and event envelopes are paired. Emit once from the event stream.
        if raw.event == "board.events" and action in {
            "manual reset",
            "calibration started",
            "calibration finished",
        }:
            self._emit(action.replace(" ", "_"), raw, self._context(self.frame))
            if action == "calibration started":
                self.calibrating = True
                self.readiness = "waiting"
            elif action == "calibration finished":
                self.calibrating = False
        phases = {
            "takeout started": "takeout_started",
            "takeout start": "takeout_started",
            "takeout finished": "takeout_finished",
            "takeout finish": "takeout_finished",
            "darts pulled": "takeout_finished",
        }
        phase = phases.get(action)
        if phase and phase != self.takeout_phase:
            self.takeout_phase = phase
            turn = self.frame.turns[0] if self.frame and self.frame.turns else None
            context = self._context(self.frame, turn)
            phase_context = (
                self.takeout_context or context if phase == "takeout_finished" else context
            )
            self.takeout_is_local = raw.event.startswith("board.") or bool(
                (phase_context.get("player") or {}).get("is_local")
            )
            if self.takeout_is_local and raw.event != "board.state":
                self.readiness = "takeout" if phase == "takeout_started" else "waiting"
            if phase == "takeout_started":
                self.takeout_context = context
            self._emit(phase, raw, self.takeout_context or context)
            if phase == "takeout_finished":
                self.takeout_context = None
        elif raw.event.startswith("board.") and action in {"throw detected", "manual reset"}:
            state_matches = self.board_event == action and (
                self.board_throws == raw.data.get("throwNumber")
                if action == "throw detected"
                else self.board_throws == 0
            )
            if raw.event != "board.state" and not state_matches:
                self.readiness = "waiting"
            self.takeout_phase = None
            self.takeout_context = None

    def _match_state(self, raw: Event) -> None:
        if raw.data.get("id", self.match_id) != self.match_id:
            return
        if "activated" in raw.data and isinstance(raw.data["activated"], int):
            editing = raw.data["activated"] >= 0
            if editing != self.editing:
                self.editing = editing
                self._emit(
                    "match_editing",
                    raw,
                    {**self._context(self.frame), "editing": editing},
                    silent=raw.snapshot or not self.synchronized,
                )
        # Activation-only updates are not full scoring states.
        if "turns" not in raw.data:
            return
        if raw.data.get("id", self.match_id) != self.match_id:
            return
        try:
            frame = MatchFrame.model_validate(raw.data)
            if max(frame.player, frame.game_winner, frame.winner) >= len(frame.players):
                raise ValueError("Player index outside roster")
            for turn in frame.turns:
                if len({dart.id for dart in turn.throws}) != len(turn.throws):
                    raise ValueError("Duplicate dart ID")
        except (ValidationError, ValueError):
            self.invalid_states += 1
            self.state_valid = False
            self.readiness = None
            return
        if frame.id != self.match_id:
            return
        silent = raw.snapshot or not self.synchronized
        previous = self.frame
        current = frame.turns[0] if frame.turns else None
        if silent:
            for turn in frame.turns:
                key = self._key(frame, turn)
                self._store(key, turn)
                for dart in turn.throws:
                    self._remember(("throw", *key, dart.id))
                self._outcomes(raw, frame, turn, silent=True, current=turn is current)
        else:
            if previous and previous.turns:
                old_turn = previous.turns[0]
                if current is None or self._key(previous, old_turn) != self._key(frame, current):
                    forward = current is not None and self._key(frame, current) not in self.turns
                    if old_turn.throws and forward:
                        self._emit(
                            "turn_end",
                            raw,
                            {
                                **self._context(previous, old_turn),
                                "score": old_turn.points,
                                "busted": old_turn.busted,
                            },
                            self._key(previous, old_turn),
                        )
            if previous and frame.players[frame.player].id != previous.players[previous.player].id:
                self._emit("player_changed", raw, self._context(frame))
            if current and self._key(frame, current) not in self.turns:
                self._emit(
                    "turn_started",
                    raw,
                    {
                        **self._context(frame, current),
                        "leg_start": bool(
                            previous and (frame.set, frame.leg) != (previous.set, previous.leg)
                        ),
                    },
                )
            if current:
                self._darts(raw, frame, current)
                self._outcomes(raw, frame, current)
        self.frame = frame
        self.synchronized = True
        self.state_valid = True

    def _darts(self, raw: Event, frame: MatchFrame, turn: Turn) -> None:
        key = self._key(frame, turn)
        old = self.turns.get(key)
        old_darts = {dart.id: dart for dart in old.throws} if old else {}
        new_ids = {dart.id for dart in turn.throws}
        replacements = set()
        context = self._context(frame, turn)
        for position, dart in enumerate(turn.throws, 1):
            before = old_darts.get(dart.id)
            if before is None and old and position <= len(old.throws):
                candidate = old.throws[position - 1]
                if candidate.id not in new_ids:
                    before = candidate
                    replacements.add(candidate.id)
            data = {**context, "dart": dart.public(position), "turn_score": turn.points}
            if before:
                if before != dart:
                    self._emit(
                        "throw_corrected", raw, {**data, "previous_dart": before.public(position)}
                    )
                self._remember(("throw", *key, dart.id))
            elif self._remember(("throw", *key, dart.id)):
                self.takeout_phase = None
                self.takeout_context = None
                self.takeout_is_local = False
                if (context.get("player") or {}).get("is_local") and self.board_throws != len(
                    turn.throws
                ):
                    self.readiness = "waiting"
                self._emit("throw", raw, data)
            else:
                self._emit("throw_corrected", raw, {**data, "restored": True})
        if old:
            for position, dart in enumerate(old.throws, 1):
                if dart.id not in new_ids and dart.id not in replacements:
                    self._emit("throw_removed", raw, {**context, "dart": dart.public(position)})
        self._store(key, turn)

    def _outcomes(
        self, raw: Event, frame: MatchFrame, turn: Turn, silent: bool = False, current: bool = True
    ) -> None:
        key = self._key(frame, turn)
        context = self._context(frame, turn)
        data = {**context, "score": turn.points, "busted": turn.busted}
        last_dart = turn.throws[-1].id if turn.throws else ""
        if turn.busted:
            self._emit("bust", raw, data, (*key, last_dart), silent)
        won = current and (frame.game_winner >= 0 or frame.winner >= 0)
        if len(turn.throws) >= 3 or turn.busted or turn.finished_at or won:
            self._emit("turn_end", raw, data, key, silent)
        if won:
            winner = frame.game_winner if frame.game_winner >= 0 else frame.winner
            win_data = {**self._context(frame, turn, winner), "score": turn.points}
            win_key = (frame.id, frame.set, frame.leg, last_dart)
            if frame.variant in {"X01", "Random Checkout"}:
                self._emit("checkout", raw, win_data, win_key, silent)
            self._emit("leg_win", raw, win_data, win_key, silent)
            if frame.winner >= 0:
                self._emit(
                    "match_win",
                    raw,
                    {**win_data, **self._context(frame, turn, frame.winner)},
                    win_key,
                    silent,
                )
