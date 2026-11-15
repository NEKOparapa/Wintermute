"""Read the stored inputs and outputs of timeline compression, within a conversation's scope."""

import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from wintermute.events.core.activity import ActivityItem, activity_message
from wintermute.events.core.presentation import timestamp_text
from wintermute.events.core.timeline_compression import TimelinePolicy
from wintermute.events.storage.read_snapshot import read_snapshot

_MAX_ID = 2**63 - 1
# Start the join from a single batch's indexed members. SQLite can otherwise scan a
# conversation's entire delivery history once for each candidate in a small page.
_VISIBLE = """
    CROSS JOIN activity_deliveries d ON d.activity_seq=m.activity_seq
                                     AND d.conversation_id=:conversation
    CROSS JOIN activity_items i ON i.seq=m.activity_seq AND i.user_id=:user
"""
_MEMBERS = " FROM timeline_batch_members m " + _VISIBLE


def _number(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > _MAX_ID:
        raise ValueError("Invalid compression identifier")
    return int(value)


def _cursor(cursor: str, prefix: str) -> int:
    if not cursor.startswith(prefix):
        raise ValueError("Invalid compression cursor")
    return _number(cursor[len(prefix) :])


def _status(row: sqlite3.Row, policy: TimelinePolicy, ai_enabled: bool) -> str:
    if row["has_ai"]:
        return "ready"
    if not row["sealed"]:
        return "open"
    if row["rule_tokens"] <= policy.ai_threshold:
        return "rules"
    if not ai_enabled:
        return "disabled"
    return "retrying" if row["attempts"] else "pending"


def _batch(row: sqlite3.Row, policy: TimelinePolicy, ai_enabled: bool) -> dict[str, Any]:
    partial = row["visible_count"] != row["item_count"]
    first = row["visible_start"] if partial else row["first_at"]
    last = row["visible_end"] if partial else row["last_at"]
    return {
        "id": str(row["batch_id"]),
        "terminal_id": row["terminal_id"],
        "started_at": timestamp_text(datetime.fromtimestamp(first, UTC)),
        "ended_at": timestamp_text(datetime.fromtimestamp(last, UTC)),
        "perception_count": row["visible_count"] - row["slice_count"],
        "slice_count": row["slice_count"],
        "partial": partial,
        "status": "rules" if partial else _status(row, policy, ai_enabled),
    }


_OVERVIEW = (
    """
    SELECT b.batch_id,b.terminal_id,b.first_at,b.last_at,b.item_count,b.attempts,
           b.sealed,b.rule_tokens,b.ai_text IS NOT NULL AS has_ai,
           COUNT(*) AS visible_count,SUM(m.is_slice) AS slice_count,
           MIN(i.occurred_at) AS visible_start,MAX(i.occurred_at) AS visible_end
    FROM timeline_batches b
    CROSS JOIN timeline_batch_members m ON m.batch_id=b.batch_id
"""
    + _VISIBLE
    + " WHERE b.user_id=:user AND b.scope IN ('',:conversation)"
)


def browse_batches(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    layer: str,
    cursor: str | None,
    limit: int,
    policy: TimelinePolicy,
    ai_enabled: bool,
) -> dict[str, Any]:
    if layer not in {"perception", "slice"} or not 1 <= limit <= 100:
        raise ValueError("Invalid compression layer or limit")
    prefix = f"batches:{layer}:"
    params = {
        "conversation": conversation,
        "user": user,
        "layer": int(layer == "slice"),
        "through": _cursor(cursor, prefix) - 1 if cursor is not None else _MAX_ID,
        "limit": limit + 1,
    }
    # First seek a bounded page of batches having a visible member of the requested layer.
    # The per-batch counts then cover both layers, exactly like the cached compression.
    query = (
        _OVERVIEW + " AND b.batch_id IN (SELECT b.batch_id FROM timeline_batches b "
        "WHERE b.user_id=:user AND b.scope IN ('',:conversation) AND b.batch_id<=:through "
        "AND EXISTS(SELECT 1 " + _MEMBERS + " WHERE m.batch_id=b.batch_id AND m.is_slice=:layer) "
        "ORDER BY b.batch_id DESC LIMIT :limit) GROUP BY b.batch_id ORDER BY b.batch_id DESC"
    )
    with read_snapshot(db):
        rows = db.execute(query, params).fetchall()
        return {
            "items": [_batch(row, policy, ai_enabled) for row in rows[:limit]],
            "next_cursor": f"{prefix}{rows[limit - 1]['batch_id']}" if len(rows) > limit else None,
        }


def _rollup(
    db: sqlite3.Connection,
    params: dict[str, Any],
    policy: TimelinePolicy,
    ai_enabled: bool,
) -> dict[str, Any] | None:
    # A visible leaf can belong to a six-batch rollup containing another, hidden leaf.
    # Verify every member before returning even the existence of the summary.
    row = db.execute(
        "SELECT r.rule_text,r.ai_text,r.ai_text IS NOT NULL AS has_ai,"
        "r.rule_tokens,r.sealed,r.attempts FROM timeline_rollups r "
        "JOIN timeline_rollup_members anchor USING(rollup_id) "
        "WHERE anchor.batch_id=:batch AND r.sealed=1 AND r.user_id=:user "
        "AND r.scope IN ('',:conversation) AND NOT EXISTS ("
        "SELECT 1 FROM timeline_rollup_members rm JOIN timeline_batch_members m USING(batch_id) "
        "WHERE rm.rollup_id=r.rollup_id AND NOT EXISTS (SELECT 1 FROM activity_deliveries d "
        "WHERE d.activity_seq=m.activity_seq AND d.conversation_id=:conversation))",
        params,
    ).fetchone()
    if row is None:
        return None
    return {
        "rule_text": row["rule_text"] or None,
        "ai_text": row["ai_text"],
        "status": _status(row, policy, ai_enabled),
    }


def batch_detail(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    batch_id: str,
    cursor: str | None,
    limit: int,
    policy: TimelinePolicy,
    ai_enabled: bool,
) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise ValueError("Invalid compression page size")
    params: dict[str, Any] = {
        "user": user,
        "conversation": conversation,
        "batch": _number(batch_id),
        "limit": limit + 1,
    }
    prefix = f"members:{batch_id}:"
    with read_snapshot(db):
        row = db.execute(
            _OVERVIEW + " AND b.batch_id=:batch GROUP BY b.batch_id",
            params,
        ).fetchone()
        if row is None:
            raise LookupError("Unknown compression batch")
        batch = _batch(row, policy, ai_enabled)
        rule_text, ai_text, rollup = None, None, None
        if not batch["partial"]:
            rule_text, ai_text = db.execute(
                "SELECT rule_text,ai_text FROM timeline_batches WHERE batch_id=:batch",
                params,
            ).fetchone()
            rollup = _rollup(db, params, policy, ai_enabled)
        members = _MEMBERS + " WHERE m.batch_id=:batch"
        after = ""
        if cursor is not None:
            params["sequence"] = _cursor(cursor, prefix)
            anchor = db.execute(
                "SELECT i.occurred_at " + members + " AND i.seq=:sequence", params
            ).fetchone()
            if anchor is None:
                raise ValueError("Unknown compression member cursor")
            params["time"] = anchor[0]
            after = " AND (i.occurred_at,i.seq)>(:time,:sequence)"
        rows = db.execute(
            "SELECT i.seq,i.data " + members + after + " ORDER BY i.occurred_at,i.seq LIMIT :limit",
            params,
        ).fetchall()
        items = []
        for entry in rows[:limit]:
            item = ActivityItem.model_validate_json(entry["data"])
            message = activity_message(item)
            items.append(
                {
                    "id": item.activity_id,
                    "layer": item.kind,
                    "occurred_at": timestamp_text(item.occurred_at),
                    "source": str(message.origin).removeprefix("活动更新 · "),
                    "text": message.text,
                    "source_count": len(item.source_event_ids),
                }
            )
        return {
            "batch": batch,
            "rule_text": rule_text or None,
            "ai_text": ai_text,
            "rollup": rollup,
            "items": items,
            "next_cursor": f"{prefix}{rows[limit - 1]['seq']}" if len(rows) > limit else None,
        }
