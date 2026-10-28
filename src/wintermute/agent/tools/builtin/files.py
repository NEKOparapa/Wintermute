"""本地 UTF-8 文件工具，支持输出限制和串行原子修改。"""

import asyncio
import codecs
import difflib
import os
import re
import stat
import tempfile
from bisect import bisect_left
from collections.abc import Callable, Iterator
from pathlib import Path
from weakref import WeakKeyDictionary, WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field

from wintermute.agent import Tool, ToolContext, ToolResult

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
DEFAULT_MAX_EDIT_BYTES = 10 * 1024 * 1024
_SKIP_CHUNK_BYTES = 64 * 1024
_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, WeakValueDictionary[str, asyncio.Lock]] = (
    WeakKeyDictionary()
)


class _FileInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    path: str = Field(
        min_length=1, description="File path, relative to cwd or absolute; ~ is expanded"
    )


class ReadInput(_FileInput):
    offset: int = Field(default=1, ge=1, description="First line to read, starting from 1")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines to read")


class WriteInput(_FileInput):
    content: str = Field(description="Complete UTF-8 content; replaces any existing file")


class TextEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    old_text: str = Field(min_length=1, description="Exact, unique text in the original file")
    new_text: str = Field(description="Replacement text; may be empty to delete the match")


class EditInput(_FileInput):
    edits: list[TextEdit] = Field(
        min_length=1,
        description="Non-overlapping replacements, all matched against the original file",
    )


