"""实时事件与历史记录入口共享的信封、设备身份和持久化状态。"""

import json
import unicodedata
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

from wintermute.agent import UserMessage
from wintermute.agent.core.types import ImageMessage, background_result_text
from wintermute.events.core.presentation import body_text, body_value, origin_text, timestamp_text

type Layer = Literal["conversation", "notification", "perception", "slice", "record"]
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


class Device(_Record):
    """应用注册的设备身份；同一个设备可以使用多个入口。"""

    device_id: Identifier
    user_id: Identifier
    allowed_layers: tuple[Layer, ...] = (
        "conversation", "notification", "perception", "slice", "record"
    )

    @field_validator("allowed_layers")
    @classmethod
    def validate_layers(cls, value: tuple[Layer, ...]) -> tuple[Layer, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("allowed_layers must be nonempty and unique")
        return value


class AudioSliceData(_Record):
    """不可变音频分段引用；不接收任意本机路径或远程 URL。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    file: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}\.ogg$")]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    bytes: Annotated[int, Field(gt=0, le=20 * 1024 * 1024)]
    format: Literal["ogg"] = "ogg"
    codec: Literal["opus"] = "opus"
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1
    recording_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
    sequence: Annotated[int, Field(ge=1)]
    start_sample: Annotated[int, Field(ge=0)]
    samples: Annotated[int, Field(gt=0, le=300 * 16000)]

    @model_validator(mode="after")
    def validate_filename(self) -> Self:
        if self.file != self.sha256 + ".ogg":
            raise ValueError("Audio filename must match its digest")
        if self.start_sample + self.samples > 120 * 60 * 16000:
            raise ValueError("Audio segment must fall within the first two recording hours")
        return self


class ScreenshotContext(_Record):
    """Device-observed usage accompanying a periodic screenshot."""

    platform: Literal["android", "ios"]
    app: Identifier | None = None
    package: Identifier | None = None
    title: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    start: Annotated[int, Field(strict=True, ge=0, le=253402300799999)] | None = None
    activity_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if not self.app and self.platform != "ios":
            raise ValueError("A foreground application is required")
        for value in (self.app, self.title, self.package, self.activity_id):
            if value is not None and (
                not value.strip() or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)
            ):
                raise ValueError("Invalid capture metadata")
        return self


class EventInput(_Record):
    """设备提交的输入；用户归属和接收时间由服务端补充。"""

    producer_event_id: Identifier
    device_id: Identifier
    layer: Layer
    type: Identifier
    schema_version: Literal[1] = 1
    source_id: Identifier = "default"
    conversation_id: Identifier | None = None
    correlation_id: Identifier | None = None
    reply_device_id: Identifier | None = None
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
            if self.type == "slice.audio":
                if set(self.payload) != {"audio"}:
                    raise ValueError("slice.audio requires only payload.audio")
                AudioSliceData.model_validate(self.payload.get("audio"))
            elif self.type == "slice.image":
                digest = self.payload.get("sha256")
                if (
                    set(self.payload) - {"sha256", "capture"}
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)
                ):
                    raise ValueError("slice.image requires a SHA-256 digest")
            elif self.type == "slice.recorded":
                text = self.payload.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("slice.recorded requires nonempty payload.text")
            else:
                raise ValueError("Unsupported slice type")
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


def event_message(event: EventRecord) -> UserMessage:
    """来源取自已认证的入口，用户正文及通知 payload 不能覆盖它。"""
    timestamp = timestamp_text(event.occurred_at)
    if event.layer == "conversation":
        user_text = str(event.payload["text"])
        images = event.payload.get("image_hashes")
        if isinstance(images, list) and images:
            return ImageMessage(
                user_text,
                "user",
                timestamp,
                origin_text("用户原话", event.device_id, event.source_id),
                images=tuple(f"{event.event_id}:chat:{i}" for i in range(len(images))),
            )
        return UserMessage(
            user_text,
            "user",
            timestamp,
            origin_text("用户原话", event.device_id, event.source_id),
        )
    if event.layer != "notification":
        raise EventError("Only conversation and notification events are session inputs")
    payload = event.payload
    agent = payload.get("agent")
    if event.type in {"subagent.completed", "subagent.error", "subagent.cancelled"} and isinstance(
        agent, str
    ):
        source = "" if event.source_id == "background_subagent" else event.source_id
        origin = origin_text("后台通知", event.device_id, source, agent)
        content = background_result_text(
            event.type.removeprefix("subagent."),
            body_text(payload.get("output") or ""),
            body_text(payload.get("error") or ""),
            truncated=payload.get("truncated") is True,
        )
    elif (
        event.type == "terminal_tool.result"
        and event.device_id.startswith("wintermute:internal:remote-tool:")
        and isinstance(text := payload.get("text"), str)
    ):
        origin = "后台通知 · 设备工具"
        content = text
    elif (
        event.type in {"question.answered", "question.dismissed"}
        and event.device_id.startswith("wintermute:internal:question:")
        and isinstance(text := payload.get("text"), str)
    ):
        origin = (
            "后台通知 · 用户回答"
            if event.type == "question.answered"
            else "后台通知 · 提问已忽略"
        )
        content = text
    elif (
        event.type == "device_notification.result"
        and event.device_id.startswith("wintermute:internal:schedule:")
    ):
        origin = "后台通知 · 设备通知投递结果"
        content = f"定时通知「{payload.get('title')}」"
        if sent := payload.get("sent"):
            content += "\n已投递至设备收件箱：" + body_text(sent)
        if isinstance(failures := payload.get("failed"), dict):
            for reason, devices in failures.items():
                content += f"\n投递失败：{body_text(devices)}。原因：{reason}"
    elif (
        event.type == "schedule.due"
        and event.device_id.startswith("wintermute:internal:schedule:")
        and isinstance(title := payload.get("title"), str)
        and isinstance(trigger_at := payload.get("trigger_at"), str)
    ):
        origin = "后台通知 · 时间表"
        content = f"日程到点：{title}\n计划触发时间：{trigger_at}"
        if payload.get("content"):
            content += "\n" + body_text(payload["content"])
    else:
        origin = origin_text("后台通知", event.device_id, event.source_id, event.type)
        visible = body_value(payload)
        if isinstance(visible, dict) and len(visible) == 1:
            key, value = next(iter(visible.items()))
            if key in {"text", "body", "content", "message", "summary", "output", "result"}:
                visible = value
        content = body_text(visible) if visible != {} else "收到通知。"
    return UserMessage(content, "notification", timestamp, origin)


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
    """模型处理完成和设备投递完成是两个独立状态。"""

    output_id: str
    event_id: str
    job_id: str
    user_id: str
    conversation_id: str
    device_id: str | None
    layer: Literal["conversation", "notification"]
    text: str
    status: Literal["pending", "delivered"] = "pending"
    error: str | None = None
