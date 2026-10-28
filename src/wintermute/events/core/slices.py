"""切片流投影及处理状态；原始切片仍使用统一事件信封。"""

from datetime import UTC, date, datetime
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from wintermute.events.core.types import Identifier, _Record


class SliceStreamItem(_Record):
    """某个自然日内同一来源的一批切片处理结果，保留全部来源事件 ID。"""

    item_id: Identifier
    user_id: Identifier
    conversation_id: Identifier | None = None
    terminal_id: Identifier
    source_id: Identifier
    day: str
    timezone: str
    started_at: AwareDatetime
    ended_at: AwareDatetime
    source_event_ids: tuple[Identifier, ...]
    text: str = Field(min_length=1)
    strategy: Identifier
    created_at: AwareDatetime

    @field_validator("day")
    @classmethod
    def validate_day(cls, value: str) -> str:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("day must use YYYY-MM-DD")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must name an available IANA timezone") from exc
        return value

    @field_validator("started_at", "ended_at", "created_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("source_event_ids")
    @classmethod
    def validate_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("source_event_ids must be nonempty and unique")
        return value

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Slice text must be nonempty")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        zone = ZoneInfo(self.timezone)
        if self.ended_at < self.started_at:
            raise ValueError("Slice end must not precede its start")
        if any(
            instant.astimezone(zone).date().isoformat() != self.day
            for instant in (self.started_at, self.ended_at)
        ):
            raise ValueError("Slice range must belong to its configured local day")
        return self


class SliceProcessingStatus(_Record):
    """可重试的持久化处理状态；错误只保存类型名，不保存输入或异常正文。"""

    event_id: Identifier
    status: Literal["pending", "retrying", "completed"]
    attempts: int = Field(ge=0)
    error_type: str | None = None
    retry_at: AwareDatetime | None = None
    item_id: str | None = None
