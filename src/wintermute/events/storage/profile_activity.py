"""从设备已结束的前台时段估计用户常用的本地时间。"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from wintermute.events.core.desktop import WINDOW_SPAN_STATE_KEY

_WINDOW_DAYS = 28
_MIN_SAMPLE_DAYS = 3
_DESKTOP = (
    "json_extract(data,'$.type')='state.observed' AND "
    f"json_extract(data,'$.payload.state_key')='{WINDOW_SPAN_STATE_KEY}'"
)
_MOBILE = (
    "json_extract(data,'$.type')='slice.recorded' AND "
    "json_extract(data,'$.payload.text') GLOB '手机前台使用「*'"
)
_OCCURRED = "substr(json_extract(data,'$.occurred_at'),1,19)"
_STAMP = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
# Android's usage collector emits these UTC timestamps at whole-second precision.
_USAGE = re.compile(rf"手机前台使用「.+」，{_STAMP} 至 {_STAMP}（UTC），约 \d+ 秒。")


def initialize(db: sqlite3.Connection) -> None:
    for table, condition in (("perception_events", _DESKTOP), ("slice_events", _MOBILE)):
        db.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_profile_usage ON {table}(user_id,{_OCCURRED}) "
            f"WHERE {condition}"
        )


def _common_slots(samples: list[int]) -> frozenset[int] | None:
    if len(samples) < _MIN_SAMPLE_DAYS:
        return None
    return frozenset(
        slot
        for slot in range(48)
        if 2 * sum(bool(day & (1 << slot)) for day in samples) >= len(samples)
    )


def _ranges(slots: frozenset[int] | None) -> str:
    if slots is None:
        return "样本不足"
    spans = []
    start = None
    for i in range(49):
        if i in slots:
            if start is None:
                start = i
        elif start is not None:
            spans.append(f"{start // 2:02}:{start % 2 * 30:02}–{i // 2:02}:{i % 2 * 30:02}")
            start = None
    return "、".join(spans) or "尚无反复出现的时段"


@dataclass(frozen=True)
class ActivityPattern:
    timezone: str
    first_day: date
    last_day: date
    weekday_days: int
    weekend_days: int
    weekday_slots: frozenset[int] | None
    weekend_slots: frozenset[int] | None
    low_activity: bool | None

    @property
    def observed_days(self) -> int:
        return self.weekday_days + self.weekend_days

    def markdown(self) -> str:
        return "\n".join(
            (
                "## 常用时间段",
                f"- 统计时区：{self.timezone}",
                f"- 统计窗口：{self.first_day} 至 {self.last_day}",
                f"- 有记录的天数：{self.observed_days} 天"
                f"（工作日 {self.weekday_days}，周末 {self.weekend_days}）",
                f"- 工作日常见活跃时段：{_ranges(self.weekday_slots)}",
                f"- 周末常见活跃时段：{_ranges(self.weekend_slots)}",
            )
        )


def activity_pattern(
    db: sqlite3.Connection, user_id: str, now: datetime, timezone: str
) -> ActivityPattern:
    zone = ZoneInfo(timezone)
    if now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    local = now.astimezone(zone)
    today = local.date()
    first_day = today - timedelta(days=_WINDOW_DAYS)
    start = datetime.combine(first_day, time.min, zone).astimezone(UTC)
    stop = datetime.combine(today, time.min, zone).astimezone(UTC)
    # Rows use normalized UTC occurrence times. Include spans closed today that crossed midnight.
    bounds = user_id, start.isoformat()[:19], now.astimezone(UTC).isoformat()[:19]
    days: dict[date, int] = {}

    def observe(begin: datetime, end: datetime, active_seconds: float | None = None) -> None:
        if begin.utcoffset() is None or end.utcoffset() is None or end > now:
            return
        begin, end = begin.astimezone(UTC), end.astimezone(UTC)
        # Desktop subtracts brief switches from its duration but cannot locate those gaps.
        gap_seconds = (
            0.0 if active_seconds is None else (end - begin).total_seconds() - active_seconds
        )
        cursor, end = max(begin, start), min(end, stop)
        while cursor < end:
            wall = cursor.astimezone(zone)
            slot = wall.hour * 2 + wall.minute // 30
            # Walk actual elapsed time through DST. Both occurrences of a repeated slot count once.
            next_slot = cursor + timedelta(
                minutes=30 - wall.minute % 30, seconds=-wall.second, microseconds=-wall.microsecond
            )
            if (min(end, next_slot) - cursor).total_seconds() > gap_seconds:
                days[wall.date()] = days.get(wall.date(), 0) | (1 << slot)
            cursor = next_slot

    for begin, end, seconds in db.execute(
        "SELECT json_extract(data,'$.payload.value.started_at'),"
        "json_extract(data,'$.payload.value.ended_at'),"
        "json_extract(data,'$.payload.value.duration_seconds') FROM perception_events "
        f"WHERE user_id=? AND filter_reason IS NULL AND {_DESKTOP} AND {_OCCURRED} BETWEEN ? AND ?",
        bounds,
    ):
        if not isinstance(begin, str) or not isinstance(end, str):
            continue
        if not isinstance(seconds, (float, int)) or seconds <= 0:
            continue
        try:
            observe(datetime.fromisoformat(begin), datetime.fromisoformat(end), seconds)
        except (OverflowError, ValueError):
            continue
    for (text,) in db.execute(
        "SELECT json_extract(data,'$.payload.text') FROM slice_events "
        f"WHERE user_id=? AND {_MOBILE} AND {_OCCURRED} BETWEEN ? AND ?",
        bounds,
    ):
        match = _USAGE.fullmatch(text)
        if match:
            try:
                begin = datetime.fromisoformat(match[1]).replace(tzinfo=UTC)
                end = datetime.fromisoformat(match[2]).replace(tzinfo=UTC)
                observe(begin, end)
            except (OverflowError, ValueError):
                continue

    weekdays = [mask for day, mask in days.items() if day.weekday() < 5]
    weekends = [mask for day, mask in days.items() if day.weekday() >= 5]
    weekday_slots, weekend_slots = _common_slots(weekdays), _common_slots(weekends)
    slots = weekday_slots if today.weekday() < 5 else weekend_slots
    slot = local.hour * 2 + local.minute // 30
    return ActivityPattern(
        timezone,
        first_day,
        today - timedelta(days=1),
        len(weekdays),
        len(weekends),
        weekday_slots,
        weekend_slots,
        None if not slots else slot not in slots,
    )
