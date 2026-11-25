"""Navigate recorded perception inputs and the immutable session messages that accepted them."""

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NotRequired, TypedDict

from purin.agent import UserMessage
from purin.events.core.activity import ActivityItem, activity_message
from purin.events.core.perception_tokens import count_tokens
from purin.events.core.presentation import timestamp_text
from purin.events.core.timeline_compression import TimelinePolicy
from purin.events.storage.perception_buffer import output_budget, source_totals
from purin.events.storage.read_snapshot import read_snapshot


class TimelineUsage(TypedDict):
    tokens: int
    tokenizer: Literal["deepseek-v4"]
    budget: int
    devices: list[str]
    duration_seconds: float | None


@dataclass
class Projection:
    threshold: int | None = None
    deduct_short: bool = False
    pending: bool = False
    tokens: int = 0


def projections(
    db: sqlite3.Connection, user: str, conversation: str, ids: list[str]
) -> dict[str, Projection]:
    result: dict[str, Projection] = {}
    for event, threshold, deduct_short, pending, tokens in db.execute(
        "SELECT json_extract(i.data,'$.source_event_ids[0]'),m.dwell_threshold,m.deduct_short,"
        "(d.applied=0 AND m.perception_line!=''),m.perception_tokens "
        "FROM json_each(?) ids CROSS JOIN activity_items i ON i.activity_id=ids.value "
        "CROSS JOIN activity_deliveries d ON d.activity_seq=i.seq AND d.conversation_id=? "
        "JOIN timeline_batch_members m ON m.activity_seq=i.seq "
        "WHERE i.user_id=? AND m.perception_line IS NOT NULL",
        (json.dumps(ids + [id_ + ":visual" for id_ in ids]), conversation, user),
    ):
        part = result.setdefault(event, Projection())
        part.threshold = threshold
        part.deduct_short = bool(deduct_short)
        part.pending |= bool(pending)
        part.tokens += tokens
    return result


def visit_usage(tokens: int, budget: int, device_id: str, seconds: object) -> TimelineUsage:
    return TimelineUsage(
        tokens=tokens,
        tokenizer="deepseek-v4",
        budget=budget,
        devices=[device_id],
        duration_seconds=(
            seconds
            if isinstance(seconds, (float, int)) and not isinstance(seconds, bool) and seconds >= 0
            else None
        ),
    )


class MessageRef(TypedDict):
    id: str
    written_at: str | None
    activity_at: str | None
    parts: list[Literal["observation", "visual"]]
    usage: NotRequired[TimelineUsage]


class SummaryRef(TypedDict):
    id: str
    occurred_at: str
    generated_at: str | None
    usage: TimelineUsage


def summary_ref(row: sqlite3.Row, policy: TimelinePolicy) -> SummaryRef:
    return {
        "id": f"round-{row['batch_id']}",
        "occurred_at": row["generated_at"]
        or timestamp_text(datetime.fromtimestamp(row["position_ms"] / 1000, UTC)),
        "generated_at": row["generated_at"],
        "usage": TimelineUsage(
            tokens=count_tokens(row["ai_text"]),
            tokenizer="deepseek-v4",
            budget=output_budget(policy),
            devices=json.loads(row["devices"]),
            duration_seconds=row["duration_seconds"],
        ),
    }


