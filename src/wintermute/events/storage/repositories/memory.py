"""每个账号的记忆整理计划和对话游标。"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from wintermute.events.storage import profiles

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore


@dataclass(frozen=True)
class MemorySchedule:
    interval_days: int
    local_time: str
    next_date: date
    zone: ZoneInfo

    @property
    def next_run_at(self) -> datetime:
        # Round-trip a skipped DST hour to a real instant. At a repeated hour use fold=0.
        wall = datetime.combine(self.next_date, time.fromisoformat(self.local_time), self.zone)
        return wall.astimezone(UTC).astimezone(self.zone)


class MemoryStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def initialize(self) -> None:
        db = self._store._connection
        db.execute(
            "CREATE TABLE IF NOT EXISTS memory_schedules (user_id TEXT PRIMARY KEY, "
            "interval_days INTEGER NOT NULL, local_time TEXT NOT NULL, next_date TEXT NOT NULL)"
        )
        db.execute("DROP INDEX IF EXISTS perception_events_profile_usage")
        db.execute("DROP INDEX IF EXISTS slice_events_profile_usage")
        db.execute("DROP TABLE IF EXISTS memory_maintenance")

    def profile_status(self, user_id: str) -> profiles.ProfileStatus:
        self._store._ensure_open()
        return profiles.status(self._store._connection, user_id, self._store._now())

    def profile_inputs(
        self, user_id: str, *, after: int, through: int, limit: int = 24
    ) -> tuple[profiles.ProfileInput, ...]:
        self._store._ensure_open()
        return profiles.inputs(self._store._connection, user_id, after, through, limit)

    def advance_memory_cursor(
        self, user_id: str, *, after: int, through: int, owner_id: str | None = None
    ) -> bool:
        """Advance only successfully summarized inputs, preserving concurrent new dialogue."""
        if not 0 <= after <= through:
            raise ValueError("Invalid memory cursor range")
        with self._store._transaction():
            if owner_id is not None:
                self._store.sessions._require_scheduler(owner_id)
            state = profiles.status(self._store._connection, user_id, self._store._now())
            if state.busy or state.cursor != after:
                return False
            if (
                through != after
                and not self._store._connection.execute(
                    "SELECT 1 FROM dialogue_entries "
                    "WHERE user_id=? AND seq=? AND event_id IS NOT NULL",
                    (user_id, through),
                ).fetchone()
            ):
                raise ValueError("Memory cursor must end on this user's dialogue input")
            self._store._connection.execute(
                "INSERT INTO profile_progress(user_id,cursor) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET cursor=excluded.cursor",
                (user_id, through),
            )
            return True

    def schedule(self, user_id: str) -> MemorySchedule:
        row = self._store._connection.execute(
            "SELECT interval_days,local_time,next_date FROM memory_schedules WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            return self.set_schedule(user_id, 1, "03:00", initialize=True)
        return MemorySchedule(
            row[0],
            row[1],
            date.fromisoformat(row[2]),
            ZoneInfo(self._store.timeline_policy.timezone),
        )

    def set_schedule(
        self, user_id: str, interval_days: int, local_time: str, *, initialize: bool = False
    ) -> MemorySchedule:
        if type(interval_days) is not int or not 1 <= interval_days <= 3650:
            raise ValueError("Interval must be between 1 and 3650 days")
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", local_time):
            raise ValueError("Expected HH:MM")
        zone = ZoneInfo(self._store.timeline_policy.timezone)
        now = self._store.now_utc()
        day = now.astimezone(zone).date()
        if MemorySchedule(interval_days, local_time, day, zone).next_run_at < now:
            day += timedelta(days=1)
        conflict = (
            "DO NOTHING"
            if initialize
            else (
                "DO UPDATE SET interval_days=excluded.interval_days,local_time=excluded.local_time,"
                "next_date=excluded.next_date WHERE interval_days!=excluded.interval_days "
                "OR local_time!=excluded.local_time"
            )
        )
        with self._store._transaction():
            self._store._connection.execute(
                "INSERT INTO memory_schedules VALUES(?,?,?,?) ON CONFLICT(user_id) " + conflict,
                (user_id, interval_days, local_time, day.isoformat()),
            )
            return self.schedule(user_id)

    def finish_schedule(self, user_id: str, plan: MemorySchedule, owner_id: str | None) -> None:
        now = self._store.now_utc()
        elapsed = (now.astimezone(plan.zone).date() - plan.next_date).days
        interval = timedelta(days=plan.interval_days)
        day = plan.next_date + max(1, elapsed // plan.interval_days) * interval
        if MemorySchedule(plan.interval_days, plan.local_time, day, plan.zone).next_run_at <= now:
            day += interval
        with self._store._transaction():
            if owner_id is not None:
                self._store.sessions._require_scheduler(owner_id)
            # Do not overwrite a new plan saved while the model was running.
            self._store._connection.execute(
                "UPDATE memory_schedules SET next_date=? WHERE user_id=? AND interval_days=? "
                "AND local_time=? AND next_date=?",
                (
                    day.isoformat(),
                    user_id,
                    plan.interval_days,
                    plan.local_time,
                    plan.next_date.isoformat(),
                ),
            )
