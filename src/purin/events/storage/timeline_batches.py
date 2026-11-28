"""Incremental, device-scoped summaries. Conversation delivery owns a separate read cursor."""

import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from purin.agent.core.types import UserMessage
from purin.events.core.activity import ActivityItem
from purin.events.core.timeline_compression import (
    TimelinePolicy,
    batch_text,
    estimate,
    raw_size,
)
from purin.events.storage import merged_timeline, timeline_rollups


@dataclass(frozen=True, slots=True)
class TimelineBatch:
    batch_id: int
    rule_text: str
    attempts: int
    output_budget: int | None = None
    previous_prefix_chars: int = 0


def mark_read(db: sqlite3.Connection, conversation: str) -> None:
    db.execute("DELETE FROM perception_buffers WHERE conversation_id=?", (conversation,))
    db.execute(
        "INSERT INTO timeline_delivery_heads SELECT d.conversation_id,MAX(i.occurred_at) "
        "FROM activity_deliveries d JOIN activity_items i ON i.seq=d.activity_seq "
        "LEFT JOIN timeline_batch_members m ON m.activity_seq=i.seq "
        "WHERE d.conversation_id=? AND d.applied=0 AND m.perception_line IS NOT '' "
        "GROUP BY d.conversation_id "
        "ON CONFLICT(conversation_id) DO UPDATE SET max_at=MAX(max_at,excluded.max_at)",
        (conversation,),
    )


def append(
    db: sqlite3.Connection,
    seq: int,
    item: ActivityItem,
    policy: TimelinePolicy,
    screenshot_fact_bytes: int | None = None,
) -> None:
    scope = (item.user_id, item.conversation_id or "", item.device_id)
    head = db.execute(
        "SELECT * FROM timeline_batches WHERE user_id=? AND scope=? AND device_id=? AND sealed=0",
        scope,
    ).fetchone()
    day = item.occurred_at.astimezone(ZoneInfo(policy.timezone)).date().isoformat()
    occurred, known = item.occurred_at.timestamp(), item.known_at.timestamp()
    # A late source is isolated from the newer period; an already sealed batch cannot grow.
    if head and (
        day != head["day"]
        or known - head["last_known"] >= policy.idle_seconds
        or occurred - head["last_at"] >= policy.idle_seconds
        or occurred < head["first_at"]
    ):
        _seal(db, head["batch_id"], policy)
        head = None
    if head is None:
        cursor = db.execute(
            "INSERT INTO timeline_batches(user_id,scope,device_id,day,first_at,last_at,"
            "last_known) VALUES(?,?,?,?,?,?,?)",
            (*scope, day, occurred, occurred, known),
        )
        batch_id = cursor.lastrowid
        assert batch_id is not None
    else:
        batch_id = head["batch_id"]
    cost = raw_size(item)
    db.execute(
        "INSERT INTO timeline_batch_members(activity_seq,batch_id,is_slice,raw_tokens) "
        "VALUES(?,?,?,?)",
        (seq, batch_id, item.kind == "slice", cost),
    )
    # Recompute only this bounded open batch. Its cached fallback is ready after every commit.
    rows = db.execute(
        "SELECT i.data FROM timeline_batch_members m JOIN activity_items i ON i.seq=m.activity_seq "
        "WHERE m.batch_id=? ORDER BY i.occurred_at,i.seq",
        (batch_id,),
    )
    summary = batch_text([ActivityItem.model_validate_json(r[0]) for r in rows], policy)
    tokens = (head["raw_tokens"] if head else 0) + cost
    # NULL means at least one observation in this batch uses ordinary timeline rules.
    # Automatic screenshots only contribute bytes of new facts. Labels, timestamps,
    # visits without changes and references to original files do not trigger AI.
    if head and screenshot_fact_bytes is not None:
        prior_bytes = head["screenshot_fact_bytes"]
        screenshot_fact_bytes = None if prior_bytes is None else prior_bytes + screenshot_fact_bytes
    rule_cost = (
        estimate(summary.rule_text) if screenshot_fact_bytes is None else screenshot_fact_bytes
    )
    db.execute(
        "UPDATE timeline_batches SET first_at=?,last_at=?,last_known=?,item_count=?,raw_tokens=?,"
        "rule_text=?,stats_text=?,rule_tokens=?,screenshot_fact_bytes=? WHERE batch_id=?",
        (
            summary.start.timestamp(),
            summary.end.timestamp(),
            known,
            (head["item_count"] if head else 0) + 1,
            tokens,
            summary.rule_text,
            summary.stats_text,
            rule_cost,
            screenshot_fact_bytes,
            batch_id,
        ),
    )
    if (
        tokens >= policy.raw_tokens
        if screenshot_fact_bytes is None
        else screenshot_fact_bytes > policy.ai_threshold
    ):
        _seal(db, batch_id, policy)


