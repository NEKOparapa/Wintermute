"""A user-scoped Markdown vault with atomic writes, audit and shared mutation locks."""

import asyncio
import hashlib
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path, PurePosixPath, PureWindowsPath
from time import monotonic
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4
from weakref import WeakKeyDictionary, WeakValueDictionary
from zoneinfo import ZoneInfo

from purin.memory.audit import audit_entry
from purin.memory.frontmatter import parse_frontmatter
from purin.memory.regex_search import RegexMatcher
from purin.memory.security import (
    AREAS,
    MAX_FILE_BYTES,
    MemoryActor,
    check_write_permission,
    contains_secret,
    validate_content,
)

_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, WeakValueDictionary[str, asyncio.Lock]] = (
    WeakKeyDictionary()
)
_thread_locks: dict[str, threading.RLock] = {}
_thread_registry_lock = threading.Lock()
_READ_BYTES = 32 * 1024
_SEARCH_SECONDS = 5.0
_DEFAULT_TEMPLATE_PATHS = ("agent-character/character.md", "about-user/profile.md")

if TYPE_CHECKING:
    from purin.memory.frontmatter import MemoryMetadata


class MemoryConflictError(ValueError):
    """A conditional write observed a newer document version."""


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _search_remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise ValueError("Memory search exceeded its 5 second time limit; narrow the scope")
    return remaining


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _atomic_write(path: Path, data: bytes, *, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if overwrite and path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=".memory-", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(mode)
        if overwrite:
            os.replace(staged, path)
        elif os.name == "nt":
            os.rename(staged, path)
        else:
            # A POSIX rename can replace an existing destination.
            os.link(staged, path)
    finally:
        staged.unlink(missing_ok=True)


async def _settled_io[T](operation: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class MemorySearcher(Protocol):
    def search(
        self,
        vault: "MemoryVault",
        query: str,
        *,
        scope: str,
        regex: bool,
        include_archived: bool,
        max_results: int,
        context_lines: int,
    ) -> list[dict[str, object]]: ...


class TextMemorySearcher:
    def search(
        self,
        vault: "MemoryVault",
        query: str,
        *,
        scope: str,
        regex: bool,
        include_archived: bool,
        max_results: int,
        context_lines: int,
    ) -> list[dict[str, object]]:
        if scope not in {"all", *AREAS}:
            raise ValueError("Invalid search scope")
        if not 1 <= len(query) <= 200 or not 1 <= max_results <= 50:
            raise ValueError("Invalid search query or result limit")
        if not 0 <= context_lines <= 5:
            raise ValueError("Invalid search context limit")
        deadline = monotonic() + _SEARCH_SECONDS
        results: list[dict[str, object]] = []
        today = datetime.now(vault.zone).date()
        with RegexMatcher(query) if regex else nullcontext() as matcher:
            files = vault.list_files(
                "" if scope == "all" else scope, max_depth=5, _deadline=deadline
            )
            _search_remaining(deadline)
            for item in files:
                _search_remaining(deadline)
                if item["is_dir"]:
                    continue
                path = str(item["path"])
                try:
                    text = vault.read_text(path)
                except (OSError, ValueError, UnicodeError):
                    continue
                if not include_archived and path.startswith("note/"):
                    metadata = vault.metadata(path, text)
                    if (
                        path.startswith("note/archive/")
                        or metadata.status == "archived"
                        or metadata.expired(today)
                    ):
                        continue
                lines = text.splitlines()
                remaining = _search_remaining(deadline)
                matches = (
                    matcher.matches(
                        text, max_results - len(results), timeout_seconds=remaining
                    )
                    if matcher is not None
                    else [
                        number
                        for number, line in enumerate(lines)
                        if query.casefold() in line.casefold()
                    ]
                )
                _search_remaining(deadline)
                for number in matches:
                    start = max(0, number - context_lines)
                    end = min(len(lines), number + context_lines + 1)
                    results.append(
                        {
                            "path": path,
                            "line": number + 1,
                            "start_line": start + 1,
                            "content": "\n".join(lines[start:end])[:4096],
                        }
                    )
                    if len(results) == max_results:
                        return results
        _search_remaining(deadline)
        return results


class MemoryVault:
    def __init__(
        self,
        root: Path,
        *,
        timezone: str = "Asia/Shanghai",
        git_snapshot_enabled: bool = False,
        searcher: MemorySearcher | None = None,
    ) -> None:
        self.root = root.absolute()
        self.zone = ZoneInfo(timezone)
        self.git_snapshot_enabled = git_snapshot_enabled
        self.searcher = searcher or TextMemorySearcher()
        self._key = os.path.normcase(str(self.root))
        with _thread_registry_lock:
            self._thread_lock = _thread_locks.setdefault(self._key, threading.RLock())

    def _check_root(self) -> None:
        for part in (self.root, *self.root.parents):
            if _is_link(part):
                raise ValueError("Memory roots and ancestors must not be symlinks or junctions")
        if self.root.resolve() != self.root:
            raise ValueError("Memory root must be a canonical path")

    def initialize(self) -> None:
        with self._thread_lock:
            self._check_root()
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            for area in (*AREAS, ".trash"):
                directory = self.root / area
                if _is_link(directory):
                    raise ValueError("Memory areas must not be symlinks or junctions")
                directory.mkdir(mode=0o700, exist_ok=True)

    def initialize_templates(self) -> None:
        """Seed empty areas once, including for vaults created before templates existed."""
        with self._thread_lock:
            self._check_root()
            initialized = self.root / ".templates-initialized"
            if initialized.exists():
                return
            templates = files(__package__).joinpath("templates")
            for path in _DEFAULT_TEMPLATE_PATHS:
                area, name = path.split("/")
                if any(not item["is_dir"] for item in self.list_files(area, max_depth=5)):
                    continue
                target = self.resolve(area, directory=True) / name
                content = templates.joinpath(area, name).read_text(encoding="utf-8")
                try:
                    _atomic_write(target, content.encode("utf-8"), overwrite=False)
                except FileExistsError:
                    pass
            # Only record completion after all areas have been handled, so startup can retry.
            try:
                initialized.touch(mode=0o600, exist_ok=False)
            except FileExistsError:
                pass

    def resolve(self, path: str, *, directory: bool = False) -> Path:
        self._check_root()
        if path == "" and directory:
            return self.root
        if (
            not path
            or "\\" in path
            or PureWindowsPath(path).drive
            or PurePosixPath(path).is_absolute()
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
        ):
            raise ValueError("Expected a safe relative POSIX memory path")
        parts = path.split("/")
        if parts[0] not in AREAS or any(
            not part or part.startswith(".") or ":" in part or part.endswith((" ", "."))
            for part in parts
        ):
            raise ValueError("Hidden files, traversal and unknown memory areas are forbidden")
        if any(PureWindowsPath(part).is_reserved() for part in parts):
            raise ValueError("Reserved device names are forbidden")
        if not directory and (len(parts) < 2 or not path.endswith(".md")):
            raise ValueError("Memory files must have the .md extension")
        if len(parts) > (4 if directory else 5):
            raise ValueError("Memory paths may have at most three nested directories")
        candidate = self.root.joinpath(*parts)
        current = self.root
        for part in parts:
            current /= part
            if _is_link(current):
                raise ValueError("Memory paths must not contain symlinks or junctions")
        if not candidate.is_relative_to(self.root) or not candidate.resolve().is_relative_to(
            self.root.resolve()
        ):
            raise ValueError("Memory path escapes its root")
        return candidate

    def read_text(self, path: str) -> str:
        with self._thread_lock:
            target = self.resolve(path)
            if not target.is_file():
                raise FileNotFoundError(path)
            limit = 8 * 1024 * 1024 if path.startswith("log/") else MAX_FILE_BYTES
            with target.open("rb") as source:
                raw = source.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("Memory file exceeds its read limit")
            if b"\x00" in raw:
                raise ValueError("Memory file contains binary content")
            return raw.decode("utf-8-sig")

    def metadata(self, path: str, text: str | None = None) -> "MemoryMetadata":
        from purin.memory.frontmatter import MemoryMetadata

        target = self.resolve(path)
        modified = datetime.fromtimestamp(target.stat().st_mtime, self.zone).date()
        content = self.read_text(path) if text is None else text
        try:
            return parse_frontmatter(content, path=path, modified=modified)
        except (ValueError, UnicodeError):
            # A hand-edited malformed header must not break the entire prompt or file browser.
            return MemoryMetadata(PurePosixPath(path).stem, "[Frontmatter 格式无效]", modified)

    def list_files(
        self,
        path: str = "",
        *,
        recursive: bool = True,
        max_depth: int = 3,
        _deadline: float | None = None,
    ) -> list[dict[str, object]]:
        if not 1 <= max_depth <= 5:
            raise ValueError("max_depth must be between 1 and 5")
        with self._thread_lock:
            parent = self.resolve(path, directory=True)
            if not parent.exists():
                return []
            if not parent.is_dir():
                raise ValueError("list path must be a directory")
            result: list[dict[str, object]] = []

            def visit(directory: Path, depth: int) -> None:
                for child in sorted(directory.iterdir(), key=lambda p: p.name):
                    if _deadline is not None:
                        _search_remaining(_deadline)
                    if child.name.startswith(".") or _is_link(child):
                        continue
                    relative = child.relative_to(self.root).as_posix()
                    try:
                        self.resolve(relative, directory=child.is_dir())
                    except ValueError:
                        continue
                    info = child.stat()
                    item: dict[str, object] = {
                        "path": relative,
                        "is_dir": child.is_dir(),
                        "bytes": 0 if child.is_dir() else info.st_size,
                        "updated_at": datetime.fromtimestamp(info.st_mtime, self.zone).isoformat(),
                    }
                    if child.is_file() and relative.startswith("note/"):
                        try:
                            metadata = self.metadata(relative)
                            item.update(title=metadata.title, summary=metadata.summary)
                        except (OSError, ValueError, UnicodeError):
                            item.update(title=child.stem, summary="[文件不可读]")
                    result.append(item)
                    if child.is_dir() and recursive and depth < max_depth:
                        visit(child, depth + 1)

            visit(parent, 1)
            return result

    def read(
        self,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, object]:
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise ValueError("Invalid line range")
        text = self.read_text(path)
        lines = text.splitlines()
        if start_line > max(1, len(lines)):
            raise ValueError("start_line is beyond the end of the file")
        limit = min(end_line if end_line is not None else len(lines), len(lines))
        parts: list[str] = []
        used = 0
        next_line: int | None = None
        truncated_line: int | None = None
        for offset in range(start_line - 1, limit):
            rendered = f"{offset + 1}: {lines[offset]}\n"
            size = len(rendered.encode("utf-8"))
            if used + size > _READ_BYTES:
                if not parts:
                    parts.append(
                        rendered.encode("utf-8")[:_READ_BYTES].decode("utf-8", errors="ignore")
                    )
                    truncated_line = offset + 1
                    next_line = offset + 2 if offset + 1 < len(lines) else None
                    break
                next_line = offset + 1
                break
            parts.append(rendered)
            used += size
        return {
            "path": path,
            "content": "".join(parts),
            "total_lines": len(lines),
            "content_sha256": content_hash(text),
            "truncated": next_line is not None or truncated_line is not None,
            "next_line": next_line,
            "truncated_line": truncated_line,
            "notice": (
                "该行超过 32 KiB，仅显示开头；请在记忆文件编辑器中查看该行全文。"
                if truncated_line is not None
                else None
            ),
        }

    def search(
        self,
        query: str,
        *,
        scope: str = "all",
        regex: bool = False,
        include_archived: bool = True,
        max_results: int = 20,
        context_lines: int = 2,
    ) -> list[dict[str, object]]:
        with self._thread_lock:
            return self.searcher.search(
                self,
                query,
                scope=scope,
                regex=regex,
                include_archived=include_archived,
                max_results=max_results,
                context_lines=context_lines,
            )

    def _lock(self) -> asyncio.Lock:
        entries = _locks.setdefault(asyncio.get_running_loop(), WeakValueDictionary())
        lock = entries.get(self._key)
        if lock is None:
            lock = asyncio.Lock()
            entries[self._key] = lock
        return lock

    async def write(
        self,
        action: str,
        path: str,
        *,
        actor: MemoryActor,
        reason: str,
        channel: str = "conversation",
        allow_character_write: bool = False,
        expected_hash: str | None = None,
        content: str = "",
        old_text: str = "",
        new_text: str = "",
        position: str = "append",
        anchor: str | None = None,
        new_path: str | None = None,
    ) -> dict[str, object]:
        async with self._lock():
            return await _settled_io(
                lambda: self._write(
                    action,
                    path,
                    actor=actor,
                    reason=reason,
                    channel=channel,
                    allow_character_write=allow_character_write,
                    expected_hash=expected_hash,
                    content=content,
                    old_text=old_text,
                    new_text=new_text,
                    position=position,
                    anchor=anchor,
                    new_path=new_path,
                )
            )

    def _write(
        self,
        action: str,
        path: str,
        *,
        actor: MemoryActor,
        reason: str,
        channel: str = "conversation",
        allow_character_write: bool = False,
        expected_hash: str | None = None,
        content: str = "",
        old_text: str = "",
        new_text: str = "",
        position: str = "append",
        anchor: str | None = None,
        new_path: str | None = None,
    ) -> dict[str, object]:
        with self._thread_lock:
            if action not in {"create", "replace", "insert", "delete", "rename", "save"}:
                raise ValueError("Unknown memory write action")
            if not 2 <= len(reason.strip()) <= 300 or contains_secret(reason):
                raise ValueError("A non-secret reason of 2 to 300 characters is required")
            if any(ord(char) < 32 for char in channel) or len(channel) > 100:
                raise ValueError("Invalid memory channel")
            self.initialize()
            target = self.resolve(path)
            check_write_permission(
                path, actor=actor, channel=channel, action=action,
                allow_character_write=allow_character_write,
            )
            if action == "save" and actor != "human_api":
                raise PermissionError("Full-file saving is only available to the human API")
            existed = target.exists()
            if action == "create" and existed:
                raise FileExistsError(path)
            if action not in {"create", "save"} and not existed:
                raise FileNotFoundError(path)
            before = self.read_text(path) if existed else ""
            if expected_hash is not None and (not existed or content_hash(before) != expected_hash):
                raise MemoryConflictError("Memory file changed; read it again before writing")
            after = content
            destination: Path | None = None
            detail = ""
            if action == "replace":
                if (
                    not old_text
                    or before.find(old_text) < 0
                    or before.find(old_text, before.find(old_text) + 1) >= 0
                ):
                    raise ValueError("old_text must match exactly once")
                after = before.replace(old_text, new_text, 1)
            elif action == "insert":
                after = self._insert(before, content, position, anchor)
            elif action in {"rename", "delete"}:
                if action == "rename":
                    if new_path is None:
                        raise ValueError("rename requires new_path")
                    destination = self.resolve(new_path)
                    check_write_permission(
                        new_path, actor=actor, channel=channel, action=action,
                        allow_character_write=allow_character_write,
                    )
                    if destination.exists():
                        raise FileExistsError(new_path)
                    detail = f"目标：{new_path}"
                else:
                    trash = self.root / ".trash"
                    if _is_link(trash):
                        raise ValueError("Trash must not be a symlink or junction")
                    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                    destination = trash / f"{stamp}__{uuid4().hex[:8]}__{path.replace('/', '__')}"
                    detail = f"备份：{destination.name}；标题：{self.metadata(path, before).title}"
                after = ""
            else:
                after = content
            if action not in {"delete", "rename"}:
                validate_content(after)
            now = datetime.now(self.zone)
            log = self.resolve(f"log/{now:%Y-%m-%d}.md")
            entry = audit_entry(
                now=now,
                actor=actor,
                channel=channel,
                action=action,
                path=path,
                reason=reason,
                before=before,
                after=after,
                detail=detail,
            )
            if destination is not None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, destination)
            else:
                _atomic_write(target, after.encode("utf-8"))
            try:
                # The shared vault lock also serializes the daily audit across instances/loops.
                with log.open("ab") as output:
                    checkpoint = output.tell()
                    try:
                        output.write(entry.encode("utf-8"))
                        output.flush()
                        os.fsync(output.fileno())
                    except BaseException:
                        output.truncate(checkpoint)
                        raise
            except BaseException:
                if destination is not None:
                    os.replace(destination, target)
                elif existed:
                    _atomic_write(target, before.encode("utf-8"))
                else:
                    target.unlink(missing_ok=True)
                raise
            return {
                "path": new_path if action == "rename" else path,
                "action": action,
                "content_sha256": content_hash(before if action == "rename" else after),
                "backup": destination.name if action == "delete" and destination else None,
            }

    @staticmethod
    def _insert(before: str, content: str, position: str, anchor: str | None) -> str:
        if not content:
            raise ValueError("insert content must not be empty")
        if position == "append":
            return before + content
        if position == "prepend":
            match = re.match(r"\A(?:\ufeff)?---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)", before)
            offset = match.end() if match else 0
        elif position in {"after_heading", "after_text"}:
            if not anchor:
                raise ValueError("This insertion position requires anchor")
            if position == "after_heading":
                matches = [
                    match
                    for match in re.finditer(r"(?m)^#{1,6}\s+.+$", before)
                    if match.group().strip() == anchor.strip()
                    or re.sub(r"^#{1,6}\s+", "", match.group()).strip() == anchor.strip()
                ]
                if len(matches) != 1:
                    raise ValueError("Heading anchor must match exactly once")
                offset = matches[0].end()
                if before[offset : offset + 1] == "\n":
                    offset += 1
            else:
                offset = before.find(anchor)
                if offset < 0 or before.find(anchor, offset + 1) >= 0:
                    raise ValueError("Text anchor must match exactly once")
                offset += len(anchor)
        else:
            raise ValueError("Invalid insertion position")
        return before[:offset] + content + before[offset:]

    def retire_generated_activity(self) -> None:
        """Retire the program-generated profile when upgrading an existing vault."""
        with self._thread_lock:
            path = self.resolve("about-user/activity.md")
            if not path.is_file():
                return
            with path.open("rb") as source:
                prefix = source.read(128).replace(b"\r\n", b"\n")
            if prefix.startswith("## 常用时间段\n- 统计时区：".encode()):
                trash = self.root / ".trash"
                if _is_link(trash):
                    raise ValueError("Trash must not be a symlink or junction")
                path.rename(trash / f"{uuid4().hex}__about-user__activity.md")

    async def snapshot(self, reason: str) -> None:
        """Opt-in local snapshots only; never operate on an ancestor repository or push."""
        if not self.git_snapshot_enabled:
            return
        async with self._lock():
            self.initialize()
            git_dir = self.root / ".git"
            if _is_link(git_dir) or (git_dir.exists() and not git_dir.is_dir()):
                raise ValueError("Snapshots require a local .git directory")

            async def git(*arguments: str) -> str:
                env = {
                    key: value for key, value in os.environ.items() if not key.startswith("GIT_")
                }
                process = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    str(self.root),
                    "-c",
                    "core.hooksPath=" + str(git_dir / "no-hooks"),
                    "-c",
                    "user.name=Purin",
                    "-c",
                    "user.email=memory@localhost",
                    "-c",
                    "commit.gpgSign=false",
                    *arguments,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                try:
                    async with asyncio.timeout(30):
                        output, _ = await process.communicate()
                except BaseException:
                    process.kill()
                    await process.wait()
                    raise
                if process.returncode:
                    raise RuntimeError("Local memory snapshot failed")
                return output.decode("utf-8", errors="replace")

            if not git_dir.exists():
                await git("init")
            top = (await git("rev-parse", "--show-toplevel")).strip()
            matches_root = await asyncio.to_thread(
                lambda: Path(top).resolve() == self.root.resolve()
            )
            if not matches_root:
                raise ValueError("Snapshot repository must match the memory root")
            await git("add", "-A", "--", *AREAS)
            if (await git("diff", "--cached", "--name-only")).strip():
                await git("commit", "-m", "Memory snapshot: " + " ".join(reason.split())[:160])
