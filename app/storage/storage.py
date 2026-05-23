from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class GlobalEventStore:
    """全局事件流存储，使用单个 JSON 数组文件保存所有对话历史。"""

    def __init__(self, data_dir: Path | str, filename: str = "events.json") -> None:
        """设置历史文件路径，并准备线程锁保护读写。"""
        self.data_dir = Path(data_dir)
        self.events_path = self.data_dir / filename
        self._lock = threading.Lock()

    def append_event(
        self,
        *,
        source: str,
        type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """根据输入参数构建事件并追加到历史文件。"""
        with self._lock:
            events = self._load_events_unlocked()
            events.append(
                {
                    "id": str(uuid.uuid4()),
                    "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "source": source,
                    "type": type,
                    "content": content,
                    "metadata": metadata or {},
                }
            )
            self._write_json_unlocked(events)

    def load_events(self) -> list[dict[str, Any]]:
        """读取全部历史事件，供构造 LLM 上下文使用。"""
        with self._lock:
            return self._load_events_unlocked()

    def _load_events_unlocked(self) -> list[dict[str, Any]]:
        """在已持有锁的前提下读取历史文件。"""
        if not self.events_path.exists():
            return []
        raw = self._read_json_unlocked([])
        if not isinstance(raw, list):
            raise ValueError(f"事件历史必须是 JSON 数组: {self.events_path}")
        return [dict(item) for item in raw]

    def _read_json_unlocked(self, default: Any) -> Any:
        """在已持有锁的前提下读取 JSON，文件不存在时返回默认值。"""
        if not self.events_path.exists():
            return default
        with self.events_path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def _write_json_unlocked(self, value: Any) -> None:
        """在已持有锁的前提下原子写入 JSON，避免写到一半留下坏文件。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.events_path.with_suffix(self.events_path.suffix + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as file:
                json.dump(_json_safe(value), file, ensure_ascii=False, indent=2)
                file.write("\n")
            tmp_path.replace(self.events_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise


def _json_safe(value: Any) -> Any:
    """递归清理字符串编码，避免不可编码字符破坏历史文件。"""
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {_json_safe(key): _json_safe(item) for key, item in value.items()}
    return value
