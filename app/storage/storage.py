from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


class GlobalEventStore:
    """原始事件流存储：每天一个 JSON 数组文件，append-only。"""

    def __init__(self, data_dir: Path | str, events_dirname: str = "events") -> None:
        """设置事件根目录，准备写锁；具体的日期文件按需创建。"""
        self.data_dir = Path(data_dir)
        self.events_dir = self.data_dir / events_dirname
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 写入

    def append_event(
        self,
        *,
        source: str,
        type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构建标准事件并追加到当天对应的 JSON 文件，返回写入后的事件。"""
        event = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": source,
            "type": type,
            "content": content,
            "metadata": metadata or {},
        }
        event_date = datetime.fromisoformat(event["timestamp"]).date()
        with self._lock:
            path = self._path_for_date(event_date)
            events = _read_json_array(path)
            events.append(event)
            _write_json_array(path, events)
        return event

    # ------------------------------------------------------------------ 读取

    def load_events(self) -> list[dict[str, Any]]:
        """读取所有日期文件并按时间戳合并，按时间升序返回。"""
        with self._lock:
            return self._load_all_unlocked()

    def load_events_for_date(self, target_date: date) -> list[dict[str, Any]]:
        """读取指定日期的事件文件，文件不存在时返回空列表。"""
        with self._lock:
            return _read_json_array(self._path_for_date(target_date))

    def load_events_in_range(
        self,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        """读取 [start_date, end_date] 区间内的事件，按时间升序合并。"""
        if start_date > end_date:
            return []
        with self._lock:
            collected: list[dict[str, Any]] = []
            for current in _iter_dates(start_date, end_date):
                collected.extend(_read_json_array(self._path_for_date(current)))
            collected.sort(key=_event_sort_key)
            return collected

    def list_event_dates(self) -> list[date]:
        """列出当前已经有事件文件的日期，方便调度和回填遍历。"""
        if not self.events_dir.exists():
            return []
        dates: list[date] = []
        for path in self.events_dir.iterdir():
            if path.suffix != ".json":
                continue
            try:
                dates.append(date.fromisoformat(path.stem))
            except ValueError:
                continue
        dates.sort()
        return dates

    # --------------------------------------------------------------- 内部工具

    def _path_for_date(self, target_date: date) -> Path:
        """返回某一天事件文件的绝对路径。"""
        return self.events_dir / f"{target_date.isoformat()}.json"

    def _load_all_unlocked(self) -> list[dict[str, Any]]:
        """在已持有锁的前提下读取所有日期文件并按时间排序。"""
        if not self.events_dir.exists():
            return []
        events: list[dict[str, Any]] = []
        for path in sorted(self.events_dir.iterdir()):
            if path.suffix != ".json":
                continue
            events.extend(_read_json_array(path))
        events.sort(key=_event_sort_key)
        return events


# ============================================================== 模块级辅助函数


def _iter_dates(start: date, end: date) -> Iterable[date]:
    """生成 [start, end] 区间内的所有日期，inclusive。"""
    current = start
    while current <= end:
        yield current
        current = date.fromordinal(current.toordinal() + 1)


def _event_sort_key(event: dict[str, Any]) -> str:
    """按 timestamp 字段排序事件，缺失时排在最前面。"""
    return str(event.get("timestamp", ""))


def _read_json_array(path: Path) -> list[dict[str, Any]]:
    """读取 JSON 数组文件，文件不存在时返回空数组。"""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    if not isinstance(raw, list):
        raise ValueError(f"JSON 文件必须是数组: {path}")
    return [dict(item) for item in raw]


def _write_json_array(path: Path, value: list[dict[str, Any]]) -> None:
    """原子写入 JSON 数组：先写临时文件再 replace，避免半截文件。"""
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


def _json_safe(value: Any) -> Any:
    """递归清理字符串编码，避免不可编码字符破坏文件。"""
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {_json_safe(key): _json_safe(item) for key, item in value.items()}
    return value
