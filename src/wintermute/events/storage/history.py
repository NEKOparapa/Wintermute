"""Read-only projections of the event journals and the exact committed session transcript."""

import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from wintermute.events.core.activity import (
    ActivityItem,
    activity_message,
    perception_activity,
)
from wintermute.events.core.presentation import body_text, origin_text, timestamp_text
from wintermute.events.core.slices import SliceStreamItem
from wintermute.events.core.types import EventRecord, event_message

type Position = tuple[int, int, int]
_MAX = 2**63 - 1
_VIEWS = {"conversation", "notification", "perception", "slice", "chat", "timeline"}
# Event times are normalized to UTC at ingestion. The same expression in the index and queries
# lets SQLite seek directly to a page boundary, including for out-of-order device captures.
_EVENT_TIME = (
    "CAST(ROUND((julianday(json_extract(data,'$.occurred_at'))-2440587.5)*86400000) AS INTEGER)"
)


def upgrade_history_storage(db: sqlite3.Connection) -> None:
    for layer in ("conversation", "notification", "perception", "slice"):
        db.execute(
            f"CREATE INDEX IF NOT EXISTS {layer}_history_time ON {layer}_events"
            f"(user_id,conversation_id,{_EVENT_TIME},seq)"
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS slice_summary_history ON slice_stream"
        "(user_id,conversation_scope,CAST(ROUND(created_at*1000) AS INTEGER),seq)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS history_output_event ON outputs(event_id)")
    # Transcripts previously only stored an event_id. Retain their scope on this table so a
    # late ASR result pages by its own processing time without scanning every recording.
    columns = {row[1] for row in db.execute("PRAGMA table_info(audio_transcripts)")}
    if "conversation_id" not in columns:
        db.execute("ALTER TABLE audio_transcripts ADD COLUMN user_id TEXT")
        db.execute("ALTER TABLE audio_transcripts ADD COLUMN conversation_id TEXT")
        db.execute(
            "UPDATE audio_transcripts AS t SET (user_id,conversation_id)="
            "(SELECT user_id,conversation_id FROM slice_events e WHERE e.event_id=t.event_id)"
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS transcript_history ON audio_transcripts("
        "user_id,conversation_id,CAST(ROUND(created_at*1000) AS INTEGER))"
    )


@dataclass(frozen=True)
class HistoryItem:
    id: str
    role: str
    source: str
    occurred_at: str | None
    text: str
    media: tuple[EventRecord, ...] = ()


def browse(
    db: sqlite3.Connection, user: str, conversation: str, view: str, cursor: str | None, limit: int
) -> tuple[list[HistoryItem], str | None]:
    if view not in _VIEWS or not 1 <= limit <= 100:
        raise ValueError("Invalid history view or limit")
    position: Position = (_MAX, _MAX, _MAX)
    if cursor is not None:
        match = re.fullmatch(r"[a-z]+:(-?[0-9]{1,19}):([0-9]{1,19}):([0-9]{1,19})", cursor)
        if not match or not cursor.startswith(view + ":"):
            raise ValueError("Invalid history cursor")
        position = (int(match[1]), int(match[2]), int(match[3]))
        if any(abs(number) > _MAX for number in position):
            raise ValueError("Invalid history cursor")
    if view in {"chat", "timeline"}:
        records = _session(db, conversation, view, position)
    else:
        records = _journal(db, user, conversation, view, position, limit + 1)
    items: list[HistoryItem] = []
    for key, item in records:
        if len(items) == limit:
            return items, f"{view}:{position[0]}:{position[1]}:{position[2]}"
        items.append(item)
        position = key
    return items, None


def _media(
    db: sqlite3.Connection, user: str, conversation: str, ids: Sequence[str]
) -> tuple[EventRecord, ...]:
    if not ids:
        return ()
    rows = db.execute(
        "SELECT data FROM slice_events WHERE event_id IN (SELECT value FROM json_each(?)) "
        "AND user_id=? AND (conversation_id=? OR conversation_id IS NULL) "
        "AND json_extract(data,'$.type') IN ('slice.image','slice.audio') ORDER BY seq",
        (json.dumps(list(ids)), user, conversation),
    )
    return tuple(EventRecord.model_validate_json(row[0]) for row in rows)


def _session(
    db: sqlite3.Connection, conversation: str, view: str, before: Position
) -> Iterator[tuple[Position, HistoryItem]]:
    # One run stores an append-only JSON transcript. Seek within it before reading older runs.
    scope = "WHERE h.conversation_id=? AND h.seq<=? "
    if view == "timeline":
        scope += (
            "AND (h.activity_seq IS NOT NULL OR EXISTS (SELECT 1 FROM activity_deliveries d "
            "WHERE d.input_run_id=h.run_id AND d.input_index IS NOT NULL)) "
        )
    batches = db.execute(
        "SELECT h.seq,j.messages,i.data FROM session_history h "
        "LEFT JOIN dispatch_jobs j ON j.job_id=h.run_id "
        "LEFT JOIN activity_items i ON i.seq=h.activity_seq "
        f"{scope}ORDER BY h.seq DESC",
        (conversation, before[0]),
    )
    for seq, messages, activity in batches:
        if activity:
            if (seq, 0, 0) >= before:
                continue
            item = ActivityItem.model_validate_json(activity)
            entry = activity_message(item)
            yield (
                (seq, 0, 0),
                HistoryItem(
                    f"history-{seq}-0",
                    "user",
                    entry.origin or "活动时间线",
                    timestamp_text(item.occurred_at),
                    entry.text,
                ),
            )
            continue
        transcript = json.loads(messages)
        end = min(before[1], len(transcript)) if before[0] == seq else len(transcript)
        for index in range(end - 1, -1, -1):
            msg = transcript[index]
            timeline = msg.get("source") == "timeline" and msg["role"] == "user"
            if view == "timeline" and not timeline:
                continue
            yield (seq, index, 0), _message(f"history-{seq}-{index}", msg)


def _message(id_: str, msg: dict[str, Any]) -> HistoryItem:
    role = msg["role"]
    if role == "tool":
        return HistoryItem(id_, role, f"工具 · {msg['name']}", None, msg["content"])
    text = msg["text"]
    if role == "assistant":
        parts = [text] if text else []
        for call in msg.get("tool_calls", []):
            args = json.dumps(call["arguments"], ensure_ascii=False)
            parts.append(f"{call['name']}({args})")
        return HistoryItem(id_, role, "主 Agent", None, "\n".join(parts))
    timestamp = msg.get("timestamp")
    # Some timeline envelopes include the end of a range and validity notes after the ISO instant.
    when = re.split(r" 至 |（", timestamp, maxsplit=1)[0] if timestamp else None
    if when:
        try:
            when = timestamp_text(datetime.fromisoformat(when))
        except ValueError:
            when = None
    return HistoryItem(id_, role, msg.get("origin") or "用户原话", when, text)


def _journal(
    db: sqlite3.Connection, user: str, conversation: str, view: str, before: Position, count: int
) -> Iterator[tuple[Position, HistoryItem]]:
    # Timestamp of the triggering event orders outputs whose schema has no clock. Unique
    # sequence and part break timestamp ties, including multiple records from one slice.
    table = f"{view}_events"  # view was checked against _VIEWS.
    candidates: list[str] = []

    def take(time: str, seq: str, part: int, ref: str, tables: str, scope: str) -> None:
        # Sort/limit each scoped index scan before merging. A long global journal or a large
        # slice payload must not make SQLite materialize the full history on every scroll.
        candidates.append(
            f"SELECT * FROM (SELECT {time} t,{seq} n,{part} k,{ref} ref FROM {tables} "
            f"WHERE {scope} AND {time}<=:t AND ({time},{seq},{part})<(:t,:n,:k) "
            f"ORDER BY {time} DESC,{seq} DESC LIMIT :count)"
        )

    scopes = ["e.conversation_id=:c"]
    if view in {"slice", "perception"}:
        scopes.append("e.conversation_id IS NULL")
    for conversation_scope in scopes:
        scope = f"e.user_id=:u AND {conversation_scope}"
        time = _EVENT_TIME.replace("json_extract(data,", "json_extract(e.data,")
        take(time, "e.seq", 0, "e.event_id", f"{table} e", scope)
        if view in {"conversation", "notification"}:
            take(
                time,
                "e.seq",
                1,
                "o.output_id",
                f"{table} e CROSS JOIN outputs o ON o.event_id=e.event_id",
                scope + " AND o.user_id=:u AND o.conversation_id=:c",
            )
        if view == "slice":
            take(
                "CAST(ROUND(a.created_at*1000) AS INTEGER)",
                "a.rowid",
                3,
                "a.event_id",
                "audio_transcripts a",
                scope.replace("e.", "a."),
            )
    if view == "slice":
        for scope in ("s.conversation_scope=:c", "s.conversation_scope=''"):
            take(
                "CAST(ROUND(s.created_at*1000) AS INTEGER)",
                "s.seq",
                2,
                "s.item_id",
                "slice_stream s",
                "s.user_id=:u AND " + scope,
            )
    params = {
        "u": user,
        "c": conversation,
        "t": before[0],
        "n": before[1],
        "k": before[2],
        "count": count,
    }
    combined = " UNION ALL ".join(candidates)
    rows = db.execute(
        f"SELECT * FROM ({combined}) ORDER BY t DESC,n DESC,k DESC LIMIT :count", params
    )
    for moment, number, part, ref in rows:
        if part == 1:
            output = db.execute("SELECT text FROM outputs WHERE output_id=?", (ref,)).fetchone()
            entry = HistoryItem(ref, "assistant", "主 Agent", None, output[0])
        elif part == 2:
            data = db.execute("SELECT data FROM slice_stream WHERE item_id=?", (ref,)).fetchone()
            summary = SliceStreamItem.model_validate_json(data[0])
            entry = HistoryItem(
                ref,
                "event",
                origin_text("切片摘要", summary.terminal_id, summary.source_id),
                timestamp_text(summary.created_at),
                summary.text,
                _media(db, user, conversation, summary.source_event_ids),
            )
        else:
            data = db.execute(f"SELECT data FROM {table} WHERE event_id=?", (ref,)).fetchone()
            event = EventRecord.model_validate_json(data[0])
            if part == 3:
                transcript = db.execute(
                    "SELECT text,created_at FROM audio_transcripts WHERE event_id=?", (ref,)
                ).fetchone()
                entry = HistoryItem(
                    f"transcript-{ref}",
                    "event",
                    origin_text("录音转写", event.terminal_id, event.source_id),
                    timestamp_text(datetime.fromtimestamp(transcript[1], UTC)),
                    transcript[0],
                    (event,),
                )
            else:
                entry = _event(event)
        yield (moment, number, part), entry


def _event(event: EventRecord) -> HistoryItem:
    when = timestamp_text(event.occurred_at)
    if event.layer in {"conversation", "notification"}:
        msg = event_message(event)
        return HistoryItem(event.event_id, "user", msg.origin or event.terminal_id, when, msg.text)
    if event.layer == "perception":
        msg = activity_message(perception_activity(event))
        return HistoryItem(
            event.event_id,
            "event",
            origin_text("感知", event.terminal_id, event.source_id),
            when,
            msg.text,
        )
    source = origin_text("切片", event.terminal_id, event.source_id)
    if event.type == "slice.recorded":
        text = str(event.payload["text"])
        if event.payload.get("metadata"):
            text += "\n" + body_text(event.payload["metadata"])
        return HistoryItem(event.event_id, "event", source, when, text)
    capture = event.payload.get("capture")
    return HistoryItem(
        event.event_id, "event", source, when, body_text(capture) if capture else "", (event,)
    )
