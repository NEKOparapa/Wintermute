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
)

from wintermute.events.integrations.mcp_client import McpToolInfo


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


def _contract(name: str) -> tuple[str, TypeAdapter[Any]] | None:
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
