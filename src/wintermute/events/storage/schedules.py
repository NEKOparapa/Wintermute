"""Indexed, conversation-scoped queries for the persistent agent calendar."""

import re
import sqlite3
from datetime import UTC, datetime

from wintermute.events.core.schedule import ScheduleEntry, ScheduleStatus

FIELDS = (
    "id,title,content,action,trigger_at,triggered_at,"
    "CASE WHEN triggered_at IS NULL THEN 'scheduled' ELSE 'triggered' END AS status"
)


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY NOT NULL,"
        "user_id TEXT NOT NULL,conversation_id TEXT NOT NULL REFERENCES conversations,"
        "title TEXT NOT NULL,content TEXT NOT NULL,action TEXT NOT NULL,"
        "trigger_at TEXT NOT NULL,triggered_at TEXT)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS schedules_calendar "
        "ON schedules(user_id,conversation_id,trigger_at,id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS schedules_due ON schedules(trigger_at,id) "
        "WHERE triggered_at IS NULL"
    )


def stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Schedule times must include a time zone")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def list_entries(
    db: sqlite3.Connection,
    user_id: str,
    conversation_id: str,
    start: datetime | None,
    end: datetime | None,
    status: ScheduleStatus | None,
    limit: int,
    cursor: str | None,
    inbox_device: str | None = None,
) -> tuple[list[ScheduleEntry], str | None]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Schedule page size must be between 1 and 100")
    if start is not None and end is not None and stamp(start) >= stamp(end):
        raise ValueError("Schedule start must be earlier than end")
    conditions = ["user_id=?", "conversation_id=?"]
    params: list[str | int] = [user_id, conversation_id]
    if start is not None:
        conditions.append("trigger_at>=?")
        params.append(stamp(start))
    if end is not None:
        conditions.append("trigger_at<?")
        params.append(stamp(end))
    if status is not None:
        if status not in ("scheduled", "triggered"):
            raise ValueError("Unknown schedule status")
        conditions.append(
            "triggered_at IS NULL" if status == "scheduled" else "triggered_at IS NOT NULL"
        )
    if cursor is not None:
        when, _, entry_id = cursor.partition("|")
        if (
            not re.fullmatch(
                r"(?:sch|ntf)_[0-9a-f]{32}" if inbox_device else r"sch_[0-9a-f]{32}", entry_id
            )
            or stamp(datetime.fromisoformat(when)) != when
        ):
            raise ValueError("Invalid schedule cursor")
        conditions.append("(trigger_at,id)>(?,?)")
        params.extend((when, entry_id))
    source = "schedules"
    if inbox_device is not None:
        # Include delivered notices without a live schedule once, in their recipient's calendar.
        source = (
            "(SELECT id,user_id,conversation_id,title,content,action,trigger_at,triggered_at "
            "FROM schedules UNION ALL SELECT n.id,n.user_id,n.conversation_id,"
            "n.title,n.body,'notify_device',coalesce(n.notify_at,n.created_at),n.created_at "
            "FROM user_notifications n JOIN notification_deliveries d ON d.notification_seq=n.seq "
            "LEFT JOIN scheduled_notifications p ON p.id=n.id WHERE d.device_id=? "
            "AND p.schedule_id IS NULL)"
        )
        params.insert(0, inbox_device)
    rows = db.execute(
        f"SELECT {FIELDS} FROM {source} WHERE "
        + " AND ".join(conditions)
        + " ORDER BY trigger_at,id LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    items = [ScheduleEntry.model_validate(dict(r)) for r in rows[:limit]]
    next_cursor = (
        f"{rows[limit - 1]['trigger_at']}|{rows[limit - 1]['id']}" if len(rows) > limit else None
    )
    return items, next_cursor