def message_refs(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    event_ids: list[str],
    policy: TimelinePolicy,
) -> dict[str, list[MessageRef]]:
    """An applied but filtered row shares the receipt, not the message."""
    by_event: dict[str, dict[str, MessageRef]] = {}
    legacy_times: dict[str, str | None] = {}
    usage: dict[str, TimelineUsage | None] = {}
    ids = event_ids + [event + ":visual" for event in event_ids]
    rows = db.execute(
        "SELECT json_extract(i.data,'$.source_event_ids[0]'),i.activity_id,COALESCE(h.seq,l.seq),"
        "COALESCE(d.input_index,0),t.written_at,h.run_id,i.occurred_at FROM json_each(?) ids "
        "CROSS JOIN activity_items i ON i.activity_id=ids.value "
        "CROSS JOIN activity_deliveries d ON d.activity_seq=i.seq AND d.conversation_id=? "
        "JOIN timeline_batch_members m ON m.activity_seq=i.seq AND m.perception_line!='' "
        "LEFT JOIN session_history h ON h.conversation_id=d.conversation_id AND "
        "h.run_id=d.input_run_id AND d.input_index IS NOT NULL "
        "LEFT JOIN session_history l ON l.conversation_id=d.conversation_id "
        "AND l.activity_seq=i.seq "
        "LEFT JOIN timeline_input_times t ON t.run_id=h.run_id AND t.input_index=d.input_index "
        "WHERE i.user_id=? AND (h.seq IS NOT NULL OR l.seq IS NOT NULL) ORDER BY 3,4,i.seq",
        (json.dumps(ids), conversation, user),
    )
    for event, activity, seq, index, written, run, occurred in rows:
        key = f"history-{seq}-{index}"
        if written is None and key not in legacy_times:
            if run is None:
                legacy_times[key] = timestamp_text(datetime.fromtimestamp(occurred, UTC))
            else:
                stamp = db.execute(
                    "SELECT json_extract(messages,?) FROM dispatch_jobs "
                    "WHERE job_id=? AND user_id=? AND conversation_id=?",
                    (f"$[{index}].timestamp", run, user, conversation),
                ).fetchone()
                legacy_times[key] = (
                    re.split(r" 至 |（", stamp[0], maxsplit=1)[0] if stamp and stamp[0] else None
                )
        ref = by_event.setdefault(event, {}).setdefault(
            key,
            {"id": key, "written_at": written, "activity_at": legacy_times.get(key), "parts": []},
        )
        ref["parts"].append("observation" if activity == event else "visual")
        if key not in usage:
            try:
                original, _, where, params = _slot(db, seq, index, user, conversation)
            except LookupError:
                usage[key] = None
            else:
                devices, seconds = source_totals(db, conversation, user, where, params)
                usage[key] = TimelineUsage(
                    tokens=count_tokens(original),
                    tokenizer="deepseek-v4",
                    budget=policy.delivery_tokens,
                    devices=devices,
                    duration_seconds=seconds,
                )
        if (info := usage[key]) is not None:
            ref["usage"] = info
    return {event: list(refs.values()) for event, refs in by_event.items()}


def _slot(
    db: sqlite3.Connection, seq: int, index: int, user: str, conversation: str
) -> tuple[str, str | None, str, tuple[object, ...]]:
    row = db.execute(
        "SELECT h.run_id,h.activity_seq,i.data,json_extract(j.messages,?),t.written_at "
        "FROM session_history h LEFT JOIN dispatch_jobs j ON j.job_id=h.run_id AND j.user_id=? "
        "LEFT JOIN activity_items i ON i.seq=h.activity_seq AND i.user_id=? "
        "LEFT JOIN timeline_input_times t ON t.run_id=h.run_id AND t.input_index=? "
        "WHERE h.seq=? AND h.conversation_id=?",
        (f"$[{index}]", user, user, index, seq, conversation),
    ).fetchone()
    if row and row[2] and index == 0:
        msg = activity_message(ActivityItem.model_validate_json(row[2]))
        return msg.model_text(), None, "i.seq=?", (row[1],)
    if row and row[3]:
        saved = json.loads(row[3])
        if saved.get("role") == "user" and saved.get("source") == "timeline":
            msg = UserMessage(saved["text"], "timeline", saved["timestamp"], saved["origin"])
            return msg.model_text(), row[4], "d.input_run_id=? AND d.input_index=?", (row[0], index)
    raise LookupError("Unknown timeline message")


