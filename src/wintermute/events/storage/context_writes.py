"""Immutable entries for timeline messages actually committed to the main Agent's context."""

import json
import sqlite3
from datetime import datetime

from wintermute.agent import UserMessage
from wintermute.events.core.activity import REVISION_NOTE, ActivityItem, activity_message
from wintermute.events.core.presentation import timestamp_text
from wintermute.events.storage.merged_timeline import SPAN_COLUMNS, activity_span

# Undated legacy writes sort below every representable activity timestamp.
UNDATED = -(2**63) + 1


def save(
    db: sqlite3.Connection,
    room: str,
    history_seq: int,
    slot: int,
    written: datetime | None,
    message: UserMessage,
    included: list[int],
) -> None:
    spans = [
        activity_span(row)
        for row in db.execute(
            "SELECT CAST(ROUND(i.occurred_at*1000000) AS INTEGER) at,"
            + SPAN_COLUMNS
            + " FROM activity_items i WHERE i.seq IN (SELECT value FROM json_each(?))",
            (json.dumps(included),),
        )
    ]
    db.execute(
        "INSERT INTO merged_context_writes(room,history_seq,slot,at,original,covered_from,"
        "covered_until) VALUES(?,?,?,?,?,?,?)",
        (
            room,
            history_seq,
            slot,
            round(written.timestamp() * 1_000_000) if written else UNDATED,
            message.model_text(),
            timestamp_text(min(start for start, _ in spans)) if spans else None,
            timestamp_text(max(end for _, end in spans)) if spans else None,
        ),
    )


def _historical_sources(
    db: sqlite3.Connection, room: str, run: str, slot: int, text: str
) -> list[int]:
    # Receipts include filtered/truncated entries. Credit only blocks verified against
    # the immutable message. Late revisions can replay entries from an earlier receipt.
    sql = "SELECT e.seq,e.line,e.batch FROM merged_entries e WHERE e.room=?"
    params: tuple[object, ...] = (room,)
    if not text.startswith(REVISION_NOTE + "\n"):
        sql += (
            " AND e.seq IN (SELECT activity_seq FROM activity_deliveries "
            "WHERE input_run_id=? AND input_index=?)"
        )
        params = (room, run, slot)
    candidates: dict[str, list[int]] = {}
    batches = set()
    for seq, line, batch in db.execute(sql, params):
        candidates[line] = [] if line in candidates else [seq]
        if batch is not None:
            batches.add(batch)
    for batch, ai, synopsis in db.execute(
        "SELECT id,ai,synopsis FROM merged_batches WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(list(batches)),),
    ):
        ids = [r[0] for r in db.execute("SELECT seq FROM merged_entries WHERE batch=?", (batch,))]
        for summary in {ai, synopsis} - {None}:
            block = "合并摘要 · " + summary
            candidates[block] = [] if block in candidates else ids
    remaining = text.removeprefix(REVISION_NOTE + "\n").removeprefix("…\n")
    sources: list[int] = []
    while remaining:
        matches = [b for b in candidates if remaining == b or remaining.startswith(b + "\n")]
        if not matches and remaining.endswith("…") and len(remaining) > 1:
            matches = [b for b in candidates if b.startswith(remaining[:-1])]
            return (
                sources + candidates[matches[0]]
                if len(matches) == 1 and candidates[matches[0]]
                else []
            )
        if len(matches) != 1 or not candidates[matches[0]]:
            return []
        block = matches[0]
        sources.extend(candidates[block])
        remaining = remaining[len(block) :].removeprefix("\n")
    return sources


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE merged_context_writes(room TEXT NOT NULL,history_seq INTEGER NOT NULL "
        "REFERENCES session_history(seq),slot INTEGER NOT NULL,at INTEGER NOT NULL,"
        "original TEXT NOT NULL,covered_from TEXT,covered_until TEXT,PRIMARY KEY(history_seq,slot))"
    )
    db.execute("CREATE INDEX merged_write_order ON merged_context_writes(room,at,history_seq,slot)")
    for row in db.execute(
        "SELECT h.conversation_id,h.seq,h.run_id,m.key,m.value,t.written_at FROM session_history h "
        "JOIN dispatch_jobs j ON j.job_id=h.run_id JOIN json_each(j.messages) m "
        "LEFT JOIN timeline_input_times t ON t.run_id=h.run_id AND t.input_index=m.key "
        "WHERE json_extract(m.value,'$.role')='user' "
        "AND json_extract(m.value,'$.source')='timeline'"
    ):
        room, seq, run, slot, raw, stamp = row
        msg = json.loads(raw)
        message = UserMessage(msg["text"], "timeline", msg["timestamp"], msg["origin"])
        included = _historical_sources(db, room, run, slot, message.text)
        save(
            db, room, seq, slot, datetime.fromisoformat(stamp) if stamp else None, message, included
        )
    for room, seq, activity, data in db.execute(
        "SELECT h.conversation_id,h.seq,i.seq,i.data FROM session_history h "
        "JOIN activity_items i ON i.seq=h.activity_seq"
    ):
        save(
            db,
            room,
            seq,
            0,
            None,
            activity_message(ActivityItem.model_validate_json(data)),
            [activity],
        )
