from __future__ import annotations

import json
import re
import threading
import uuid
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

SCHEDULE_STATUSES = {"active", "completed", "cancelled"}
RECURRENCE_FREQUENCIES = {"none", "daily", "weekly", "monthly"}

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ScheduleStore:
    """JSON-file backed schedule storage under data/schedule."""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.schedule_dir = self.data_dir / "schedule"
        self.schedule_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create_schedule(
        self,
        *,
        title: object,
        trigger_at: datetime | str,
        content: object = "",
        recurrence: object = None,
    ) -> dict[str, Any]:
        title_text = _clean_required_text(title, "title")
        content_text = _clean_optional_text(content)
        trigger_time = _parse_datetime(trigger_at, field="trigger_at")
        normalized_recurrence = _normalize_recurrence(
            recurrence,
            trigger_at=trigger_time,
        )
        now = _now_iso()
        schedule = {
            "id": str(uuid.uuid4()),
            "title": title_text,
            "content": content_text,
            "next_trigger_at": _format_datetime(trigger_time),
            "status": "active",
            "recurrence": normalized_recurrence,
            "created_at": now,
            "updated_at": now,
            "last_triggered_at": None,
            "triggered_occurrences": [],
        }
        with self._lock:
            self._write_schedule_unlocked(schedule)
        return dict(schedule)

    def get_schedule(self, schedule_id: object) -> dict[str, Any] | None:
        path = self._path_for_id(schedule_id)
        with self._lock:
            if not path.exists():
                return None
            return self._load_schedule_unlocked(path)

    def list_schedules(
        self,
        *,
        status: object = None,
        start: datetime | date | str | None = None,
        end: datetime | date | str | None = None,
        include_cancelled: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        status_filter = _normalize_status(status) if status is not None else None
        start_dt = _parse_boundary(start, field="start")
        end_dt = _parse_boundary(end, field="end")
        if start_dt is not None and end_dt is not None and end_dt <= start_dt:
            raise ValueError("end 必须晚于 start。")

        with self._lock:
            schedules = [
                self._load_schedule_unlocked(path)
                for path in sorted(self.schedule_dir.glob("*.json"))
            ]

        filtered = []
        for schedule in schedules:
            item_status = str(schedule.get("status", "")).strip()
            if status_filter is not None and item_status != status_filter:
                continue
            if status_filter is None and not include_cancelled and item_status == "cancelled":
                continue
            trigger_time = _optional_datetime(schedule.get("next_trigger_at"))
            if start_dt is not None and (trigger_time is None or trigger_time < start_dt):
                continue
            if end_dt is not None and (trigger_time is None or trigger_time >= end_dt):
                continue
            filtered.append(schedule)

        filtered.sort(key=_schedule_sort_key)
        if limit is not None:
            filtered = filtered[: max(0, int(limit))]
        return [dict(item) for item in filtered]

    def update_schedule(self, schedule_id: object, changes: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(changes, dict):
            raise ValueError("changes 必须是对象。")
        path = self._path_for_id(schedule_id)
        with self._lock:
            if not path.exists():
                return None
            schedule = self._load_schedule_unlocked(path)

            if "title" in changes:
                schedule["title"] = _clean_required_text(changes["title"], "title")
            if "content" in changes:
                schedule["content"] = _clean_optional_text(changes["content"])

            trigger_key = "trigger_at" if "trigger_at" in changes else "next_trigger_at"
            if trigger_key in changes:
                trigger_time = _parse_datetime(changes[trigger_key], field=trigger_key)
                schedule["next_trigger_at"] = _format_datetime(trigger_time)

            if "recurrence" in changes:
                next_trigger_at = _optional_datetime(schedule.get("next_trigger_at"))
                schedule["recurrence"] = _normalize_recurrence(
                    changes["recurrence"],
                    trigger_at=next_trigger_at,
                )

            if "status" in changes:
                schedule["status"] = _normalize_status(changes["status"])
            if schedule.get("status") == "active" and not schedule.get("next_trigger_at"):
                raise ValueError("active 日程必须有 next_trigger_at。")

            schedule["updated_at"] = _now_iso()
            self._write_schedule_unlocked(schedule)
            return dict(schedule)

    def delete_schedule(self, schedule_id: object) -> dict[str, Any] | None:
        return self.update_schedule(schedule_id, {"status": "cancelled"})

    def due_schedules(self, now: datetime | str | None = None) -> list[dict[str, Any]]:
        reference = _parse_datetime(now, field="now") if now is not None else datetime.now().astimezone()
        schedules = self.list_schedules(status="active", include_cancelled=False)
        due = []
        for schedule in schedules:
            trigger_time = _optional_datetime(schedule.get("next_trigger_at"))
            if trigger_time is not None and trigger_time <= reference:
                due.append(schedule)
        return due

    def mark_triggered(
        self,
        schedule_id: object,
        *,
        triggered_at: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        trigger_time = (
            _parse_datetime(triggered_at, field="triggered_at")
            if triggered_at is not None
            else datetime.now().astimezone()
        )
        trigger_iso = _format_datetime(trigger_time)
        path = self._path_for_id(schedule_id)
        with self._lock:
            if not path.exists():
                return None
            schedule = self._load_schedule_unlocked(path)
            if schedule.get("status") != "active":
                return dict(schedule)

            occurrences = schedule.get("triggered_occurrences")
            if not isinstance(occurrences, list):
                occurrences = []
            occurrences.append(trigger_iso)
            schedule["triggered_occurrences"] = occurrences
            schedule["last_triggered_at"] = trigger_iso
            schedule["updated_at"] = trigger_iso

            recurrence = _normalize_recurrence(schedule.get("recurrence"))
            if recurrence["frequency"] == "none":
                schedule["status"] = "completed"
            else:
                current_next = _optional_datetime(schedule.get("next_trigger_at")) or trigger_time
                next_trigger = _next_recurrence_after(current_next, recurrence)
                guard = 0
                while next_trigger <= trigger_time and guard < 10000:
                    next_trigger = _next_recurrence_after(next_trigger, recurrence)
                    guard += 1
                until = _optional_datetime(recurrence.get("until"))
                if until is not None and next_trigger > until:
                    schedule["status"] = "completed"
                else:
                    schedule["next_trigger_at"] = _format_datetime(next_trigger)
                    schedule["recurrence"] = recurrence

            self._write_schedule_unlocked(schedule)
            return dict(schedule)

    def schedules_for_prompt(
        self,
        *,
        reference: datetime | None = None,
        day: date | None = None,
        days: int = 7,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        reference_time = reference or datetime.now().astimezone()
        target_day = day or reference_time.date()
        window_end = reference_time + timedelta(days=max(1, days))
        schedules = self.list_schedules(include_cancelled=False)

        items: list[dict[str, Any]] = []
        for schedule in schedules:
            if schedule.get("status") != "active":
                continue
            trigger_time = _optional_datetime(schedule.get("next_trigger_at"))
            if trigger_time is None:
                continue
            if trigger_time < reference_time:
                items.append(
                    {"category": "overdue", "time": _format_datetime(trigger_time), "schedule": schedule}
                )
            elif trigger_time < window_end:
                items.append(
                    {"category": "upcoming", "time": _format_datetime(trigger_time), "schedule": schedule}
                )

        for schedule in schedules:
            if _triggered_on_day(schedule, target_day):
                triggered_at = str(schedule.get("last_triggered_at") or "")
                items.append(
                    {
                        "category": "triggered_today",
                        "time": triggered_at,
                        "schedule": schedule,
                    }
                )

        items.sort(key=_prompt_item_sort_key)
        return [dict(item) for item in items[: max(0, limit)]]

    def _path_for_id(self, schedule_id: object) -> Path:
        text = str(schedule_id or "").strip()
        if not text:
            raise ValueError("id 不能为空。")
        if not _ID_PATTERN.fullmatch(text):
            raise ValueError(f"id 格式无效: {text}")
        path = (self.schedule_dir / f"{text}.json").resolve()
        base = self.schedule_dir.resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"id 越界日程目录: {text}") from exc
        return path

    def _load_schedule_unlocked(self, path: Path) -> dict[str, Any]:
        raw = _read_json(path)
        if not isinstance(raw, dict):
            raise ValueError(f"日程文件必须是 JSON 对象: {path}")
        return dict(raw)

    def _write_schedule_unlocked(self, schedule: dict[str, Any]) -> None:
        schedule_id = str(schedule.get("id") or "").strip()
        path = self._path_for_id(schedule_id)
        _write_json(path, schedule)


def _normalize_recurrence(value: object, *, trigger_at: datetime | None = None) -> dict[str, Any]:
    if value is None or value == "":
        return {"frequency": "none", "interval": 1, "until": None}
    if isinstance(value, str):
        raw: dict[str, Any] = {"frequency": value}
    elif isinstance(value, dict):
        raw = dict(value)
    else:
        raise ValueError("recurrence 必须是对象、字符串或空。")

    frequency = str(
        raw.get("frequency") or raw.get("type") or raw.get("kind") or "none"
    ).strip().lower()
    if frequency not in RECURRENCE_FREQUENCIES:
        raise ValueError(f"recurrence.frequency 无效: {frequency}")
    raw_interval = raw.get("interval", 1)
    if raw_interval is None or raw_interval == "":
        raw_interval = 1
    try:
        interval = int(raw_interval)
    except (TypeError, ValueError) as exc:
        raise ValueError("recurrence.interval 必须是正整数。") from exc
    if interval < 1:
        raise ValueError("recurrence.interval 必须是正整数。")

    until_value = raw.get("until")
    until = _optional_datetime(until_value)
    if until is not None and trigger_at is not None and until < trigger_at:
        raise ValueError("recurrence.until 不能早于 trigger_at。")
    if frequency == "none":
        interval = 1
        until = None
    return {
        "frequency": frequency,
        "interval": interval,
        "until": _format_datetime(until) if until is not None else None,
    }


def _next_recurrence_after(current: datetime, recurrence: dict[str, Any]) -> datetime:
    raw_interval = recurrence.get("interval", 1)
    interval = int(raw_interval if raw_interval not in (None, "") else 1)
    frequency = str(recurrence.get("frequency") or "none")
    if frequency == "daily":
        return current + timedelta(days=interval)
    if frequency == "weekly":
        return current + timedelta(weeks=interval)
    if frequency == "monthly":
        return _add_months(current, interval)
    raise ValueError("非重复日程没有下一次触发时间。")


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _normalize_status(value: object) -> str:
    status = str(value or "").strip().lower()
    if status not in SCHEDULE_STATUSES:
        raise ValueError(f"status 无效: {status or '(空)'}")
    return status


def _schedule_sort_key(schedule: dict[str, Any]) -> tuple[str, str]:
    return (
        str(schedule.get("next_trigger_at") or "9999-12-31T23:59:59"),
        str(schedule.get("created_at") or ""),
    )


def _prompt_item_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    category_order = {"overdue": 0, "upcoming": 1, "triggered_today": 2}
    return (category_order.get(str(item.get("category")), 99), str(item.get("time") or ""))


def _triggered_on_day(schedule: dict[str, Any], target_day: date) -> bool:
    occurrences = schedule.get("triggered_occurrences")
    if not isinstance(occurrences, list):
        return False
    for item in occurrences:
        try:
            if _parse_datetime(str(item), field="triggered_occurrences").date() == target_day:
                return True
        except ValueError:
            continue
    return False


def _parse_boundary(value: datetime | date | str | None, *, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _localize(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min).astimezone()
    text = str(value).strip()
    if "T" not in text and " " not in text:
        return datetime.combine(date.fromisoformat(text), time.min).astimezone()
    return _parse_datetime(text, field=field)


def _parse_datetime(value: datetime | str | object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return _localize(value)
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} 不能为空。")
    try:
        return _localize(datetime.fromisoformat(text))
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 日期时间。") from exc


def _optional_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    return _parse_datetime(value, field="datetime")


def _localize(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.astimezone()
    return value.astimezone()


def _format_datetime(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean_required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} 不能为空。")
    return text


def _clean_optional_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _read_json(path: Path) -> Any:
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


def _json_safe(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {_json_safe(key): _json_safe(item) for key, item in value.items()}
    return value
