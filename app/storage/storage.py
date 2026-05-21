from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models.models import Event, today_key


class FileStore:
    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.events_dir = self.data_dir / "events"

    def ensure(self) -> None:
        self.events_dir.mkdir(parents=True, exist_ok=True)

    def event_path(self, day: str | None = None) -> Path:
        return self.events_dir / f"{day or today_key()}.json"

    def append_event(self, event: Event, day: str | None = None) -> None:
        events = self.load_events(day)
        events.append(event)
        self.write_json(self.event_path(day), [item.to_dict() for item in events])

    def load_events(self, day: str | None = None) -> list[Event]:
        path = self.event_path(day)
        if not path.exists():
            return []
        raw = self.read_json(path, [])
        return [Event.from_dict(item) for item in raw]

    def read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def write_json(self, path: Path, value: Any) -> None:
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
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {
            _json_safe(key): _json_safe(item)
            for key, item in value.items()
        }
    return value
