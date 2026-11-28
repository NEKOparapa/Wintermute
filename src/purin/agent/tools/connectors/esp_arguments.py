"""Input contracts for the five Metalio ESP32 device applications."""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("title", "body", "text", "label", check_fields=False)
    @classmethod
    def device_text(cls, value: str) -> str:
        if any(ord(c) < 32 and c not in "\t\n\r" for c in value):
            raise ValueError("设备文本包含不支持的控制字符")
        if value and not value.strip():
            raise ValueError("设备文本不能只包含空白字符")
        return value


class _Status(_Args):
    action: Literal["status"]


class _Stop(_Args):
    action: Literal["stop"]


class _Pulse(_Args):
    action: Literal["pulse"]
    duration_ms: int = Field(default=100, ge=1, le=1000)


class _Play(_Args):
    action: Literal["play"]
    file: str = Field(
        default="",
        max_length=64,
        description="SD 卡 Audio 目录中的 WAV 文件名，省略播放提示音",
        pattern=r"^(?:[A-Za-z0-9_-][A-Za-z0-9_. -]*\.[Ww][Aa][Vv])?$",
    )
    volume: int = Field(default=45, ge=1, le=100)


class _List(_Args):
    action: Literal["list"]
    offset: int = Field(default=0, ge=0, le=65535)


class _Card(_Args):
    action: Literal["show"]
    title: str = Field(min_length=1, max_length=48, pattern=r"\S")
    body: str = Field(min_length=1, max_length=240, pattern=r"\S")


class _Start(_Args):
    action: Literal["start"]
    seconds: int = Field(ge=1, le=86400)
    label: str = Field(default="", max_length=48)


_BookId = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,12}$", description="推送书籍的书目 ID")]
_TxtFilename = Annotated[
    str,
    Field(pattern=r'^[^/\\:*?"<>|\x00-\x1f]+\.[Tt][Xx][Tt]$'),
]


class _Book(_Args):
    id: _BookId


class _ShelfBook(_Args):
    model_config = ConfigDict(
        json_schema_extra={"oneOf": [{"required": ["id"]}, {"required": ["file"]}]}
    )
    id: _BookId | SkipJsonSchema[None] = None
    file: _TxtFilename | SkipJsonSchema[None] = Field(
        default=None,
        description="list 返回的本机 TXT 原文件名，与 id 二选一",
    )

    @model_validator(mode="after")
    def select_one_book(self) -> Self:
        selected = self.model_fields_set & {"id", "file"}
        if len(selected) != 1 or (self.id is None and self.file is None):
            raise ValueError("请使用书目 id 或 TXT 文件名 file 指定一本书")
        if self.file is not None and len(self.file.encode("utf-8")) > 255:
            raise ValueError("TXT 文件名最多 255 个 UTF-8 字节")
        return self


class _Add(_Book):
    action: Literal["add"]
    title: str = Field(min_length=1, max_length=48, pattern=r"\S")
    text: str = Field(min_length=1, max_length=2500, pattern=r"\S")


class _Read(_ShelfBook):
    action: Literal["read"]
    chapter: int = Field(default=1, ge=1, le=262144, description="推送书籍的章号或 TXT 的分段号")
    page: int = Field(default=1, ge=1, le=2500, description="页码，从 1 开始")

    @model_validator(mode="after")
    def pushed_chapter_limit(self) -> Self:
        if self.id is not None and self.chapter > 999:
            raise ValueError("推送书籍的章号最大为 999")
        return self


class _Append(_Book):
    action: Literal["append"]
    chapter: int = Field(ge=2, le=999, description="依次追加的章号，第一章由 add 创建")
    title: str = Field(min_length=1, max_length=48, pattern=r"\S")
    text: str = Field(min_length=1, max_length=2500, pattern=r"\S")


class _Delete(_ShelfBook):
    action: Literal["delete"]


def _actions(variants: Any) -> TypeAdapter[Any]:
    return TypeAdapter(Annotated[variants, Field(discriminator="action")])


ESP_CONTRACTS: dict[str, tuple[str, TypeAdapter[Any]]] = {
    "esp_vibration": ("短震、停止或检查 ESP 马达输出", _actions(_Pulse | _Stop | _Status)),
    "esp_player": (
        "播放板载提示音或 SD 卡 WAV，列出音频、停止或查询播放状态",
        _actions(_Play | _List | _Stop | _Status),
    ),
    "esp_card": ("在墨水屏显示提示卡片，查询用户是否已确认", _actions(_Card | _Status)),
    "esp_timer": ("在 ESP 启动本地倒计时，停止或查看剩余秒数", _actions(_Start | _Stop | _Status)),
    "esp_library": (
        "书库管理：列出设备上的书籍，按页阅读，发送书籍或追加章节，"
        "按书目 ID 或 TXT 文件名删除指定书籍",
        _actions(_Add | _Append | _List | _Read | _Delete),
    ),
}
