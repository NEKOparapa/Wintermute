"""Deterministic code-generated note discovery and a bounded prompt index."""

from dataclasses import dataclass
from datetime import date, datetime

from purin.memory.frontmatter import MemoryMetadata
from purin.memory.security import redact
from purin.memory.vault import MemoryVault


@dataclass(frozen=True)
class MemoryIndexEntry:
    path: str
    metadata: MemoryMetadata

    @property
    def title(self) -> str:
        return self.metadata.title

    @property
    def summary(self) -> str:
        return self.metadata.summary

    @property
    def updated_at(self) -> date:
        return self.metadata.updated_at


def scan_index(vault: MemoryVault, *, today: date | None = None) -> tuple[MemoryIndexEntry, ...]:
    today = today or datetime.now(vault.zone).date()
    entries: list[MemoryIndexEntry] = []
    for item in vault.list_files("note", max_depth=5):
        path = str(item["path"])
        if item["is_dir"] or path.startswith("note/archive/"):
            continue
        try:
            metadata = vault.metadata(path)
        except (OSError, ValueError, UnicodeError):
            continue
        if metadata.status == "archived" or metadata.expired(today):
            continue
        entries.append(MemoryIndexEntry(path, metadata))
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _inline(text: str, limit: int) -> str:
    return " ".join(redact(text).replace("`", "'").replace("|", "／").split())[:limit]


def format_index(
    entries: tuple[MemoryIndexEntry, ...],
    *,
    max_bytes: int = 12 * 1024,
    max_entries: int = 200,
) -> str:
    if max_bytes < 1 or max_entries < 1:
        raise ValueError("Index budgets must be positive")
    lines = {
        entry.path: (
            f"- `{entry.path}` | {_inline(entry.title, 120)} | "
            f"{_inline(entry.summary, 240)} (更新: {entry.updated_at})\n"
        )
        for entry in entries
    }
    total_bytes = sum(len(line.encode()) for line in lines.values())
    if len(entries) <= max_entries and total_bytes <= max_bytes:
        return "".join(lines[entry.path] for entry in sorted(entries, key=lambda item: item.path))
    omitted_note = f'[已省略 {len(entries)} 条笔记，可用 memory_read(action="search") 查找。]\n'
    available = max(0, max_bytes - len(omitted_note.encode("utf-8")))
    kept: list[MemoryIndexEntry] = []
    used = 0
    for entry in sorted(entries, key=lambda item: (-item.updated_at.toordinal(), item.path)):
        size = len(lines[entry.path].encode("utf-8"))
        if len(kept) < max_entries and used + size <= available:
            kept.append(entry)
            used += size
    omitted_note = (
        f'[已省略 {len(entries) - len(kept)} 条笔记，可用 memory_read(action="search") 查找。]\n'
    )
    result = "".join(lines[entry.path] for entry in sorted(kept, key=lambda item: item.path))
    return (result + omitted_note).encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
