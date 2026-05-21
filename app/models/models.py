from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

EventLevel = Literal["L0", "L1", "L2", "L3"]
EventSource = Literal["user", "system"]
EventType = Literal[
    "user_message",
    "assistant_response",
]


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_key() -> str:
    return datetime.now().astimezone().date().isoformat()


@dataclass
class Event:
    id: str
    timestamp: str
    level: EventLevel
    source: EventSource
    type: EventType
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        level: EventLevel,
        source: EventSource,
        type: EventType,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> "Event":
        return cls(
            id=str(uuid.uuid4()),
            timestamp=iso_now(),
            level=level,
            source=source,
            type=type,
            content=content,
            metadata=metadata or {},
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            id=str(data["id"]),
            timestamp=str(data["timestamp"]),
            level=data["level"],
            source=data["source"],
            type=data["type"],
            content=str(data["content"]),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "level": self.level,
            "source": self.source,
            "type": self.type,
            "content": self.content,
            "metadata": self.metadata,
        }
