"""Queue device notifications and asynchronous questions for the conversation."""

import asyncio
import hashlib
import json
import mimetypes
import os
import stat
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from purin.agent import Tool, ToolContext, ToolResult
from purin.agent.tools.devices.parameters import NOTIFY_FIELDS, DeviceMessagingRequest
from purin.events.storage import notifications
from purin.events.storage.sqlite import SQLiteStore

if TYPE_CHECKING:
    from purin.events.core.attachments import HistoryAttachment

# Prefer standard types to machine-specific overrides such as Excel claiming .csv on Windows.
_STANDARD_MIME = mimetypes.MimeTypes()


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


def device_messaging_tool(
    store: SQLiteStore,
    user: str,
    conversation_id: str,
    notification_targets: Callable[[], frozenset[str]],
    workspace: Path,
    resolve_history: Callable[[SQLiteStore, str, str, str], "HistoryAttachment"],
    reply_device_id: Callable[[], str | None],
) -> Tool[DeviceMessagingRequest]:
    root = workspace.resolve()

    async def execute(args: DeviceMessagingRequest, context: ToolContext) -> ToolResult:
        try:
            if not context.run_id:
                raise ValueError("发送设备消息需要已登记的主 Agent 运行")
            if args.action == "ask":
                item = store.questions.create(
                    user_id=user,
                    conversation_id=conversation_id,
                    run_id=context.run_id,
                    call_id=context.call_id,
                    title=args.title,
                    question=args.question,
                    options=args.options,
                    multi_select=args.multi_select,
                    allow_custom=args.allow_custom,
                    reply_device_id=reply_device_id(),
                )
                return ToolResult(
                    json.dumps(
                        {
                            "id": item.id,
                            "status": item.status,
                            "title": item.title,
                            "options": item.options,
                            "multi_select": item.multi_select,
                            "allow_custom": item.allow_custom,
                            "message": "提问已发至本会话各设备，用户回答或忽略后会自动通知你。",
                        },
                        ensure_ascii=False,
                    )
                )

            assert args.notify_at is not None
            key = json.dumps([context.run_id, context.call_id], separators=(",", ":"))
            fields = args.model_dump(mode="json", include={"title", *NOTIFY_FIELDS})
            fields["targets"] = sorted(args.targets)
            fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
            receipt = store.schedules.previous_notification(user, conversation_id, key, fingerprint)
            if receipt is None:
                if not notification_targets().issuperset(args.targets):
                    raise ValueError("请从 device_statuses 中选择 can_notify 为 true 的设备 ID")
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
                receipt = store.schedules.schedule_notification(
                    user_id=user,
                    conversation_id=conversation_id,
                    targets=tuple(args.targets),
                    title=args.title,
                    notify_at=args.notify_at,
                    body=args.body,
                    expires_at=args.expires_at,
                    files=files,
                    request_key=key,
                    fingerprint=fingerprint,
                )
            return ToolResult(
                json.dumps(
                    {
                        **receipt,
                        "targets": args.targets,
                        "attachment_count": len(args.attachments),
                    },
                    ensure_ascii=False,
                )
            )
        except (ValueError, LookupError) as exc:
            return ToolResult(str(exc), is_error=True)

    return Tool(
        "device_messaging",
        "notify 必须提供含时区的 notify_at，将通知加入时间表，到点自动投递设备收件箱。"
        "目标 ID 从 device_statuses 获取。返回 schedule_id 可改期或取消，投递结果进入通知层。"
        "ask 向本会话各设备发送异步提问，提供标题和 question，可附选项或允许自定义回答。"
        "提问立即返回，任一设备回答或忽略后会自动通知主 Agent。",
        DeviceMessagingRequest,
        execute,
    )
