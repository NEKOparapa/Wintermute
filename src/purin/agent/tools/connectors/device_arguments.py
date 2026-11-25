"""Argument contracts for tools implemented by the device clients."""

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from purin.events.integrations.mcp_client import McpToolInfo

from .esp_arguments import ESP_CONTRACTS


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Command(_Args):
    command: str = Field(min_length=1, description="本机命令，最多 8192 UTF-8 字节")

    @field_validator("command")
    @classmethod
    def check_command(cls, command: str) -> str:
        if not command.strip() or "\x00" in command or len(command.encode()) > 8192:
            raise ValueError("命令必须是非空文本，不能包含 NUL，最多 8192 UTF-8 字节")
        return command


class _VaultInfo(_Args):
    action: Literal["vault_info"]


class _ListNotes(_Args):
    action: Literal["list_notes"]
    folder: str = ""
    limit: int = Field(default=50, ge=1, le=100)


class _ReadNote(_Args):
    action: Literal["read_note"]
    path: str = Field(min_length=1, description="相对于笔记库的路径")
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=4000, ge=200, le=5500)


class _SearchNotes(_Args):
    action: Literal["search_notes"]
    query: str = Field(pattern=r"\S")
    limit: int = Field(default=15, ge=1, le=30)


class _WriteNote(_Args):
    action: Literal["write_note", "append_note"]
    path: str = Field(min_length=1, description="相对于笔记库的路径")
    content: str


type _ObsidianArgs = Annotated[
    _VaultInfo | _ListNotes | _ReadNote | _SearchNotes | _WriteNote, Field(discriminator="action")
]
_OBSIDIAN = TypeAdapter(_ObsidianArgs)
_SHELL = TypeAdapter(_Command)
_STATUS = TypeAdapter(_Args)


class _Alarm(_Args):
    hour: int = Field(ge=0, le=23, description="手机本地时间，24 小时制")
    minute: int = Field(ge=0, le=59)
    label: str = Field(default="", max_length=120)


class _Timer(_Args):
    seconds: int = Field(ge=1, le=86400)
    label: str = Field(default="", max_length=120)


class _Limit(_Args):
    limit: int = Field(default=10, ge=1, le=30)


class _Usage(_Limit):
    days: int = Field(default=1, ge=1, le=7, description="含今天在内的最近几个本地自然日")


class _Window(_Args):
    start_ms: int = Field(ge=0, le=4102444800000, description="开始时间，Unix 毫秒")
    end_ms: int = Field(ge=1, le=4102444800000, description="结束时间，Unix 毫秒")

    @model_validator(mode="after")
    def check_window(self) -> "_Window":
        if not 0 < self.end_ms - self.start_ms <= 31 * 86400000:
            raise ValueError("时间范围须大于零且最多 31 天")
        return self


class _CalendarQuery(_Window, _Limit):
    pass


class _CalendarDraft(_Window):
    title: str = Field(min_length=1, max_length=200, pattern=r"\S")
    location: str = Field(default="", max_length=200)


class _Search(_Limit):
    query: str = Field(min_length=1, max_length=80, pattern=r"\S")


class _Notifications(_Limit):
    package: str = Field(default="", max_length=200, description="可选的来源应用包名")


class _Files(_Limit):
    folder: str = Field(default="", max_length=512, description="授权目录内的相对路径")
    query: str = Field(default="", max_length=80, description="按名称筛选本层文件或子目录")


class _FileText(_Args):
    path: str = Field(min_length=1, max_length=512, description="列举结果中的相对文件路径")
    offset: int = Field(default=0, ge=0, le=1000000, description="字符偏移量")


class _HealthDay(_Args):
    days_ago: int = Field(default=0, ge=0, le=6, description="本地自然日，0 为今天，1 为昨天")


class _PhotoSearch(_CalendarQuery):
    kind: Literal["all", "photo", "screenshot", "video"] = "all"


class _ItemId(_Args):
    id: str = Field(min_length=1, max_length=200, pattern=r"\S")


class _ReminderDraft(_Args):
    title: str = Field(min_length=1, max_length=200, pattern=r"\S")
    due_ms: int | None = Field(
        default=None, ge=1, le=4102444800000, description="到期并通知的 Unix 毫秒时间"
    )


class _MusicPlay(_Args):
    action: Literal["play"]
    id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"\S",
        description="ios_music_search 返回的曲库歌曲编号",
    )


class _MusicSeek(_Args):
    action: Literal["seek"]
    seconds: int = Field(ge=0, le=86400)


class _MusicTransport(_Args):
    action: Literal["pause", "resume", "next", "previous", "status"]


type _MusicAction = Annotated[
    _MusicPlay | _MusicSeek | _MusicTransport, Field(discriminator="action")
]


class _MacNote(_Args):
    title: str = Field(min_length=1, max_length=120, pattern=r"\S")
    body: str = Field(min_length=1, max_length=2000, pattern=r"\S", description="纯文本正文")


