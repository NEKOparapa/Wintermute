"""Read the stored inputs and outputs of timeline compression, within a conversation's scope."""

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from wintermute.events.core.activity import ActivityItem, activity_message, perception_activity
from wintermute.events.core.perception_rules import (
    SHORT_DWELL_SECONDS,
    RuleDecision,
    filter_perception,
)
from wintermute.events.core.presentation import timestamp_text
from wintermute.events.core.timeline_compression import TimelinePolicy, estimate
from wintermute.events.core.types import EventRecord
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
    status = "rules" if partial else _status(row, policy, ai_enabled)
    if row["round_status"] in ("ready", "pending", "retrying"):
        status = row["round_status"] if ai_enabled or row["round_status"] == "ready" else "disabled"
    return {
        "id": str(row["batch_id"]),
        "terminal_id": row["terminal_id"],
        "started_at": timestamp_text(datetime.fromtimestamp(first, UTC)),
        "ended_at": timestamp_text(datetime.fromtimestamp(last, UTC)),
        "perception_count": row["visible_count"] - row["slice_count"],
        "slice_count": row["slice_count"],
        "partial": partial,
        "status": status,
    }


_ROUND = """
    FROM perception_rounds r JOIN timeline_batches j ON j.batch_id=r.batch_id
    WHERE r.conversation_id=:conversation AND r.through_seq>=m.activity_seq
    AND r.after_seq<m.activity_seq AND m.perception_line!=''
    AND (j.ai_text IS NOT NULL OR EXISTS (SELECT 1 FROM perception_buffers s
        WHERE s.conversation_id=r.conversation_id AND s.job_id=r.batch_id))
    ORDER BY r.through_seq LIMIT 1
"""

_OVERVIEW = (
    """
    SELECT b.batch_id,b.terminal_id,b.first_at,b.last_at,b.item_count,b.attempts,
           b.sealed,b.rule_tokens,b.ai_text IS NOT NULL AS has_ai,
           COUNT(*) AS visible_count,SUM(m.is_slice) AS slice_count,
           MIN(i.occurred_at) AS visible_start,MAX(i.occurred_at) AS visible_end,
           (SELECT CASE WHEN j.ai_text IS NOT NULL THEN 'ready' WHEN j.attempts>0 THEN 'retrying'
               ELSE 'pending' END """
    + _ROUND
    + """) AS round_status
    FROM timeline_batches b
    CROSS JOIN timeline_batch_members m ON m.batch_id=b.batch_id
"""
    + _VISIBLE
    + " WHERE b.user_id=:user AND b.scope IN ('',:conversation)"
)


def _instant(at: float | None) -> str | None:
    return None if at is None else timestamp_text(datetime.fromtimestamp(at, UTC))


def _envelope(message: dict[str, Any], limit: int | None = None) -> dict[str, Any]:
    text = str(message.get("text") or "")
    if limit is not None and len(text) > limit:
        text = text[:limit] + "…"
    return {"text": text, "timestamp": message.get("timestamp"), "origin": message.get("origin")}


def _delivery(db: sqlite3.Connection, run_id: str | None, index: int | None) -> dict[str, Any]:
    """Locate the committed timeline message that carried this activity into a run."""
    empty: dict[str, Any] = {
        "run_id": run_id,
        "input_index": index,
        "delivered_at": None,
        "message": None,
        "trigger": None,
    }
    if run_id is None or index is None:
        return empty
    row = db.execute(
        "SELECT h.seq,j.messages FROM dispatch_jobs j LEFT JOIN session_history h "
        "ON h.run_id=j.job_id WHERE j.job_id=?",
        (run_id,),
    ).fetchone()
    if row is None or not row["messages"]:
        return empty
    transcript = json.loads(row["messages"])
    if not 0 <= index < len(transcript) or transcript[index].get("source") != "timeline":
        return empty

    def asked(message: dict[str, Any]) -> bool:
        return message.get("role") == "user" and message.get("source") != "timeline"

    # The request that read it usually follows the timeline. A mid-run read follows a tool call.
    trigger = next((m for m in transcript[index + 1 :] if asked(m)), None) or next(
        (m for m in reversed(transcript[:index]) if asked(m)), None
    )
    entry = transcript[index]
    return {
        "run_id": run_id,
        "input_index": index,
        "delivered_at": entry.get("timestamp"),
        "message": {
            "id": f"history-{row['seq']}-{index}" if row["seq"] is not None else None,
            **_envelope(entry),
        },
        "trigger": None if trigger is None else _envelope(trigger, 160),
    }


