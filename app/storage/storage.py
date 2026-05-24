from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any


MEMORY_KINDS = {"session", "daily", "weekly", "monthly"}


class GlobalEventStore:
    """全局事件流存储，按本地日期切分 JSON 数组文件。"""

    def __init__(self, data_dir: Path | str) -> None:
        """设置事件目录，并准备线程锁保护读写。"""
        self.data_dir = Path(data_dir)
        self.events_dir = self.data_dir / "events"
        self._lock = threading.Lock()

    def append_event(
        self,
        *,
        source: str,
        type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        timestamp: datetime | str | None = None,
    ) -> dict[str, Any]:
        """根据输入参数构建事件，并追加到 timestamp 所属日期文件。"""
        event_time = _coerce_datetime(timestamp)
        event = {
            "id": str(uuid.uuid4()),
            "timestamp": event_time.isoformat(timespec="seconds"),
            "source": source,
            "type": type,
            "content": content,
            "metadata": metadata or {},
        }
        with self._lock:
            path = self._path_for_date(event_time.date())
            events = self._load_events_from_path_unlocked(path)
            events.append(event)
            self._write_json_unlocked(path, events)
        return dict(event)

    def load_events(
        self,
        *,
        start: date | datetime | str | None = None,
        end: date | datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """读取事件；start/end 使用 [start, end) 过滤，省略时读取所有日切文件。"""
        start_dt = _coerce_boundary(start)
        end_dt = _coerce_boundary(end)
        with self._lock:
            events: list[dict[str, Any]] = []
            for path in sorted(self.events_dir.glob("*.json")):
                events.extend(self._load_events_from_path_unlocked(path))
        return _sorted_filtered_events(events, start_dt=start_dt, end_dt=end_dt)

    def load_events_for_date(self, target_date: date | str) -> list[dict[str, Any]]:
        """读取某个自然日的全部 raw events。"""
        event_date = _coerce_date(target_date)
        start_dt = datetime.combine(event_date, time.min).astimezone()
        end_dt = start_dt + timedelta(days=1)
        with self._lock:
            return _sorted_filtered_events(
                self._load_events_from_path_unlocked(self._path_for_date(event_date)),
                start_dt=start_dt,
                end_dt=end_dt,
            )

    def _path_for_date(self, event_date: date) -> Path:
        return self.events_dir / f"{event_date.isoformat()}.json"

    def _load_events_from_path_unlocked(self, path: Path) -> list[dict[str, Any]]:
        """在已持有锁的前提下读取事件文件。"""
        if not path.exists():
            return []
        raw = _read_json(path, [])
        if not isinstance(raw, list):
            raise ValueError(f"事件历史必须是 JSON 数组: {path}")
        return [dict(item) for item in raw]

    def _write_json_unlocked(self, path: Path, value: Any) -> None:
        """在已持有锁的前提下原子写入 JSON。"""
        _write_json(path, value)


class MemoryStore:
    """按 kind 分目录保存 session/daily/weekly/monthly 记忆。"""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.memories_dir = self.data_dir / "memories"
        self._lock = threading.Lock()

    def save_memory(
        self,
        *,
        kind: str,
        label: str,
        period: dict[str, str],
        content: str,
        source_event_ids: list[str] | None = None,
        source_memory_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """保存一条记忆；固定周期文件存在时跳过，session 用 source_event_ids 去重。"""
        self._validate_kind(kind)
        memory = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "period": dict(period),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_event_ids": list(source_event_ids or []),
            "source_memory_ids": list(source_memory_ids or []),
            "content": content,
            "metadata": metadata or {},
        }
        with self._lock:
            path = self.path_for(kind, label)
            if kind == "session":
                memories = self._load_session_file_unlocked(path)
                new_ids = set(memory["source_event_ids"])
                if new_ids and any(set(item.get("source_event_ids", [])) == new_ids for item in memories):
                    return None
                memories.append(memory)
                self._write_json_unlocked(path, memories)
                return dict(memory)

            if path.exists():
                return None
            self._write_json_unlocked(path, memory)
            return dict(memory)

    def load_memory(self, kind: str, label: str) -> dict[str, Any] | None:
        """读取 daily/weekly/monthly 单条记忆；文件不存在时返回 None。"""
        self._validate_kind(kind)
        path = self.path_for(kind, label)
        with self._lock:
            if not path.exists():
                return None
            raw = _read_json(path, None)
        if not isinstance(raw, dict):
            raise ValueError(f"记忆文件必须是 JSON 对象: {path}")
        return dict(raw)

    def load_session_memories(self, label: str) -> list[dict[str, Any]]:
        """读取某天所有 session 记忆。"""
        path = self.path_for("session", label)
        with self._lock:
            return self._load_session_file_unlocked(path)

    def load_all_memories(self) -> list[dict[str, Any]]:
        """扫描所有记忆文件，并返回展平后的记忆列表。"""
        memories: list[dict[str, Any]] = []
        with self._lock:
            for kind in ("monthly", "weekly", "daily"):
                for path in sorted((self.memories_dir / kind).glob("*.json")):
                    raw = _read_json(path, None)
                    if not isinstance(raw, dict):
                        raise ValueError(f"记忆文件必须是 JSON 对象: {path}")
                    memories.append(dict(raw))
            for path in sorted((self.memories_dir / "session").glob("*.json")):
                memories.extend(self._load_session_file_unlocked(path))
        return memories

    def memory_exists(self, kind: str, label: str) -> bool:
        """判断指定 kind/label 的记忆文件是否已存在。"""
        self._validate_kind(kind)
        return self.path_for(kind, label).exists()

    def path_for(self, kind: str, label: str) -> Path:
        """返回指定记忆文件路径。"""
        self._validate_kind(kind)
        return self.memories_dir / kind / f"{label}.json"

    def source_event_ids_for_session(self, label: str) -> set[str]:
        """返回某天已进入 session 记忆的 source_event_ids。"""
        ids: set[str] = set()
        for memory in self.load_session_memories(label):
            ids.update(str(item) for item in memory.get("source_event_ids", []))
        return ids

    def _load_session_file_unlocked(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        raw = _read_json(path, [])
        if not isinstance(raw, list):
            raise ValueError(f"session 记忆必须是 JSON 数组: {path}")
        return [dict(item) for item in raw]

    def _write_json_unlocked(self, path: Path, value: Any) -> None:
        _write_json(path, value)

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in MEMORY_KINDS:
            raise ValueError(f"未知记忆类型: {kind}")


def _sorted_filtered_events(
    events: list[dict[str, Any]],
    *,
    start_dt: datetime | None,
    end_dt: datetime | None,
) -> list[dict[str, Any]]:
    filtered = []
    for event in events:
        timestamp = _coerce_datetime(event.get("timestamp"))
        if start_dt is not None and timestamp < start_dt:
            continue
        if end_dt is not None and timestamp >= end_dt:
            continue
        filtered.append(dict(event))
    return sorted(filtered, key=lambda item: str(item.get("timestamp", "")))


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(_json_safe(value), file, ensure_ascii=False, indent=2)
            file.write("\n")
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _coerce_datetime(value: datetime | str | object | None) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()


def _coerce_boundary(value: date | datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _coerce_datetime(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min).astimezone()
    text = str(value)
    if "T" in text or " " in text:
        return _coerce_datetime(text)
    return datetime.combine(date.fromisoformat(text), time.min).astimezone()


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _json_safe(value: Any) -> Any:
    """递归清理字符串编码，避免不可编码字符破坏历史文件。"""
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {_json_safe(key): _json_safe(item) for key, item in value.items()}
    return value
