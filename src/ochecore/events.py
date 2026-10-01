import asyncio
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError


class Event(BaseModel):
    """Versioned transport envelope; snapshots must not be treated as new throws."""

    schema_version: int = 1
    id: str = Field(default_factory=lambda: str(uuid4()))
    sequence: int
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str
    event: str
    channel: str | None = None
    topic: str | None = None
    snapshot: bool = False
    data: dict[str, Any]


class CloudMessage(BaseModel):
    channel: str
    topic: str
    data: dict[str, Any]


def parse_cloud_message(raw: str | bytes) -> CloudMessage | None:
    """Validate the transport envelope. Throw normalization belongs to phase 2."""
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
    if message.channel == "autodarts.matches" and match_id and message.topic == f"{match_id}.state":
        return "match.state"
    if message.channel == "autodarts.users" and message.topic.endswith(".events"):
        return "user.events"
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


class EventBus:
    def __init__(self, capacity: int = 100):
        self.history: deque[Event] = deque(maxlen=capacity)
        self.queues: set[asyncio.Queue[Event]] = set()
        self.sequence = 0
        self.dropped = 0

    def publish(self, source: str, event: str, data: dict, **kwargs: Any) -> Event:
        self.sequence += 1
        envelope = Event(
            sequence=self.sequence, source=source, event=event, data=redact(data), **kwargs
        )
        self.history.append(envelope)
        for queue in self.queues:
            if queue.full():
                queue.get_nowait()
                self.dropped += 1
            queue.put_nowait(envelope)
        return envelope

    @contextmanager
    def subscribe(self, capacity: int = 100) -> Iterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=capacity)
        self.queues.add(queue)
        try:
            yield queue
        finally:
            self.queues.discard(queue)
