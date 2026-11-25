"""Read the stored inputs and outputs of timeline compression, within a conversation's scope."""

import sqlite3
from typing import Any

from purin.events.core.activity import perception_activity
from purin.events.core.perception_rules import (
    SHORT_DWELL_SECONDS,
    RuleDecision,
    filter_perception,
)
from purin.events.core.timeline_compression import TimelinePolicy
from purin.events.core.types import EventRecord
from purin.events.storage.perception_trace import message_refs, summary_ref
from purin.events.storage.read_snapshot import read_snapshot

_ROUND = """
    FROM perception_rounds r JOIN timeline_batches j ON j.batch_id=r.batch_id
    WHERE r.conversation_id=:conversation AND r.through_seq>=m.activity_seq
    AND r.after_seq<m.activity_seq AND m.perception_line!=''
    AND (j.ai_text IS NOT NULL OR EXISTS (SELECT 1 FROM perception_buffers s
        WHERE s.conversation_id=r.conversation_id AND s.job_id=r.batch_id))
    ORDER BY r.through_seq LIMIT 1
"""


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
            "SELECT data,filter_reason FROM perception_events WHERE event_id=:event "
            "AND user_id=:user AND (conversation_id IS NULL OR conversation_id=:conversation)",
            params,
        ).fetchone()
        if raw is None:
            raise LookupError("Unknown perception")
        event = EventRecord.model_validate_json(raw[0])
        rows = db.execute(
            "SELECT i.activity_id,i.occurred_at,m.dwell_threshold,m.deduct_short,m.perception_line,"
            "m.perception_tokens,"
            "d.applied,j.batch_id,s.position_ms,s.generated_at,"
            "j.rule_text,COALESCE(r.original_summary,j.ai_text) AS ai_text,j.attempts,"
            "r.devices,r.duration_seconds FROM activity_items i "
            "CROSS JOIN activity_deliveries d ON d.activity_seq=i.seq "
            "AND d.conversation_id=:conversation "
            "JOIN timeline_batch_members m ON m.activity_seq=i.seq "
            "LEFT JOIN timeline_batches j ON j.batch_id=(SELECT j.batch_id " + _ROUND + ") "
            "LEFT JOIN perception_summary_history s ON s.batch_id=j.batch_id "
            "LEFT JOIN perception_rounds r ON r.batch_id=j.batch_id "
            "WHERE i.user_id=:user AND i.activity_id IN (:event,:visual) "
            "AND json_extract(i.data,'$.source_event_ids[0]')=:event "
            "AND m.perception_line IS NOT NULL ORDER BY i.occurred_at,i.seq",
            params,
        ).fetchall()
        decisions: list[RuleDecision] = []
        threshold = rows[0]["dwell_threshold"] if rows else SHORT_DWELL_SECONDS
        kept = (
            filter_perception(
                perception_activity(event),
                decisions,
                threshold=threshold,
                deduct_short=not rows or bool(rows[0]["deduct_short"]),
            )
            if rows or raw["filter_reason"] == "short_dwell"
            else None
        )
        parts = []
        rounds = {}
        for row in rows:
            round_id = str(row["batch_id"]) if row["batch_id"] is not None else None
            parts.append(
                {
                    "kind": "observation" if row["activity_id"] == event_id else "visual",
                    "text": row["perception_line"],
                    "tokens": row["perception_tokens"],
                    "delivered": bool(row["applied"]),
                    "round_id": round_id,
                }
            )
            if round_id is not None and round_id not in rounds:
                status = (
                    "ready"
                    if row["ai_text"] is not None
                    else (
                        "disabled"
                        if not ai_enabled
                        else "retrying"
                        if row["attempts"]
                        else "pending"
                    )
                )
                rounds[round_id] = {
                    "id": round_id,
                    "status": status,
                    "input": row["rule_text"],
                    "summary": row["ai_text"],
                    "timeline_entry": (
                        summary_ref(row, policy) if row["position_ms"] is not None else None
                    ),
                }
        size = db.execute(
            "SELECT tail_cost FROM perception_buffers WHERE conversation_id=:conversation",
            params,
        ).fetchone()
        return {
            "context_messages": message_refs(db, user, conversation, [event_id], policy).get(
                event_id, []
            ),
            "dwell_threshold_seconds": threshold,
            "filter_reason": raw["filter_reason"],
            "decisions": decisions,
            "retained": kept.content if kept is not None else None,
            "parts": parts,
            "rounds": list(rounds.values()),
            "ai_enabled": ai_enabled,
            "budget": policy.delivery_tokens,
            "pending_size": max(0, size[0]) if size else 0,
        }
