"""User-scoped dialogue cursor for the background memory worker."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ProfileInput:
    seq: int
    event_id: str
    conversation_id: str
    occurred_at: datetime
    received_at: datetime
    text: str
    previous_reply: str | None


@dataclass(frozen=True)
class ProfileStatus:
    now: datetime
    cursor: int
    latest_seq: int
    latest_received_at: datetime | None
    oldest_pending_at: datetime | None
    busy: bool


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS profile_progress (user_id TEXT PRIMARY KEY, "
        "cursor INTEGER NOT NULL CHECK(cursor>=0))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS profile_input_order ON dialogue_entries(user_id,seq) "
        "WHERE event_id IS NOT NULL"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS profile_busy ON dispatch_jobs(user_id) "
        "WHERE status IN ('queued','running')"
    )


_USER_INPUTS = (
    " FROM dialogue_entries d JOIN conversation_events e ON e.event_id=d.event_id "
    "WHERE d.user_id=? AND d.event_id IS NOT NULL "
)


def status(db: sqlite3.Connection, user: str, now: datetime) -> ProfileStatus:
    row = db.execute(
        "SELECT cursor FROM profile_progress WHERE user_id=?", (user,)
    ).fetchone()
    cursor = row["cursor"] if row else 0
    columns = "SELECT d.seq,json_extract(e.data,'$.received_at') AS received"
    latest = db.execute(columns + _USER_INPUTS + "ORDER BY d.seq DESC LIMIT 1", (user,)).fetchone()
    oldest = db.execute(
        columns + _USER_INPUTS + "AND d.seq>? ORDER BY d.seq LIMIT 1", (user, cursor)
    ).fetchone()
    busy = db.execute(
        "SELECT 1 FROM dispatch_jobs WHERE user_id=? AND status IN ('queued','running') LIMIT 1",
        (user,),
    ).fetchone()
    return ProfileStatus(
        now=now,
        cursor=cursor,
        latest_seq=latest["seq"] if latest else 0,
        latest_received_at=datetime.fromisoformat(latest["received"]) if latest else None,
        oldest_pending_at=datetime.fromisoformat(oldest["received"]) if oldest else None,
        busy=busy is not None,
    )


def inputs(
    db: sqlite3.Connection, user: str, after: int, through: int, limit: int
) -> tuple[ProfileInput, ...]:
    if not 0 <= after <= through or not 1 <= limit <= 1000:
        raise ValueError("Invalid profile input window")
    # Use the preceding visible chat turn only when it was an actual conversation reply.
    # A new user message or a notification in between breaks the reference.
    rows = db.execute(
        "SELECT d.seq,d.conversation_id,e.event_id,json_extract(e.data,'$.occurred_at') occurred,"
        "json_extract(e.data,'$.received_at') received,json_extract(e.data,'$.payload.text') text,"
        "(SELECT CASE WHEN o.layer='conversation' THEN o.text END FROM dialogue_entries p "
        "LEFT JOIN outputs o ON o.output_id=p.output_id WHERE p.user_id=d.user_id "
        "AND p.conversation_id=d.conversation_id AND p.seq<d.seq ORDER BY p.seq DESC LIMIT 1) reply"
        + _USER_INPUTS
        + "AND d.seq>? AND d.seq<=? ORDER BY d.seq LIMIT ?",
        (user, after, through, limit),
    )
    return tuple(
        ProfileInput(
            row["seq"],
            row["event_id"],
            row["conversation_id"],
            datetime.fromisoformat(row["occurred"]),
            datetime.fromisoformat(row["received"]),
            row["text"],
            row["reply"],
        )
        for row in rows
    )
