"""四个事件入口共享的信封、终端身份和持久化状态。"""

import json
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

type Layer = Literal["conversation", "notification", "perception", "slice"]
type JobStatus = Literal["queued", "running", "completed", "failed", "interrupted"]
type Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


def utc_now() -> datetime:
    """使用带时区的服务端时间。"""
    return datetime.now(UTC)


class EventError(ValueError):
    """事件格式、归属或状态不符合接收约定。"""


class DuplicateEventError(EventError):
    """发送方复用了事件 ID，却改变了事件内容。"""


class SchedulerBusyError(RuntimeError):
    """数据库已有调度器，或者当前调度器的租约已经失效。"""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class Terminal(_Record):
    """应用注册的终端身份；同一个终端可以使用多个入口。"""

    terminal_id: Identifier
    user_id: Identifier
    allowed_layers: tuple[Layer, ...] = ("conversation", "notification", "perception", "slice")

    @field_validator("allowed_layers")
    @classmethod
    def validate_layers(cls, value: tuple[Layer, ...]) -> tuple[Layer, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("allowed_layers must be nonempty and unique")
        return value


class EventInput(_Record):
    """终端提交的输入；用户归属和接收时间由服务端补充。"""

    producer_event_id: Identifier
    terminal_id: Identifier
    layer: Layer
    type: Identifier
    schema_version: Literal[1] = 1
    source_id: Identifier = "default"
    conversation_id: Identifier | None = None
    correlation_id: Identifier | None = None
    reply_terminal_id: Identifier | None = None
    source_seq: Annotated[int, Field(strict=True, ge=0)] | None = None
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value

    @model_validator(mode="after")
    def validate_layer(self) -> Self:
        if self.layer == "conversation":
            if self.type != "user.message" or self.conversation_id is None:
                raise ValueError("Conversation input requires user.message and conversation_id")
            text = self.payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("user.message requires nonempty payload.text")
        elif self.layer == "notification" and self.conversation_id is None:
            raise ValueError("Notification input requires a target conversation_id")
        elif self.layer == "slice":
            if self.type != "slice.recorded":
                raise ValueError("Slice input requires slice.recorded")
            text = self.payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("slice.recorded requires nonempty payload.text")
            metadata = self.payload.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("slice.recorded payload.metadata must be an object")
        if self.layer == "perception" and self.type == "state.observed":
            for key in ("subject", "state_key"):
                item = self.payload.get(key)
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(f"state.observed requires payload.{key}")
            if "value" not in self.payload:
                raise ValueError("state.observed requires payload.value")
        if self.layer == "perception" and self.payload.get("valid_until") is not None:
            valid_until = self.payload["valid_until"]
            if not isinstance(valid_until, str):
                raise ValueError("payload.valid_until must be an ISO datetime string")
            parsed = datetime.fromisoformat(valid_until)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("payload.valid_until must include a timezone")
        return self


class EventRecord(EventInput):
    """已持久化的完整事件；调用方收到的是独立快照。"""

    event_id: Identifier
    user_id: Identifier
    received_at: AwareDatetime


class Receipt(_Record):
    event: EventRecord
    duplicate: bool
    job_id: str | None = None


class JobRecord(_Record):
    job_id: str
    event_id: str
    user_id: str
    conversation_id: str
    layer: Literal["conversation", "notification"]
    status: JobStatus
    priority: int
    attempts: int
    error: str | None = None


class OutputRecord(_Record):
    """模型处理完成和终端投递完成是两个独立状态。"""

    output_id: str
    event_id: str
    job_id: str
    user_id: str
    conversation_id: str
    terminal_id: str | None
    layer: Literal["conversation", "notification"]
    text: str
    status: Literal["pending", "delivered"] = "pending"
    error: str | None = None