class _MacSpeech(_Args):
    text: str = Field(
        min_length=1, max_length=200, pattern=r"\S", description="在 Mac 上朗读的短文本"
    )


_IOS: dict[str, tuple[str, TypeAdapter[Any]]] = {
    "ios_calendar_events": ("查询 iPhone 日历的指定时间范围", TypeAdapter(_CalendarQuery)),
    "ios_calendar_create": ("在 iPhone 日历中新建日程", TypeAdapter(_CalendarDraft)),
    "ios_contacts": ("按姓名查找 iPhone 已授权联系人和联系方式", TypeAdapter(_Search)),
    "ios_location": ("查询 iPhone 单次位置、误差和采样时间", _STATUS),
    "ios_reminders_list": ("列出 iPhone 提醒事项中未完成的待办", TypeAdapter(_Limit)),
    "ios_reminders_create": ("新建待办，可指定到期提醒时间", TypeAdapter(_ReminderDraft)),
    "ios_reminders_complete": ("根据所列编号将待办标为完成", TypeAdapter(_ItemId)),
    "ios_health_summary": (
        "经本机确认，分享指定日的步数、睡眠和运动摘要，仅用于健康健身服务",
        TypeAdapter(_HealthDay),
    ),
    "ios_music_search": ("在 Apple Music 曲库按关键词查找歌曲", TypeAdapter(_Search)),
    "ios_music_control": (
        "播放所选歌曲，或查询、暂停、恢复、切歌和跳转进度",
        TypeAdapter(_MusicAction),
    ),
    "ios_photos_search": ("按时间和类型列出获准访问的照片或视频元数据", TypeAdapter(_PhotoSearch)),
    "ios_photo_open": ("在 iPhone 上显示所选照片或视频的预览图", TypeAdapter(_ItemId)),
}


_ANDROID: dict[str, tuple[str, TypeAdapter[Any]]] = {
    "android_set_alarm": ("在安卓手机时钟中设置闹钟", TypeAdapter(_Alarm)),
    "android_start_timer": ("在安卓手机时钟中开始倒计时", TypeAdapter(_Timer)),
    "android_app_usage": ("按应用汇总近期前台使用时间", TypeAdapter(_Usage)),
    "android_calendar_events": ("查询手机系统日历中的日程", TypeAdapter(_CalendarQuery)),
    "android_calendar_create": ("打开手机日历，预填日程供用户保存", TypeAdapter(_CalendarDraft)),
    "android_contacts": ("按姓名查询联系人及其号码、邮箱", TypeAdapter(_Search)),
    "android_files_list": ("列出授权目录的文件，可按文件名筛选", TypeAdapter(_Files)),
    "android_files_read": ("分段读取授权目录中的 UTF-8 文本文件", TypeAdapter(_FileText)),
    "android_location": ("获取手机当前位置、定位时间和估算误差", _STATUS),
    "android_notifications": ("查看手机通知栏当前的通知", TypeAdapter(_Notifications)),
    "android_sms": ("按关键词搜索手机短信的对方号码和正文", TypeAdapter(_Search)),
}


_MAC: dict[str, tuple[str, TypeAdapter[Any]]] = {
    "mac_finder_selection": ("列出 Mac 访达中选中项目的名称和类型，最多 20 项", _STATUS),
    "mac_notes_create": ("在 Mac 备忘录默认账户中新建一篇笔记", TypeAdapter(_MacNote)),
    "mac_music_control": (
        "查询 Mac 音乐的当前曲目，或暂停、继续、切歌",
        TypeAdapter(_MusicTransport),
    ),
    "mac_speak": ("让 Mac 通过系统语音朗读短文本", TypeAdapter(_MacSpeech)),
}


def _contract(name: str) -> tuple[str, TypeAdapter[Any]] | None:
    if name in _MAC:
        return _MAC[name]
    if name in ESP_CONTRACTS:
        return ESP_CONTRACTS[name]
    if name in _IOS:
        return _IOS[name]
    if name in _ANDROID:
        return _ANDROID[name]
    if name == "obsidian":
        return "查询笔记库、列出、搜索、读取、写入或追加笔记", _OBSIDIAN
    if name == "device_status":
        return "查询设备运行状态", _STATUS
    if name in {"powershell", "cmd", "sh", "bash", "pwsh"}:
        return "在指定设备上运行用户授权的本机命令", _SHELL
    return None


def device_tool_info(name: str) -> McpToolInfo:
    contract = _contract(name)
    if contract is None:
        return McpToolInfo(name, "设备上报的工具", {"type": "object"})
    description, adapter = contract
    return McpToolInfo(name, description, adapter.json_schema())


def validate_device_arguments(name: str, args: dict[str, JsonValue]) -> None:
    contract = _contract(name)
    if contract is not None:
        try:
            contract[1].validate_python(args, strict=True)
        except ValidationError as exc:
            issue = exc.errors(include_input=False)[0]
            path = ".".join(map(str, issue["loc"]))
            raise ValueError(f"{path}: {issue['msg']}") from exc
