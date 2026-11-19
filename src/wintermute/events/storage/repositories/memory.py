"""记忆整理游标、维护日期和用户活动查询。"""

import asyncio
import sqlite3
from contextlib import closing
from datetime import date, datetime
from typing import TYPE_CHECKING

from wintermute.events.storage import profiles
from wintermute.events.storage.profile_activity import ActivityPattern, activity_pattern

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore


class MemoryStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

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

    def housekeeping_date(self, user_id: str) -> date | None:
        self._store._ensure_open()
        row = self._store._connection.execute(
            "SELECT completed_date FROM memory_maintenance WHERE user_id=?", (user_id,)
        ).fetchone()
        return date.fromisoformat(row[0]) if row is not None else None

    def set_housekeeping_date(
        self, user_id: str, completed_date: date, *, owner_id: str | None = None
    ) -> None:
        with self._store._transaction():
            if owner_id is not None:
                self._store.sessions._require_scheduler(owner_id)
            self._store._connection.execute(
                "INSERT INTO memory_maintenance(user_id,completed_date) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET completed_date=excluded.completed_date",
                (user_id, completed_date.isoformat()),
            )

    def profile_activity(self, user_id: str, now: datetime, timezone: str) -> ActivityPattern:
        self._store._ensure_open()
        return activity_pattern(self._store._connection, user_id, now, timezone)

    async def read_profile_activity(
        self, user_id: str, now: datetime, timezone: str
    ) -> ActivityPattern:
        self._store._ensure_open()
        if self._store._database_path is None:
            return self.profile_activity(user_id, now, timezone)
        uri = self._store._database_path.as_uri() + "?mode=ro"

        def read() -> ActivityPattern:
            with closing(sqlite3.connect(uri, uri=True)) as reader:
                reader.row_factory = sqlite3.Row
                return activity_pattern(reader, user_id, now, timezone)

        return await asyncio.to_thread(read)
