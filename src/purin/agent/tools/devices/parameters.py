"""Model-facing arguments for device messages."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from purin.events.storage import notifications
from purin.events.storage.repositories import questions

ASK_FIELDS = frozenset({"question", "options", "multi_select", "allow_custom"})
NOTIFY_FIELDS = frozenset({"targets", "body", "attachments", "notify_at", "expires_at"})


class HistoryReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    history_file_id: Annotated[str, Field(min_length=1, max_length=256)]


type RelativePath = Annotated[str, Field(min_length=1, max_length=1024)]
type OptionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=questions.MAX_OPTION_LEN)
]


class DeviceMessagingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["notify", "ask"]
    title: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=questions.MAX_TITLE_LEN),
    ] = Field(description="设备上显示的标题。")
    targets: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list,
        max_length=128,
        description="notify 必填。从 device_statuses 中选择 can_notify 为 true 的设备 ID。",
    )
    body: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] = Field(
        default="", description="notify 的通知正文。正文和附件至少提供一项。"
    )
    attachments: list[RelativePath | HistoryReference] = Field(
        default_factory=list,
        max_length=notifications.MAX_FILES,
        description=(
            "notify 的工作目录相对路径或 history_file_id 引用。"
            "最多 4 个，单个 8 MiB，合计 16 MiB。"
        ),
    )
    notify_at: AwareDatetime | None = Field(
        default=None, description="notify 必填。发送通知的时间，使用带时区的 ISO 8601。"
    )
    expires_at: AwareDatetime | None = Field(
        default=None, description="notify 可选的提醒到期时间（含时区的 ISO 8601）。"
    )
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=questions.MAX_QUESTION_LEN)
    ] = Field(default="", description="ask 必填，向本会话用户提出的问题。")
    options: list[OptionText] = Field(
        default_factory=list,
        max_length=questions.MAX_OPTIONS,
        description="ask 的预设选项。留空时由用户输入回答。",
    )
    multi_select: bool = Field(default=False, description="ask 是否允许多选。")
    allow_custom: bool = Field(default=True, description="ask 是否允许用户自定义回答。")

    @field_validator("notify_at", "expires_at", mode="before")
    @classmethod
    def parse_time(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def check_request(self) -> Self:
        other_fields = NOTIFY_FIELDS if self.action == "ask" else ASK_FIELDS
        if unexpected := self.model_fields_set & other_fields:
            raise ValueError(f"{self.action} 不接受这些参数：{', '.join(sorted(unexpected))}")
        if self.action == "ask":
            if not self.question:
                raise ValueError("提问需要 question")
            if len(set(self.options)) != len(self.options):
                raise ValueError("预设选项不能重复")
            if not self.options and not self.allow_custom:
                raise ValueError("未提供预设选项时必须允许自定义输入")
        elif self.notify_at is None:
            raise ValueError("发送通知需要带时区的通知时间 notify_at")
        elif self.expires_at is not None and self.expires_at <= self.notify_at:
            raise ValueError("expires_at 必须晚于 notify_at")
        elif not self.targets or not (self.body or self.attachments):
            raise ValueError("发送通知需要 targets，以及正文或附件")
        elif len(set(self.targets)) != len(self.targets):
            raise ValueError("同一接收设备只能选择一次")
        return self