def _seal(db: sqlite3.Connection, batch_id: int, policy: TimelinePolicy) -> None:
    db.execute("UPDATE timeline_batches SET sealed=1 WHERE batch_id=?", (batch_id,))
    timeline_rollups.add_sealed_batch(db, batch_id, policy)


def seal_idle(db: sqlite3.Connection, policy: TimelinePolicy, now: float) -> None:
    for row in db.execute(
        "SELECT batch_id FROM timeline_batches WHERE sealed=0 AND last_known<=?",
        (now - policy.idle_seconds,),
    ).fetchall():
        _seal(db, row[0], policy)


def next_batch(db: sqlite3.Connection, policy: TimelinePolicy, now: float) -> TimelineBatch | None:
    row = db.execute(
        "SELECT batch_id,rule_text,attempts FROM timeline_batches WHERE sealed=1 "
        "AND ai_text IS NULL AND rule_tokens>? AND retry_at<=? ORDER BY retry_at,batch_id LIMIT 1",
        (policy.ai_threshold, now),
    ).fetchone()
    return TimelineBatch(**dict(row)) if row else None


# Snapshot receipts never get a batch member. Include only the last visible version of
# each slice and suppress a revision that returns to the already delivered text.
_UNREAD_FROM = """
    FROM activity_deliveries d INDEXED BY activity_pending
    CROSS JOIN timeline_batch_members m ON m.activity_seq=d.activity_seq
    CROSS JOIN activity_items i ON i.seq=d.activity_seq
    """
_UNREAD_WHERE = """
    LEFT JOIN activity_items old ON old.seq=CASE WHEN m.is_slice THEN (
        SELECT a.activity_seq FROM activity_deliveries a WHERE a.conversation_id=d.conversation_id
        AND a.activity_key=d.activity_key AND a.applied=1 ORDER BY a.activity_seq DESC LIMIT 1
    ) END
    WHERE d.conversation_id=? AND d.applied=0 AND i.user_id=?
    AND m.perception_line IS NOT ''
    AND (m.is_slice=0 OR (
        old.content IS NOT i.content AND NOT EXISTS(
            SELECT 1 FROM activity_deliveries n WHERE n.conversation_id=d.conversation_id
            AND n.activity_key=d.activity_key AND n.activity_seq>d.activity_seq
        )
    ))
    """


# Only genuine late arrivals/revisions need the already-read part of the timeline.
_SUFFIX_FROM = """
    FROM activity_items i INDEXED BY activity_timeline
    CROSS JOIN activity_deliveries d ON d.activity_seq=i.seq
    LEFT JOIN timeline_batch_members m ON m.activity_seq=i.seq
    """
_SUFFIX_WHERE = """
    WHERE d.conversation_id=? AND i.user_id=? AND (i.occurred_at,i.seq)>=(?,?)
    AND m.perception_line IS NOT ''
    AND (COALESCE(m.is_slice,json_extract(i.data,'$.kind')='slice')=0 OR NOT EXISTS(
        SELECT 1 FROM activity_deliveries n WHERE n.conversation_id=d.conversation_id
        AND n.activity_key=d.activity_key AND n.activity_seq>d.activity_seq
    ))
    """


_VISIBLE = _UNREAD_FROM + _UNREAD_WHERE
_SUFFIX = _SUFFIX_FROM + _SUFFIX_WHERE


def pending(
    db: sqlite3.Connection,
    conversation: str,
    user: str,
    policy: TimelinePolicy,
    included: list[int] | None = None,
) -> UserMessage | None:
    scope: tuple[str | float | int, ...] = (conversation, user)
    # Locate a revision by its *earlier* position if its timestamp has changed. For an
    # ordinary poll the partial unread index makes an empty inbox an inexpensive lookup.
    changes = db.execute(
        "SELECT MIN(i.occurred_at),MIN(old.occurred_at) " + _VISIBLE, scope
    ).fetchone()
    if changes[0] is None:
        return None
    head = db.execute(
        "SELECT max_at FROM timeline_delivery_heads WHERE conversation_id=?", (conversation,)
    ).fetchone()
    revised = changes[1] is not None or (head is not None and changes[0] < head[0])
    query = _VISIBLE
    if revised:
        # A changed slice can move later as well as earlier. Use (time, seq) to avoid
        # replaying unchanged siblings with the exact same timestamp.
        start = db.execute(
            "SELECT i.occurred_at at,i.seq seq "
            + _VISIBLE
            + " UNION ALL SELECT old.occurred_at,old.seq "
            + _VISIBLE
            + " AND old.seq IS NOT NULL ORDER BY at,seq LIMIT 1",
            (*scope, *scope),
        ).fetchone()
        scope = (*scope, start[0], start[1])
        query = _SUFFIX

    return merged_timeline.pending(db, conversation, query, scope, revised, policy, included)