def _positive_limit(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _working_directory(cwd: str | Path) -> Path:
    directory = Path(cwd).expanduser().resolve()
    if not directory.is_dir():
        raise NotADirectoryError(f"Working directory does not exist or is not a directory: {cwd}")
    return directory


def _resolve_path(cwd: Path, path: str) -> Path:
    candidate = Path(path).expanduser()
    return (candidate if candidate.is_absolute() else cwd / candidate).resolve()


def _require_file(path: Path) -> os.stat_result:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Expected a regular file: {path}")
    return info


def _mutation_lock(path: Path) -> asyncio.Lock:
    """让同一事件循环中操作相同真实路径的工厂和 Runner 共享修改锁。"""
    loop = asyncio.get_running_loop()
    loop_locks = _locks.setdefault(loop, WeakValueDictionary())
    key = os.path.normcase(str(path))
    lock = loop_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        loop_locks[key] = lock
    return lock


async def _settled_io[T](operation: Callable[[], T]) -> T:
    """取消调用后，等待仍可能写入的工作线程结束，再允许释放修改锁。"""
    worker = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # 取出并发发生的 I/O 异常，但仍向调用方报告取消。
        if not worker.cancelled():
            worker.exception()
        raise


def _decode_text(raw: bytes) -> str:
    if b"\x00" in raw:
        raise ValueError("Binary files are unsupported; expected UTF-8 text")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("File is not valid UTF-8 text") from exc


def _read_file(path: Path, offset: int, limit: int, max_bytes: int) -> str:
    _require_file(path)
    parts: list[str] = []
    used = 0
    with path.open("rb") as stream:
        if stream.read(3) != codecs.BOM_UTF8:
            stream.seek(0)
        # 按固定大小分块跳过前面的长行，避免解码或整行载入内存。
        current = 1
        while current < offset:
            chunk = stream.readline(_SKIP_CHUNK_BYTES)
            if not chunk:
                raise ValueError(f"offset={offset} is beyond the end of the file")
            if chunk.endswith(b"\n"):
                current += 1
        for _ in range(limit):
            raw = stream.readline(max_bytes - used + 1)
            if not raw:
                if not parts and offset > 1:
                    raise ValueError(f"offset={offset} is beyond the end of the file")
                return "".join(parts)
            if len(raw) > max_bytes - used:
                if not parts:
                    raise ValueError(
                        f"Line {offset} exceeds the {max_bytes}-byte read limit. "
                        "Use exec to inspect a smaller part of this line."
                    )
                break
            parts.append(_decode_text(raw))
            used += len(raw)
        else:
            if not stream.read(1):
                return "".join(parts)
    end = offset + len(parts) - 1
    content = "".join(parts)
    separator = "\n" if content.endswith("\n") else "\n\n"
    return (
        content
        + separator
        + (f"[Showing lines {offset}-{end}; output limited. Continue with offset={end + 1}.]")
    )


def _atomic_write(path: Path, content: bytes) -> None:
    """完整写入成功后才替换目标文件，并保留已有文件的权限模式。"""
    existing: os.stat_result | None = None
    if path.exists():
        existing = _require_file(path)
        if not existing.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError(f"File is read-only: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if existing is not None:
            temporary_path.chmod(stat.S_IMODE(existing.st_mode))
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _bounded_diff(lines: Iterator[str], max_lines: int, max_bytes: int) -> str:
    parts: list[str] = []
    used = 0
    count = 0
    truncated = False
    for line in lines:
        if not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        raw = line.encode("utf-8")
        line_count = line.count("\n")
        if count + line_count > max_lines or used + len(raw) > max_bytes:
            truncated = True
            break
        parts.append(line)
        used += len(raw)
        count += line_count
    if truncated:
        parts.append("[Diff output truncated; use read to inspect the updated file.]\n")
    return "".join(parts)


def _edit_file(
    path: Path, edits: list[TextEdit], max_file_bytes: int, max_lines: int, max_bytes: int
) -> str:
    _require_file(path)
    with path.open("rb") as stream:
        raw = stream.read(max_file_bytes + 1)
    if len(raw) > max_file_bytes:
        raise ValueError(f"File exceeds the {max_file_bytes}-byte edit limit")
    bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
    original = _decode_text(raw[len(bom) :])
    normalized = original.replace("\r\n", "\n")
    # 将规范化后的索引映射回原文位置，保留未修改区域的原始换行符。
    removed = [match.start() - i for i, match in enumerate(re.finditer("\r\n", original))]
    first_newline = re.search("\r?\n", original)
    newline = first_newline.group() if first_newline else "\n"
    matches: list[tuple[int, int, str]] = []
    for index, edit in enumerate(edits, 1):
        old = edit.old_text.replace("\r\n", "\n")
        start = normalized.find(old)
        if start == -1:
            raise ValueError(f"Edit {index}: old_text was not found in the original file")
        if normalized.find(old, start + 1) != -1:
            raise ValueError(f"Edit {index}: old_text must match exactly once; add more context")
        end = start + len(old)
        replacement = edit.new_text.replace("\r\n", "\n").replace("\n", newline)
        matches.append((start, end, replacement))
    matches.sort()
    for previous, following in zip(matches, matches[1:], strict=False):
        if following[0] < previous[1]:
            raise ValueError("Edits overlap in the original file; combine them into one edit")
    pieces: list[str] = []
    cursor = 0
    for start, end, replacement in matches:
        original_start = start + bisect_left(removed, start)
        original_end = end + bisect_left(removed, end)
        pieces.extend((original[cursor:original_start], replacement))
        cursor = original_end
    pieces.append(original[cursor:])
    updated = "".join(pieces)
    if updated == original:
        raise ValueError("Edits would not change the file")
    diff = _bounded_diff(
        difflib.unified_diff(
            normalized.splitlines(keepends=True),
            updated.replace("\r\n", "\n").splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        ),
        max_lines,
        max_bytes,
    )
    _atomic_write(path, bom + updated.encode("utf-8"))
    return diff


def create_read_tool(
    cwd: str | Path, *, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = DEFAULT_MAX_BYTES
) -> Tool[ReadInput]:
    """创建限制输出大小的文本读取工具；相对路径以 cwd 为基准，访问范围不限于此。"""
    directory = _working_directory(cwd)
    _positive_limit("max_lines", max_lines)
    _positive_limit("max_bytes", max_bytes)

    async def execute(arguments: ReadInput, context: ToolContext) -> ToolResult:
        path = await asyncio.to_thread(_resolve_path, directory, arguments.path)
        limit = min(arguments.limit or max_lines, max_lines)
        content = await asyncio.to_thread(_read_file, path, arguments.offset, limit, max_bytes)
        return ToolResult(content)

    return Tool(
        "read",
        f"Read a UTF-8 text file. Output is limited to {max_lines} lines or {max_bytes} bytes. "
        "Use 1-based offset and limit to page through large files. Binary files are unsupported.",
        ReadInput,
        execute,
    )


def create_write_tool(cwd: str | Path) -> Tool[WriteInput]:
    """创建 UTF-8 写入工具，自动创建父目录并原子替换目标文件。"""
    directory = _working_directory(cwd)

    async def execute(arguments: WriteInput, context: ToolContext) -> ToolResult:
        path = await asyncio.to_thread(_resolve_path, directory, arguments.path)
        async with _mutation_lock(path):
            await _settled_io(lambda: _atomic_write(path, arguments.content.encode("utf-8")))
        return ToolResult(
            f"Wrote {len(arguments.content.encode('utf-8'))} bytes to {arguments.path}"
        )

    return Tool(
        "write",
        "Create or overwrite a UTF-8 text file, creating parent directories when needed. "
        "Use for new files or complete rewrites; use edit for targeted changes.",
        WriteInput,
        execute,
    )


def create_edit_tool(
    cwd: str | Path,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_file_bytes: int = DEFAULT_MAX_EDIT_BYTES,
) -> Tool[EditInput]:
    """创建批量精确文本编辑工具，所有匹配均基于原始文件。"""
    directory = _working_directory(cwd)
    for name, value in (
        ("max_lines", max_lines),
        ("max_bytes", max_bytes),
        ("max_file_bytes", max_file_bytes),
    ):
        _positive_limit(name, value)

    async def execute(arguments: EditInput, context: ToolContext) -> ToolResult:
        path = await asyncio.to_thread(_resolve_path, directory, arguments.path)
        async with _mutation_lock(path):
            diff = await _settled_io(
                lambda: _edit_file(path, arguments.edits, max_file_bytes, max_lines, max_bytes)
            )
        return ToolResult(f"Edited {arguments.path}\n{diff}")

    return Tool(
        "edit",
        "Edit a UTF-8 file with exact text replacements. Each edits[].old_text must occur "
        "exactly once in the original file; edits must not overlap. CRLF/LF match equivalently, "
        "and untouched text and UTF-8 BOM are preserved. Returns a bounded unified diff. "
        f"Files larger than {max_file_bytes} bytes are rejected.",
        EditInput,
        execute,
    )