def members(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    target: str,
    cursor: str | None,
    limit: int,
    policy: TimelinePolicy,
) -> dict[str, Any]:
    match = re.fullmatch(
        r"(?:round-([1-9][0-9]{0,18})|history-([1-9][0-9]{0,18})-([0-9]{1,10}))", target
    )
    if not match or not 1 <= limit <= 100:
        raise ValueError("Invalid trace target or limit")
    after = 0
    if cursor is not None:
        prefix = target + ":"
        if not cursor.startswith(prefix) or not re.fullmatch(
            r"[1-9][0-9]{0,18}", cursor[len(prefix) :]
        ):
            raise ValueError("Invalid trace cursor")
        after = int(cursor[len(prefix) :])
    if max(after, int(match[1] or match[2])) > 2**63 - 1:
        raise ValueError("Invalid trace position")
    with read_snapshot(db):
        text = written = None
        previous = None
        if match[1]:
            bounds = db.execute(
                "SELECT r.after_seq,r.through_seq,b.rule_text,"
                "COALESCE(r.original_summary,b.ai_text) AS ai_text,"
                "r.original_summary IS NULL AS legacy,r.previous_batch_id "
                "FROM perception_rounds r "
                "JOIN timeline_batches b USING(batch_id) "
                "WHERE r.batch_id=? AND r.conversation_id=? AND b.user_id=?",
                (int(match[1]), conversation, user),
            ).fetchone()
            if bounds is None:
                raise LookupError("Unknown perception round")
            if bounds["previous_batch_id"] is not None:
                prior = db.execute(
                    "SELECT s.*,COALESCE(r.original_summary,b.ai_text) AS ai_text,"
                    "r.devices,r.duration_seconds "
                    "FROM perception_summary_history s JOIN timeline_batches b USING(batch_id) "
                    "JOIN perception_rounds r USING(batch_id) "
                    "WHERE s.batch_id=? AND s.conversation_id=? AND b.user_id=?",
                    (bounds["previous_batch_id"], conversation, user),
                ).fetchone()
                if prior is not None:
                    previous = summary_ref(prior, policy)
            where, params = (
                "d.activity_seq>? AND d.activity_seq<=? AND m.is_slice=0",
                tuple(bounds[:2]),
            )
        else:
            text, written, where, params = _slot(
                db, int(match[2]), int(match[3]), user, conversation
            )
        rows = db.execute(
            "SELECT i.seq,i.activity_id,m.is_slice,json_extract(i.data,'$.source_event_ids[0]'),"
            "i.occurred_at,json_extract(i.data,'$.device_id'),"
            "COALESCE(json_extract(i.data,'$.content.value.foreground_app'),"
            "json_extract(i.data,'$.content.state_key'),json_extract(i.data,'$.event_type')), "
            "json_extract(i.data,'$.content.value.started_at'),"
            "CASE WHEN json_type(e.data,'$.payload.value.duration_seconds') IN ('integer','real') "
            "THEN json_extract(e.data,'$.payload.value.duration_seconds') END "
            "FROM activity_deliveries d JOIN activity_items i ON i.seq=d.activity_seq "
            "JOIN timeline_batch_members m ON m.activity_seq=i.seq "
            "LEFT JOIN perception_events e ON m.is_slice=0 AND "
            "e.event_id=json_extract(i.data,'$.source_event_ids[0]') "
            "WHERE d.conversation_id=? AND i.user_id=? AND m.perception_line IS NOT '' AND "
            + where
            + " AND d.activity_seq>? ORDER BY d.activity_seq LIMIT ?",
            (conversation, user, *params, after, limit + 1),
        ).fetchall()
        costs = projections(db, user, conversation, list({r[3] for r in rows[:limit] if not r[2]}))
        return {
            "text": text,
            "written_at": written,
            **(
                {
                    "round": {
                        "input": bounds["rule_text"],
                        "summary": bounds["ai_text"],
                        **(
                            {"archived_text_only": True}
                            if bounds["legacy"] and (bounds["ai_text"] or "").endswith("…")
                            else {}
                        ),
                        "previous_summary": previous,
                    }
                }
                if match[1]
                else {}
            ),
            "items": [
                {
                    "id": activity,
                    "event_id": None if is_slice else event,
                    "title": "切片"
                    if is_slice
                    else "截图识别补充"
                    if title == "desktop.visual"
                    else title,
                    "device_id": device_id,
                    **(
                        {
                            "usage": visit_usage(
                                costs[event].tokens, policy.delivery_tokens, device_id, seconds
                            )
                        }
                        if event in costs
                        else {}
                    ),
                    "occurred_at": start
                    if isinstance(start, str)
                    else timestamp_text(datetime.fromtimestamp(at, UTC)),
                }
                for _, activity, is_slice, event, at, device_id, title, start, seconds in rows[
                    :limit
                ]
            ],
            "next_cursor": f"{target}:{rows[limit - 1][0]}" if len(rows) > limit else None,
        }