_ROUND_MEMBERS = (
    " FROM activity_deliveries d JOIN activity_items i ON i.seq=d.activity_seq "
    "JOIN timeline_batch_members m ON m.activity_seq=i.seq WHERE d.conversation_id=? "
    "AND i.seq>? AND i.seq<=? AND m.perception_line!=''"
)


def _round_members(db: sqlite3.Connection, conversation: str, batch: int) -> dict[str, Any]:
    span = db.execute(
        "SELECT after_seq,through_seq FROM perception_rounds WHERE batch_id=?", (batch,)
    ).fetchone()
    if span is None:
        return {"members": [], "member_count": 0}
    params = (conversation, span["after_seq"], span["through_seq"])
    count = db.execute("SELECT COUNT(*)" + _ROUND_MEMBERS, params).fetchone()[0]
    members = [
        {
            "id": source or activity,
            "kind": "observation" if activity == source else "visual",
            "occurred_at": _instant(at),
            "text": line,
        }
        for source, activity, at, line in db.execute(
            "SELECT json_extract(i.data,'$.source_event_ids[0]'),i.activity_id,i.occurred_at,"
            "substr(m.perception_line,1,160)"
            + _ROUND_MEMBERS
            + " ORDER BY i.occurred_at,i.seq LIMIT 50",
            params,
        )
    ]
    return {"members": members, "member_count": count}


def _later_rounds(
    db: sqlite3.Connection,
    conversation: str,
    batch: int,
    status_of: Callable[[sqlite3.Row], str],
) -> list[dict[str, Any]]:
    """Follow the rolling checkpoint: a later round starts from this round's stored summary."""
    chain: list[dict[str, Any]] = []
    current = batch
    for _ in range(20):
        row = db.execute(
            "SELECT r.through_seq,b.ai_text FROM perception_rounds r JOIN timeline_batches b "
            "ON b.batch_id=r.batch_id WHERE r.batch_id=?",
            (current,),
        ).fetchone()
        if row is None or row["ai_text"] is None:
            break
        following = db.execute(
            "SELECT b.batch_id,b.rule_text,b.ai_text,b.attempts,b.first_at,b.last_at "
            "FROM perception_rounds r JOIN timeline_batches b ON b.batch_id=r.batch_id "
            "WHERE r.conversation_id=? AND r.after_seq=? AND r.batch_id>? "
            "ORDER BY r.batch_id LIMIT 1",
            (conversation, row["through_seq"], current),
        ).fetchone()
        # A read resets the cursor; only a round seeded with this exact summary continues it.
        if following is None or not str(following["rule_text"] or "").startswith(
            "已有摘要：\n" + row["ai_text"]
        ):
            break
        chain.append(
            {
                "id": str(following["batch_id"]),
                "status": status_of(following),
                "summary": following["ai_text"],
                "started_at": _instant(following["first_at"]),
                "ended_at": _instant(following["last_at"]),
            }
        )
        current = following["batch_id"]
    return chain


