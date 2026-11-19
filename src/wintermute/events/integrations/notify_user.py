"""Send a durable, attachment-capable message to explicitly selected devices."""

import asyncio
import hashlib
import json
import mimetypes
import os
import stat
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.events.storage import notifications
from wintermute.events.storage.sqlite import SQLiteStore

if TYPE_CHECKING:
    from wintermute.config.models import HTTPTerminalSettings
    from wintermute.events.core.attachments import HistoryAttachment

# Prefer standard types to machine-specific overrides such as Excel claiming .csv on Windows.
_STANDARD_MIME = mimetypes.MimeTypes()


class HistoryReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    history_file_id: Annotated[str, Field(min_length=1, max_length=256)]


type RelativePath = Annotated[str, Field(min_length=1, max_length=1024)]


class NotifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["list_targets", "send"]
    targets: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list, max_length=128
    )
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] = ""
    body: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] = ""
    attachments: list[RelativePath | HistoryReference] = Field(
        default_factory=list,
        max_length=notifications.MAX_FILES,
        description=(
            "工作目录内的相对文件路径，或带 history_file_id 的历史附件引用。"
            "最多 4 个，每个 8 MiB，合计 16 MiB。"
        ),
    )
    expires_at: AwareDatetime | None = Field(
        None, description="可选，提醒停止弹出的时间（ISO 8601，含时区）。"
    )

    @field_validator("expires_at", mode="before")
    @classmethod
    def parse_time(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def check_request(self) -> Self:
        if self.operation == "list_targets":
            if self.targets or self.title or self.body or self.attachments or self.expires_at:
                raise ValueError("查询接收终端时只需 operation")
        elif not self.title or not self.targets or not (self.body or self.attachments):
            raise ValueError("发送需要目标、标题，以及正文或附件")
        elif len(set(self.targets)) != len(self.targets):
            raise ValueError("同一接收终端只能选择一次")
        return self


def _relative(value: str) -> Path:
    portable = PureWindowsPath(value)
    if (
        portable.drive
        or portable.root
        or not portable.parts
        or ".." in portable.parts
        or any(
            PureWindowsPath(p).is_reserved()
            or p.endswith((" ", "."))
            or any(ord(c) < 32 or c in '<>:"|?*' for c in p)
            for p in portable.parts
        )
    ):
        raise ValueError("请选择工作目录内的普通文件")
    if len(portable.name.encode("utf-8")) > 255:
        raise ValueError("附件文件名过长")
    return Path(*portable.parts)


def _snapshot(
    root: Path, relative: Path, name: str, mime: str, remaining: int
) -> notifications.FileSnapshot:
    path = root / relative
    # Open each POSIX component relative to an already-open directory to prevent link swaps.
    with ExitStack() as stack:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
            fd = os.open(root, directory_flags)
            stack.callback(os.close, fd)
            for part in relative.parts[:-1]:
                fd = os.open(part, directory_flags, dir_fd=fd)
                stack.callback(os.close, fd)
            file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(relative.name, file_flags, dir_fd=fd)
            stream = stack.enter_context(os.fdopen(fd, "rb"))
        else:
            if path.resolve(strict=True) != path or not path.is_file():
                raise ValueError("请选择工作目录内的普通文件")
            stream = stack.enter_context(path.open("rb"))
            if path.resolve(strict=True) != path or not os.path.samestat(
                path.stat(), os.fstat(stream.fileno())
            ):
                raise ValueError("附件路径已变化，请重试")
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("附件必须是普通文件")
        limit = min(remaining, notifications.MAX_FILE_BYTES)
        if info.st_size > limit:
            raise ValueError("附件超出大小限制：单个最多 8 MiB，合计最多 16 MiB")
        data = stream.read(info.st_size + 1)
        if len(data) != info.st_size or os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns:
            raise ValueError("读取期间附件已修改，请重试")
    return notifications.FileSnapshot(name, mime, data)


def _snapshots(sources: list[tuple[Path, Path, str, str]]) -> list[notifications.FileSnapshot]:
    remaining = notifications.MAX_TOTAL_BYTES
    files = []
    for root, relative, name, mime in sources:
        try:
            file = _snapshot(root, relative, name, mime, remaining)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"无法打开附件「{name}」") from exc
        remaining -= len(file.data)
        files.append(file)
    return files


def notify_user_tool(
    store: SQLiteStore,
    conversation_id: str,
    bindings: Sequence["HTTPTerminalSettings"],
    workspace: Path,
    resolve_history: Callable[[SQLiteStore, str, str, str], "HistoryAttachment"],
) -> Tool[NotifyRequest]:
    user = store.get_conversation_user(conversation_id)
    targets = {
        b.terminal_id: b.display_name or b.terminal_id
        for b in bindings
        if b.user_id == user and b.conversation_id == conversation_id
    }
    root = workspace.resolve()

    async def execute(args: NotifyRequest, context: ToolContext) -> ToolResult:
        if args.operation == "list_targets":
            items = [{"terminal_id": terminal, "name": name} for terminal, name in targets.items()]
            return ToolResult(json.dumps({"targets": items}, ensure_ascii=False))
        try:
            if not set(args.targets) <= targets.keys():
                raise ValueError("目标不在本会话的接收终端中，请先查询 list_targets")
            if not context.run_id:
                raise ValueError("发送通知需要已登记的主 Agent 运行")
            key = json.dumps([context.run_id, context.call_id], separators=(",", ":"))
            fields = args.model_dump(mode="json")
            fields["targets"] = sorted(args.targets)
            fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
            id = store.notifications.previous_user_notification(
                user_id=user,
                conversation_id=conversation_id,
                request_key=key,
                fingerprint=fingerprint,
            )
            if id is None:
                sources = []
                for ref in args.attachments:
                    if isinstance(ref, str):
                        relative = _relative(ref)
                        mime = (
                            _STANDARD_MIME.guess_type(relative.name)[0]
                            or mimetypes.guess_type(relative.name)[0]
                            or "application/octet-stream"
                        )
                        sources.append((root, relative, relative.name, mime))
                    else:
                        media = resolve_history(store, user, conversation_id, ref.history_file_id)
                        sources.append(
                            (
                                media.path.parent,
                                Path(media.path.name),
                                media.name,
                                media.content_type,
                            )
                        )
                files = await asyncio.to_thread(_snapshots, sources) if sources else []
                id = store.notifications.send_user_notification(
                    user_id=user,
                    conversation_id=conversation_id,
                    targets=tuple(args.targets),
                    title=args.title,
                    body=args.body,
                    expires_at=args.expires_at,
                    files=files,
                    request_key=key,
                    fingerprint=fingerprint,
                )
            return ToolResult(
                json.dumps(
                    {
                        "id": id,
                        "targets": args.targets,
                        "status": "queued",
                        "attachment_count": len(args.attachments),
                    },
                    ensure_ascii=False,
                )
            )
        except (ValueError, LookupError) as exc:
            return ToolResult(str(exc), is_error=True)

    return Tool(
        "notify_user",
        "向本会话的一个或多个设备发送用户提醒。list_targets 查询设备名称和 terminal_id。"
        "send 提交标题、正文和可选文件。可以附上工作目录中的文件，或使用后端历史附件 ID。"
        "成功表示已加入各目标收件箱。文件在发送时保存快照，用户点开后下载。",
        NotifyRequest,
        execute,
    )
