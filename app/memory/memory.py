from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


class MemoryKind(str, Enum):
    """记忆条目的层级类型，决定文件归属和压缩入口。"""

    SESSION = "session"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class MemoryEntry:
    """单条记忆条目，所有层级共用同一个数据结构。"""

    id: str
    kind: MemoryKind
    period_start: datetime
    period_end: datetime
    summary: str
    tokens: int
    created_at: datetime
    tags: list[str] = field(default_factory=list)
    source_event_ids: list[str] = field(default_factory=list)
    source_memory_ids: list[str] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        *,
        kind: MemoryKind,
        period_start: datetime,
        period_end: datetime,
        summary: str,
        tokens: int,
        tags: list[str] | None = None,
        source_event_ids: list[str] | None = None,
        source_memory_ids: list[str] | None = None,
    ) -> "MemoryEntry":
        """构造一条带新 id 和当前时间戳的记忆条目。"""
        return cls(
            id=str(uuid.uuid4()),
            kind=kind,
            period_start=period_start,
            period_end=period_end,
            summary=summary,
            tokens=tokens,
            created_at=datetime.now().astimezone(),
            tags=list(tags or []),
            source_event_ids=list(source_event_ids or []),
            source_memory_ids=list(source_memory_ids or []),
        )

    def to_json(self) -> dict[str, Any]:
        """序列化为 JSON 可写入字典。"""
        data = asdict(self)
        data["kind"] = self.kind.value
        data["period_start"] = self.period_start.isoformat(timespec="seconds")
        data["period_end"] = self.period_end.isoformat(timespec="seconds")
        data["created_at"] = self.created_at.isoformat(timespec="seconds")
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "MemoryEntry":
        """反序列化 JSON 字典回 MemoryEntry。"""
        return cls(
            id=str(data["id"]),
            kind=MemoryKind(data["kind"]),
            period_start=datetime.fromisoformat(str(data["period_start"])),
            period_end=datetime.fromisoformat(str(data["period_end"])),
            summary=str(data.get("summary", "")),
            tokens=int(data.get("tokens", 0)),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            tags=list(data.get("tags", [])),
            source_event_ids=list(data.get("source_event_ids", [])),
            source_memory_ids=list(data.get("source_memory_ids", [])),
        )


class MemoryStore:
    """多级记忆存储：按 kind 分子目录、按 period 分文件。"""

    def __init__(self, data_dir: Path | str, memories_dirname: str = "memories") -> None:
        """设置记忆根目录并准备写锁。"""
        self.data_dir = Path(data_dir)
        self.memories_dir = self.data_dir / memories_dirname
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 写入

    def append(self, entry: MemoryEntry) -> None:
        """把记忆条目写入它所在 kind 和 period 对应的文件。"""
        path = self._path_for(entry.kind, entry.period_start)
        with self._lock:
            entries = _read_json_array(path)
            entries.append(entry.to_json())
            _write_json_array(path, entries)

    # ------------------------------------------------------------------ 读取

    def load_by_kind(self, kind: MemoryKind) -> list[MemoryEntry]:
        """读取某一层级的全部记忆，按 period_start 升序返回。"""
        kind_dir = self.memories_dir / kind.value
        with self._lock:
            entries = self._load_all_files_unlocked(kind_dir)
        entries.sort(key=lambda entry: entry.period_start)
        return entries

    def load_all(self) -> list[MemoryEntry]:
        """读取所有层级的全部记忆，按 period_start 升序合并。"""
        entries: list[MemoryEntry] = []
        for kind in MemoryKind:
            entries.extend(self.load_by_kind(kind))
        entries.sort(key=lambda entry: entry.period_start)
        return entries

    def has_for_period(
        self,
        kind: MemoryKind,
        period_start: datetime,
        period_end: datetime,
    ) -> bool:
        """检查是否已经为指定 period 生成过该层级记忆，用于压缩幂等。"""
        for entry in self.load_by_kind(kind):
            if entry.period_start == period_start and entry.period_end == period_end:
                return True
        return False

    def compressed_event_ids(self) -> set[str]:
        """返回所有已被某条 session 记忆收录的原始事件 id 集合。"""
        compressed: set[str] = set()
        for entry in self.load_by_kind(MemoryKind.SESSION):
            compressed.update(entry.source_event_ids)
        return compressed

    # --------------------------------------------------------------- 内部工具

    def _path_for(self, kind: MemoryKind, period_start: datetime) -> Path:
        """根据层级和起始时间，返回对应文件的绝对路径。"""
        return self.memories_dir / kind.value / f"{_period_filename(kind, period_start)}.json"

    def _load_all_files_unlocked(self, kind_dir: Path) -> list[MemoryEntry]:
        """读取一个 kind 目录下的全部 JSON 文件并解析成记忆条目。"""
        if not kind_dir.exists():
            return []
        entries: list[MemoryEntry] = []
        for path in sorted(kind_dir.iterdir()):
            if path.suffix != ".json":
                continue
            for raw in _read_json_array(path):
                entries.append(MemoryEntry.from_json(raw))
        return entries


# ============================================================== 模块级辅助函数


def _period_filename(kind: MemoryKind, period_start: datetime) -> str:
    """根据层级把 period_start 映射成对应的文件名。"""
    if kind in (MemoryKind.SESSION, MemoryKind.DAILY):
        return period_start.date().isoformat()
    if kind is MemoryKind.WEEKLY:
        iso = period_start.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    if kind is MemoryKind.MONTHLY:
        return f"{period_start.year:04d}-{period_start.month:02d}"
    raise ValueError(f"未知的记忆层级: {kind}")


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    """读取 JSON 数组文件，文件不存在时返回空数组。"""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    if not isinstance(raw, list):
        raise ValueError(f"JSON 文件必须是数组: {path}")
    return [dict(item) for item in raw]


def _write_json_array(path: Path, value: Iterable[dict[str, Any]]) -> None:
    """原子写入 JSON 数组：临时文件 + replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(list(value), file, ensure_ascii=False, indent=2)
            file.write("\n")
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