def perception_stages(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    event_id: str,
    policy: TimelinePolicy,
    ai_enabled: bool,
) -> dict[str, Any]:
    """Inspect the reported event, its visible projections and their actual frozen AI rounds."""
    params = {
        "user": user,
        "conversation": conversation,
        "event": event_id,
        "visual": event_id + ":visual",
    }
    with read_snapshot(db):
        raw = db.execute(
            "SELECT data FROM perception_events WHERE event_id=:event AND user_id=:user "
            "AND (conversation_id IS NULL OR conversation_id=:conversation)",
            params,
        ).fetchone()
        if raw is None:
            raise LookupError("Unknown perception")
        event = EventRecord.model_validate_json(raw[0])
        rows = db.execute(
            "SELECT i.activity_id,i.occurred_at,m.dwell_threshold,m.perception_line,"
            "d.applied,d.input_run_id,d.input_index,j.batch_id,j.first_at,j.last_at,"
            "j.rule_text,j.ai_text,j.attempts FROM activity_items i "
            "JOIN activity_deliveries d ON d.activity_seq=i.seq "
            "AND d.conversation_id=:conversation "
            "JOIN timeline_batch_members m ON m.activity_seq=i.seq "
            "LEFT JOIN timeline_batches j ON j.batch_id=(SELECT j.batch_id " + _ROUND + ") "
            "WHERE i.user_id=:user AND i.activity_id IN (:event,:visual) "
            "AND json_extract(i.data,'$.source_event_ids[0]')=:event "
            "AND m.perception_line IS NOT NULL ORDER BY i.occurred_at,i.seq",
            params,
        ).fetchall()
        decisions: list[RuleDecision] = []
        threshold = rows[0]["dwell_threshold"] if rows else SHORT_DWELL_SECONDS
        kept = (
            filter_perception(perception_activity(event), decisions, threshold=threshold)
            if rows
            else None
        )
        parts = []
        rounds = {}

        def status_of(row: sqlite3.Row) -> str:
            if row["ai_text"] is not None:
                return "ready"
            if not ai_enabled:
                return "disabled"
            return "retrying" if row["attempts"] else "pending"

        for row in rows:
            round_id = str(row["batch_id"]) if row["batch_id"] is not None else None
            later: list[dict[str, Any]] = []
            if round_id is not None and round_id not in rounds:
                later = _later_rounds(db, conversation, row["batch_id"], status_of)
                rounds[round_id] = {
                    "id": round_id,
                    "status": status_of(row),
                    "input": row["rule_text"],
                    "summary": row["ai_text"],
                    "started_at": _instant(row["first_at"]),
                    "ended_at": _instant(row["last_at"]),
                    **_round_members(db, conversation, row["batch_id"]),
                    "later": later,
                    "next_id": later[0]["id"] if later else None,
                }
            delivery = None
            if row["applied"]:
                delivery = _delivery(db, row["input_run_id"], row["input_index"])
                if delivery["message"] is not None:
                    msg_text = str(delivery["message"].get("text") or "")
                    round_entry = rounds.get(round_id) if round_id is not None else None
                    round_summaries = (
                        [
                            round_entry["summary"],
                            *(entry["summary"] for entry in round_entry["later"]),
                        ]
                        if round_entry
                        else []
                    )
                    used_summary = any(
                        summary and ("感知摘要：" + summary[:24]) in msg_text
                        for summary in round_summaries
                    )
                    delivery["form"] = (
                        "summary"
                        if round_id is not None
                        and (
                            used_summary
                            or (
                                "感知摘要：" in msg_text
                                and row["perception_line"]
                                and row["perception_line"] not in msg_text
                            )
                        )
                        else "raw"
                    )
                else:
                    delivery["form"] = None
            parts.append(
                {
                    "kind": "observation" if row["activity_id"] == event_id else "visual",
                    "text": row["perception_line"],
                    "delivered": bool(row["applied"]),
                    "round_id": round_id,
                    "delivery": delivery,
                }
            )
        size = db.execute(
            "SELECT s.tail_cost+COALESCE(length(CAST(j.ai_text AS BLOB)),-1) "
            "FROM perception_buffers s LEFT JOIN timeline_batches j ON j.batch_id=s.previous_id "
            "WHERE s.conversation_id=:conversation",
            params,
        ).fetchone()
        return {
            "dwell_threshold_seconds": threshold,
            "decisions": decisions,
            "retained": kept.content if kept is not None else None,
            "parts": parts,
            "rounds": list(rounds.values()),
            "ai_enabled": ai_enabled,
            "budget": policy.delivery_tokens,
            "pending_size": max(0, size[0]) if size else 0,
        }


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
        "AND EXISTS(SELECT 1 " + _MEMBERS + " WHERE m.batch_id=b.batch_id AND m.is_slice=:layer "
        "AND m.perception_line IS NOT '') "
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
            if row["round_status"]:
                result = db.execute(
                    "SELECT (SELECT j.ai_text " + _ROUND + "),"
                    "(SELECT j.rule_text " + _ROUND + ") FROM timeline_batch_members m "
                    "WHERE m.batch_id=:batch",
                    params,
                ).fetchone()
                ai_text = result[0]
                rollup = {"rule_text": result[1], "ai_text": ai_text, "status": batch["status"]}
            elif not rule_text and not batch["slice_count"]:
                rule_text = "短停留已过滤。"
            else:
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


