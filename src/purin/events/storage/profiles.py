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
    text: str
    previous_reply: str | None


@dataclass(frozen=True)
class ProfileStatus:
    now: datetime
    cursor: int
    latest_seq: int
    busy: bool


_USER_INPUTS = (
    " FROM dialogue_entries d JOIN conversation_events e ON e.event_id=d.event_id "
    "WHERE d.user_id=? AND d.event_id IS NOT NULL "
)


def status(db: sqlite3.Connection, user: str, now: datetime) -> ProfileStatus:
    row = db.execute("SELECT cursor FROM profile_progress WHERE user_id=?", (user,)).fetchone()
    cursor = row["cursor"] if row else 0
    latest = db.execute(
        "SELECT MAX(seq) FROM dialogue_entries WHERE user_id=? AND event_id IS NOT NULL", (user,)
    ).fetchone()[0]
    busy = db.execute(
        "SELECT 1 FROM dispatch_jobs WHERE user_id=? AND status IN ('queued','running') LIMIT 1",
        (user,),
    ).fetchone()
    return ProfileStatus(
        now=now,
        cursor=cursor,
        latest_seq=latest or 0,
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
        "json_extract(e.data,'$.payload.text') text,"
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
            row["text"],
            row["reply"],
        )
        for row in rows
    )
