"""一次性日程及其进入主 Agent 通知层的事件信封。"""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, field_validator

from wintermute.events.core.presentation import timestamp_text
from wintermute.events.core.types import EventInput, Identifier, _Record

type ScheduleAction = Literal["notify_agent"]
type ScheduleStatus = Literal["scheduled", "triggered"]


class ScheduleEntry(_Record):
    id: Identifier
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    content: Annotated[str, Field(max_length=16000)] = ""
    trigger_at: AwareDatetime
    action: ScheduleAction = "notify_agent"
    status: ScheduleStatus = "scheduled"
    triggered_at: AwareDatetime | None = None

    @field_validator("trigger_at", "triggered_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


def due_notification(entry: ScheduleEntry, user_id: str, conversation_id: str) -> EventInput:
    terminal = "wintermute:internal:schedule:" + sha256(user_id.encode()).hexdigest()
    return EventInput(
        terminal_id=terminal,
        conversation_id=conversation_id,
        layer="notification",
        type="schedule.due",
        source_id="时间表",
        producer_event_id=entry.id,
        correlation_id=entry.id,
        occurred_at=entry.trigger_at,
        payload={
            "title": entry.title,
            "content": entry.content,
            "trigger_at": timestamp_text(entry.trigger_at),
        },
    )