def _match_summary_round(
    db: sqlite3.Connection, conversation: str, msg_text: str, max_seq: int | None
) -> sqlite3.Row | None:
    for line in msg_text.splitlines():
        if not line.startswith("感知摘要："):
            continue
        excerpt = line.removeprefix("感知摘要：").rstrip("…")
        candidates: list[sqlite3.Row] = db.execute(
            "SELECT r.batch_id,r.after_seq,r.through_seq,b.first_at,b.last_at,b.ai_text "
            "FROM perception_rounds r JOIN timeline_batches b ON b.batch_id=r.batch_id "
            "WHERE r.conversation_id=? AND b.ai_text IS NOT NULL "
            "ORDER BY r.through_seq DESC,r.batch_id DESC",
            (conversation,),
        ).fetchall()
        matched = [
            row
            for row in candidates
            if str(row["ai_text"]).startswith(excerpt)
            or (len(excerpt) >= 12 and excerpt[:12] in str(row["ai_text"]))
        ]
        if max_seq is not None:
            bounded = [row for row in matched if row["through_seq"] <= max_seq]
            if bounded:
                return bounded[0]
        if matched:
            return matched[0]
    return None


def timeline_sources(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    message_id: str,
) -> dict[str, Any]:
    """Break a delivered timeline message down into summaries, raw perception lines, and slices."""
    match = re.fullmatch(r"history-([1-9][0-9]{0,18})-([0-9]{1,10})", message_id)
    if not match or int(match[1]) > _MAX_ID:
        raise ValueError("Invalid timeline message identifier")
    seq, index = int(match[1]), int(match[2])
    with read_snapshot(db):
        row = db.execute(
            "SELECT h.seq,h.run_id,h.activity_seq,j.messages FROM session_history h "
            "LEFT JOIN dispatch_jobs j ON j.job_id=h.run_id "
            "WHERE h.conversation_id=? AND h.seq=?",
            (conversation, seq),
        ).fetchone()
        if row is None:
            raise LookupError("Unknown timeline message")
        if row["activity_seq"] is not None:
            if index != 0:
                raise LookupError("Unknown timeline message")
            item_row = db.execute(
                "SELECT i.seq,i.data,m.batch_id,m.is_slice,m.perception_line "
                "FROM activity_items i LEFT JOIN timeline_batch_members m ON m.activity_seq=i.seq "
                "WHERE i.seq=? AND i.user_id=?",
                (row["activity_seq"], user),
            ).fetchone()
            if item_row is None:
                raise LookupError("Unknown timeline message")
            item = ActivityItem.model_validate_json(item_row["data"])
            entry = activity_message(item)
            text = entry.text
            part_kind = "perception_line" if item.kind == "perception" else "slice"
            part: dict[str, Any] = {
                "kind": part_kind,
                "activity_id": item.activity_id,
                "occurred_at": timestamp_text(item.occurred_at),
                "text": item_row["perception_line"] or text,
                "bytes": estimate(item_row["perception_line"] or text),
                "source": str(entry.origin or "活动时间线"),
                "source_event_ids": list(item.source_event_ids),
            }
            if item.kind == "perception":
                part["event_id"] = (
                    item.source_event_ids[0] if item.source_event_ids else item.activity_id
                )
                part["line_kind"] = (
                    "visual" if item.activity_id.endswith(":visual") else "observation"
                )
            elif item_row["batch_id"] is not None:
                part["batch_id"] = str(item_row["batch_id"])
            return {
                "message": {
                    "id": message_id,
                    "text": text,
                    "timestamp": timestamp_text(item.occurred_at),
                    "origin": entry.origin or "活动时间线",
                    "bytes": estimate(text),
                },
                "parts": [part],
                "counts": {
                    "total_activities": 1,
                    "summary_count": 0,
                    "perception_count": 1 if item.kind == "perception" else 0,
                    "slice_count": 1 if item.kind == "slice" else 0,
                },
            }

        if not row["messages"]:
            raise LookupError("Unknown timeline message")
        transcript = json.loads(row["messages"])
        if not 0 <= index < len(transcript):
            raise LookupError("Unknown timeline message")
        msg = transcript[index]
        if msg.get("role") != "user" or msg.get("source") != "timeline":
            raise LookupError("Unknown timeline message")
        msg_text = str(msg.get("text") or "")
        rows = db.execute(
            "SELECT i.seq,i.activity_id,i.occurred_at,i.data,"
            "m.batch_id,m.is_slice,m.perception_line,"
            "b.ai_text AS batch_ai_text,b.first_at AS batch_first,b.last_at AS batch_last "
            "FROM activity_deliveries d "
            "JOIN activity_items i ON i.seq=d.activity_seq "
            "LEFT JOIN timeline_batch_members m ON m.activity_seq=i.seq "
            "LEFT JOIN timeline_batches b ON b.batch_id=m.batch_id "
            "WHERE d.conversation_id=? AND d.input_run_id=? AND d.input_index=? AND i.user_id=? "
            "ORDER BY i.occurred_at,i.seq",
            (conversation, row["run_id"], index, user),
        ).fetchall()

        perception_seqs = [r["seq"] for r in rows if not r["is_slice"] and r["perception_line"]]
        summary_round = _match_summary_round(
            db, conversation, msg_text, max(perception_seqs) if perception_seqs else None
        )
        through_seq = summary_round["through_seq"] if summary_round is not None else -1

        parts: list[dict[str, Any]] = []
        first_line = msg_text.splitlines()[0] if msg_text else ""
        if first_line.startswith("注意：") or first_line == "顺序修订：":
            parts.append({"kind": "note", "text": first_line, "bytes": estimate(first_line)})

        summary_members: list[dict[str, Any]] = []
        raw_lines: list[tuple[float, dict[str, Any]]] = []
        slice_parts: list[tuple[float, dict[str, Any]]] = []
        slice_batches_seen: set[int] = set()

        for r in rows:
            item = ActivityItem.model_validate_json(r["data"])
            entry = activity_message(item)
            event_id = item.source_event_ids[0] if item.source_event_ids else item.activity_id
            if not r["is_slice"] and r["perception_line"]:
                line = str(r["perception_line"])
                line_kind = "visual" if item.activity_id.endswith(":visual") else "observation"
                if summary_round is not None and r["seq"] <= through_seq and line not in msg_text:
                    summary_members.append(
                        {
                            "event_id": event_id,
                            "activity_id": item.activity_id,
                            "kind": line_kind,
                            "occurred_at": timestamp_text(item.occurred_at),
                            "text": line,
                        }
                    )
                else:
                    raw_lines.append(
                        (
                            r["occurred_at"],
                            {
                                "kind": "perception_line",
                                "event_id": event_id,
                                "activity_id": item.activity_id,
                                "line_kind": line_kind,
                                "occurred_at": timestamp_text(item.occurred_at),
                                "text": line,
                                "bytes": estimate(line),
                            },
                        )
                    )
            else:
                batch_ai = r["batch_ai_text"]
                if r["batch_id"] is not None and batch_ai and str(batch_ai)[:20] in msg_text:
                    if r["batch_id"] not in slice_batches_seen:
                        slice_batches_seen.add(r["batch_id"])
                        summary_text = str(batch_ai)
                        slice_parts.append(
                            (
                                r["batch_last"] or r["occurred_at"],
                                {
                                    "kind": "slice",
                                    "activity_id": item.activity_id,
                                    "batch_id": str(r["batch_id"]),
                                    "occurred_at": _instant(r["batch_first"])
                                    or timestamp_text(item.occurred_at),
                                    "ended_at": _instant(r["batch_last"]),
                                    "text": summary_text,
                                    "bytes": estimate(summary_text),
                                    "source": str(entry.origin or "切片"),
                                    "source_event_ids": list(item.source_event_ids),
                                    "summarized": True,
                                },
                            )
                        )
                    continue
                slice_text = entry.text
                slice_parts.append(
                    (
                        r["occurred_at"],
                        {
                            "kind": "slice",
                            "activity_id": item.activity_id,
                            "batch_id": str(r["batch_id"]) if r["batch_id"] is not None else None,
                            "occurred_at": timestamp_text(item.occurred_at),
                            "text": slice_text,
                            "bytes": estimate(slice_text),
                            "source": str(entry.origin or "切片"),
                            "source_event_ids": list(item.source_event_ids),
                            "summarized": False,
                        },
                    )
                )

        timeline_items: list[tuple[float, dict[str, Any]]] = []
        if summary_round is not None:
            summary_text = "感知摘要：" + str(summary_round["ai_text"])
            timeline_items.append(
                (
                    float(summary_round["last_at"]),
                    {
                        "kind": "perception_summary",
                        "round_id": str(summary_round["batch_id"]),
                        "started_at": _instant(summary_round["first_at"]),
                        "ended_at": _instant(summary_round["last_at"]),
                        "text": summary_text,
                        "summary": str(summary_round["ai_text"]),
                        "bytes": estimate(summary_text),
                        "member_count": len(summary_members),
                        "members": summary_members,
                    },
                )
            )
        timeline_items.extend(raw_lines)
        timeline_items.extend(slice_parts)
        timeline_items.sort(key=lambda pair: pair[0])
        parts.extend(item for _, item in timeline_items)

        return {
            "message": {
                "id": message_id,
                "text": msg_text,
                "timestamp": msg.get("timestamp"),
                "origin": msg.get("origin") or "活动时间线",
                "bytes": estimate(msg_text),
            },
            "parts": parts,
            "counts": {
                "total_activities": len(rows),
                "summary_count": 1 if summary_round is not None else 0,
                "perception_count": len(summary_members) + len(raw_lines),
                "slice_count": len(slice_parts),
            },
        }


