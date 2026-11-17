"""Navigate recorded perception inputs and the immutable session messages that accepted them."""

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from wintermute.agent import UserMessage
from wintermute.events.core.activity import ActivityItem, activity_message
from wintermute.events.core.presentation import timestamp_text
from wintermute.events.storage.read_snapshot import read_snapshot


class MessageRef(TypedDict):
    id: str
    written_at: str | None
    activity_at: str | None
    parts: list[Literal["observation", "visual"]]


class SummaryRef(TypedDict):
    id: str
    occurred_at: str
    generated_at: str | None


def summary_ref(batch: int, position_ms: int, generated_at: str | None) -> SummaryRef:
    return {
        "id": f"round-{batch}",
        "occurred_at": generated_at
        or timestamp_text(datetime.fromtimestamp(position_ms / 1000, UTC)),
        "generated_at": generated_at,
    }


def message_refs(
    db: sqlite3.Connection, user: str, conversation: str, event_ids: list[str]
) -> dict[str, list[MessageRef]]:
    """An applied but filtered row shares the receipt, not the message."""
    by_event: dict[str, dict[str, MessageRef]] = {}
    legacy_times: dict[str, str | None] = {}
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
        if match[1]:
            bounds = db.execute(
                "SELECT r.after_seq,r.through_seq,b.rule_text,b.ai_text FROM perception_rounds r "
                "JOIN timeline_batches b USING(batch_id) "
                "WHERE r.batch_id=? AND r.conversation_id=? AND b.user_id=?",
                (int(match[1]), conversation, user),
            ).fetchone()
            if bounds is None:
                raise LookupError("Unknown perception round")
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
            "i.occurred_at,json_extract(i.data,'$.terminal_id'),"
            "COALESCE(json_extract(i.data,'$.content.value.foreground_app'),"
            "json_extract(i.data,'$.content.state_key'),json_extract(i.data,'$.event_type')), "
            "json_extract(i.data,'$.content.value.started_at') "
            "FROM activity_deliveries d JOIN activity_items i ON i.seq=d.activity_seq "
            "JOIN timeline_batch_members m ON m.activity_seq=i.seq "
            "WHERE d.conversation_id=? AND i.user_id=? AND m.perception_line IS NOT '' AND "
            + where
            + " AND d.activity_seq>? ORDER BY d.activity_seq LIMIT ?",
            (conversation, user, *params, after, limit + 1),
        ).fetchall()
        return {
            "text": text,
            "written_at": written,
            **(
                {"round": {"input": bounds["rule_text"], "summary": bounds["ai_text"]}}
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
                    "terminal": terminal,
                    "occurred_at": start
                    if isinstance(start, str)
                    else timestamp_text(datetime.fromtimestamp(at, UTC)),
                }
                for _, activity, is_slice, event, at, terminal, title, start in rows[:limit]
            ],
            "next_cursor": f"{target}:{rows[limit - 1][0]}" if len(rows) > limit else None,
        }