def perception_summary(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    *,
    start: float | None,
    end: float | None,
    policy: TimelinePolicy,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate perception pipeline counts for a time window (defaults to today in policy TZ)."""
    from wintermute.events.storage.history import _EVENT_TIME, _destination

    tz = ZoneInfo(policy.timezone)
    if start is None or end is None:
        today = (now or datetime.now(tz)).astimezone(tz).date()
        local_start = datetime.combine(today, time.min, tzinfo=tz)
        local_end = local_start + timedelta(days=1)
        if start is None:
            start = local_start.timestamp()
        if end is None:
            end = local_end.timestamp()
    start_ms = round(start * 1000)
    end_ms = round(end * 1000)
    with read_snapshot(db):
        event_rows = db.execute(
            f"SELECT event_id,data FROM perception_events WHERE user_id=? "
            f"AND (conversation_id=? OR conversation_id IS NULL) "
            f"AND {_EVENT_TIME}>=? AND {_EVENT_TIME}<?",
            (user, conversation, start_ms, end_ms),
        ).fetchall()
        ids = [row["event_id"] for row in event_rows]
        projections: dict[str, list[sqlite3.Row]] = {}
        thresholds: dict[str, int] = {}
        observation_lines = 0
        visual_lines = 0
        round_ids: set[int] = set()
        summarized_events: set[str] = set()
        delivery_keys: set[tuple[str, int]] = set()
        if ids:
            for row in db.execute(
                "SELECT json_extract(i.data,'$.source_event_ids[0]') source,"
                "i.activity_id,m.dwell_threshold,m.perception_line!='' has_line,"
                "d.applied,d.input_run_id,d.input_index,d.input_index IS NOT NULL carried,"
                "(SELECT r.batch_id FROM perception_rounds r "
                "WHERE r.conversation_id=d.conversation_id AND r.after_seq<i.seq "
                "AND r.through_seq>=i.seq ORDER BY r.through_seq DESC LIMIT 1) round_id "
                "FROM activity_items i "
                "JOIN activity_deliveries d ON d.activity_seq=i.seq AND d.conversation_id=? "
                "JOIN timeline_batch_members m ON m.activity_seq=i.seq "
                "WHERE i.user_id=? AND m.perception_line IS NOT NULL "
                "AND i.activity_id IN (SELECT value FROM json_each(?))",
                (conversation, user, json.dumps(ids + [eid + ":visual" for eid in ids])),
            ):
                source = row["source"]
                projections.setdefault(source, []).append(row)
                thresholds[source] = row["dwell_threshold"]
                if row["has_line"]:
                    if row["activity_id"] == source:
                        observation_lines += 1
                    else:
                        visual_lines += 1
                    if row["round_id"] is not None:
                        round_ids.add(row["round_id"])
                        summarized_events.add(source)
                    if row["applied"] and row["input_run_id"] and row["input_index"] is not None:
                        delivery_keys.add((row["input_run_id"], row["input_index"]))
        dest_counts = {
            "filtered": 0,
            "buffered": 0,
            "summarized": 0,
            "delivered": 0,
            "acknowledged": 0,
            "unprojected": 0,
        }
        partial = 0
        for row in event_rows:
            eid = row["event_id"]
            found = projections.get(eid)
            if not found:
                dest_counts["unprojected"] += 1
                continue
            # _destination expects 'summarized' column; round_id is not None plays that role.
            normalized = [
                {
                    "has_line": r["has_line"],
                    "applied": r["applied"],
                    "carried": r["carried"],
                    "summarized": r["round_id"] is not None,
                }
                for r in found
            ]
            dest = _destination(normalized)  # type: ignore[arg-type]
            dest_counts[dest] = dest_counts.get(dest, 0) + 1
            if dest != "filtered":
                event = EventRecord.model_validate_json(row["data"])
                decisions: list[RuleDecision] = []
                filter_perception(
                    perception_activity(event),
                    decisions,
                    threshold=thresholds.get(eid, SHORT_DWELL_SECONDS),
                )
                if any(not d["kept"] for d in decisions):
                    partial += 1

        size = db.execute(
            "SELECT s.tail_cost+COALESCE(length(CAST(j.ai_text AS BLOB)),-1) "
            "FROM perception_buffers s LEFT JOIN timeline_batches j ON j.batch_id=s.previous_id "
            "WHERE s.conversation_id=?",
            (conversation,),
        ).fetchone()
        pending_bytes = max(0, size[0]) if size else 0
        kept = len(event_rows) - dest_counts["filtered"] - dest_counts["unprojected"]
        return {
            "from": _instant(start),
            "to": _instant(end),
            "uploaded": len(event_rows),
            "filtered": dest_counts["filtered"],
            "kept": kept,
            "partial": partial,
            "observation_lines": observation_lines,
            "visual_lines": visual_lines,
            "round_count": len(round_ids),
            "summarized_events": len(summarized_events),
            "buffered_events": dest_counts["buffered"],
            "delivered_events": dest_counts["delivered"],
            "acknowledged_events": dest_counts["acknowledged"],
            "delivery_runs": len(delivery_keys),
            "destinations": dest_counts,
            "pending_bytes": pending_bytes,
            "budget": policy.delivery_tokens,
        }
