"""Read-only projections of the event journals and the exact committed session transcript."""

import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, TypedDict

from purin.events.core.activity import (
    ActivityItem,
    activity_message,
    perception_activity,
)
from purin.events.core.perception_rules import RuleDecision, filter_perception
from purin.events.core.presentation import body_text, origin_text, timestamp_text
from purin.events.core.slices import SliceStreamItem
from purin.events.core.timeline_compression import TimelinePolicy
from purin.events.core.types import EventRecord, event_message
from purin.events.storage import context_writes, merged_timeline
from purin.events.storage.perception_trace import (
    MessageRef,
    Projection,
    SummaryRef,
    TimelineUsage,
    message_refs,
    projections,
    summary_ref,
    visit_usage,
)

type Position = tuple[int, int, int]
_MAX = 2**63 - 1
_VIEWS = {
    "conversation",
    "notification",
    "perception",
    "perceptiontimeline",
    "merged",
    "slice",
    "chat",
    "dialogue",
}
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


class ScreenshotGroupInfo(TypedDict):
    id: str
    app: str
    subject: str
    started_at: str
    ended_at: str
    frame_count: int
    change_count: int


class ActivityRef(TypedDict):
    id: str
    layer: str
    device_id: str
    occurred_at: str


class MergedSummary(TypedDict):
    kind: str
    generated_at: str | None
    covered_from: str
    covered_until: str


class ContextWrite(TypedDict):
    covered_from: str | None
    covered_until: str | None


@dataclass(frozen=True)
class HistoryItem:
    id: str
    role: str
    source: str
    occurred_at: str | None
    text: str
    media: tuple[EventRecord, ...] = ()
    group: ScreenshotGroupInfo | None = None
    layer: str | None = None
    device_id: str | None = None
    message_id: str | None = None
    raw: EventRecord | None = None
    filter_result: str | None = None
    context_messages: tuple[MessageRef, ...] = ()
    pending_context: bool = False
    summary_round: SummaryRef | None = None
    usage: TimelineUsage | None = None
    members: tuple[ActivityRef, ...] = ()
    material_type: str | None = None
    processing: str | None = None
    merged_summary: MergedSummary | None = None
    context_write: ContextWrite | None = None


def upgrade_dialogue_storage(db: sqlite3.Connection) -> None:
    # Only index references. Text, sender and notification origin still belong to their journals.
    db.execute(
        "CREATE TABLE IF NOT EXISTS dialogue_entries (seq INTEGER PRIMARY KEY,"
        "user_id TEXT NOT NULL,conversation_id TEXT NOT NULL,"
        "event_id TEXT UNIQUE REFERENCES conversation_events(event_id),"
        "output_id TEXT UNIQUE REFERENCES outputs(output_id),completed_at TEXT,"
        "CHECK((event_id IS NULL) != (output_id IS NULL)))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS dialogue_order ON dialogue_entries(user_id,conversation_id,seq)"
    )
    # Legacy outputs have no clock. Place them after the last input absorbed by their run.
    db.execute(
        "INSERT OR IGNORE INTO dialogue_entries(user_id,conversation_id,event_id,output_id) "
        "SELECT user_id,conversation_id,input,output FROM ("
        "SELECT user_id,conversation_id,event_id input,NULL output,seq position,0 part "
        "FROM dispatch_jobs WHERE layer='conversation' UNION ALL "
        "SELECT o.user_id,o.conversation_id,NULL,o.output_id,"
        "COALESCE((SELECT MAX(j.seq) FROM dispatch_jobs j WHERE j.run_id=o.job_id),r.seq),o.seq "
        "FROM outputs o JOIN dispatch_jobs r ON r.job_id=o.job_id WHERE o.text!=''"
        ") ORDER BY position,part"
    )


def _dialogue(
    db: sqlite3.Connection, user: str, conversation: str, before: Position, count: int
) -> Iterator[tuple[Position, HistoryItem]]:
    rows = db.execute(
        "SELECT d.seq,e.data,o.output_id,o.text,o.layer,d.completed_at FROM dialogue_entries d "
        "LEFT JOIN conversation_events e ON e.event_id=d.event_id "
        "LEFT JOIN outputs o ON o.output_id=d.output_id "
        "WHERE d.user_id=? AND d.conversation_id=? AND d.seq<? ORDER BY d.seq DESC LIMIT ?",
        (user, conversation, before[0], count),
    )
    for seq, data, output_id, text, layer, completed_at in rows:
        if data is None:
            item = HistoryItem(output_id, "assistant", "主 Agent", completed_at, text, layer=layer)
        else:
            event = EventRecord.model_validate_json(data)
            item = replace(
                _event(event),
                layer=event.layer,
                device_id=event.device_id,
                message_id=event.producer_event_id,
            )
        yield (seq, 0, 0), item


def browse(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    view: str,
    cursor: str | None,
    limit: int,
    policy: TimelinePolicy,
    *,
    grouped: bool = False,
) -> tuple[list[HistoryItem], str | None]:
    if view not in _VIEWS or not 1 <= limit <= 100 or (grouped and view != "slice"):
        raise ValueError("Invalid history view or limit")
    cursor_view = "slicegroups" if grouped else view
    position: Position = (_MAX, _MAX, _MAX)
    if cursor is not None:
        match = re.fullmatch(r"[a-z]+:(-?[0-9]{1,19}):([0-9]{1,19}):([0-9]{1,19})", cursor)
        if not match or not cursor.startswith(cursor_view + ":"):
            raise ValueError("Invalid history cursor")
        position = (int(match[1]), int(match[2]), int(match[3]))
        if any(abs(number) > _MAX for number in position):
            raise ValueError("Invalid history cursor")
    if view == "dialogue":
        records = _dialogue(db, user, conversation, position, limit + 1)
    elif view == "merged":
        records = _merged(db, conversation, position, limit + 1)
    elif view == "chat":
        records = _session(db, conversation, position)
    else:
        records = _journal(db, user, conversation, view, position, limit + 1, grouped, policy)
    items: list[HistoryItem] = []
    for key, item in records:
        if len(items) == limit:
            return items, f"{cursor_view}:{position[0]}:{position[1]}:{position[2]}"
        items.append(item)
        position = key
    return items, None


def _merged(
    db: sqlite3.Connection, room: str, before: Position, count: int
) -> Iterator[tuple[Position, HistoryItem]]:
    for row in merged_timeline.history_rows(db, room, before, count):
        at = merged_timeline.stamp(row["at"]) if row["at"] != context_writes.UNDATED else None
        if row["part"] >= 2:
            entry = HistoryItem(
                f"history-{row['seq']}-{row['part'] - 2}",
                "context_write",
                "写入上下文",
                at,
                row["text"],
                context_write=ContextWrite(
                    covered_from=row["covered_from"],
                    covered_until=row["covered_until"],
                ),
            )
        elif row["batch"] is None:
            source = "感知层" if row["layer"] == "perception" else "切片层"
            entry = HistoryItem(
                row["activity_id"],
                "event",
                source + " · " + row["device_id"],
                at,
                row["text"],
                layer=row["layer"],
            )
        else:
            members = []
            starts, ends = [], []
            for m in db.execute(
                "SELECT e.at,e.layer,i.activity_id,json_extract(i.data,'$.device_id') device_id,"
                + merged_timeline.SPAN_COLUMNS
                + " FROM merged_entries e JOIN activity_items i ON i.seq=e.seq "
                "WHERE e.room=? AND e.batch=? ORDER BY e.at,e.seq",
                (room, row["batch"]),
            ):
                event_time = datetime.fromtimestamp(m["at"] / 1_000_000, UTC)
                start, end = merged_timeline.activity_span(m)
                starts.append(start)
                ends.append(end)
                members.append(
                    ActivityRef(
                        id=m["activity_id"],
                        layer=m["layer"],
                        device_id=m["device_id"],
                        occurred_at=timestamp_text(event_time),
                    )
                )
            entry = HistoryItem(
                f"merged-{row['batch']}",
                "summary",
                "合并摘要",
                at,
                row["text"],
                members=tuple(members),
                merged_summary=MergedSummary(
                    kind="ai" if row["is_ai"] else "rules",
                    generated_at=at if row["generated_at"] is not None else None,
                    covered_from=timestamp_text(min(starts)),
                    covered_until=timestamp_text(max(ends)),
                ),
            )
        yield (row["at"], row["seq"], row["part"]), entry


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
    db: sqlite3.Connection, conversation: str, before: Position
) -> Iterator[tuple[Position, HistoryItem]]:
    # One run stores an append-only JSON transcript. Seek within it before reading older runs.
    batches = db.execute(
        "SELECT h.seq,j.messages,i.data,d.completed_at FROM session_history h "
        "LEFT JOIN dispatch_jobs j ON j.job_id=h.run_id "
        "LEFT JOIN activity_items i ON i.seq=h.activity_seq "
        "LEFT JOIN outputs o ON o.job_id=h.run_id AND o.is_final=1 "
        "LEFT JOIN dialogue_entries d ON d.output_id=o.output_id "
        "WHERE h.conversation_id=? AND h.seq<=? ORDER BY h.seq DESC",
        (conversation, before[0]),
    )
    for seq, messages, activity, completed_at in batches:
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
            row = _message(f"history-{seq}-{index}", msg)
            if completed_at and index == len(transcript) - 1 and row.role == "assistant":
                row = replace(row, occurred_at=completed_at)
            yield (seq, index, 0), row


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
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    view: str,
    before: Position,
    count: int,
    grouped: bool,
    policy: TimelinePolicy,
) -> Iterator[tuple[Position, HistoryItem]]:
    # Sequence and part break timestamp ties, including multiple records from one slice.
    include_summaries = view == "perceptiontimeline"
    if include_summaries:
        view = "perception"
    table = f"{view}_events"  # view was checked against _VIEWS.
    candidates: list[str] = []
    group_time = "CAST(ROUND(s.started_at*1000) AS INTEGER)"
    # Once enough groups fill a page, older raw frames and superseded descriptions
    # cannot enter it. Seek to this lower bound rather than scanning days of hidden rows.
    oldest = -_MAX
    if grouped:
        for visibility in (conversation, ""):
            boundary = db.execute(
                f"SELECT {group_time} FROM screenshot_groups s WHERE s.user_id=? "
                f"AND s.conversation_scope=? AND {group_time}<=? "
                f"AND ({group_time},s.rowid,4)<(?,?,?) "
                f"ORDER BY {group_time} DESC,s.rowid DESC LIMIT 1 OFFSET ?",
                (user, visibility, before[0], *before, count - 1),
            ).fetchone()
            if boundary:
                oldest = max(oldest, boundary[0])

    def take(time: str, seq: str, part: int, ref: str, tables: str, scope: str) -> None:
        # Sort/limit each scoped index scan before merging. A long global journal or a large
        # slice payload must not make SQLite materialize the full history on every scroll.
        candidates.append(
            f"SELECT * FROM (SELECT {time} t,{seq} n,{part} k,{ref} ref FROM {tables} "
            f"WHERE {scope} AND {time} BETWEEN :oldest AND :t AND ({time},{seq},{part})<(:t,:n,:k) "
            f"ORDER BY {time} DESC,{seq} DESC LIMIT :count)"
        )

    if include_summaries:
        take(
            "s.position_ms",
            "s.batch_id",
            5,
            "s.batch_id",
            "perception_summary_history s CROSS JOIN timeline_batches b USING(batch_id)",
            "s.conversation_id=:c AND b.user_id=:u",
        )
    scopes = ["e.conversation_id=:c"]
    if view in {"slice", "perception"}:
        scopes.append("e.conversation_id IS NULL")
    for conversation_scope in scopes:
        scope = f"e.user_id=:u AND {conversation_scope}"
        time = _EVENT_TIME.replace("json_extract(data,", "json_extract(e.data,")
        raw_scope = scope
        if grouped:
            raw_scope += (
                " AND NOT EXISTS (SELECT 1 FROM screenshot_frames f WHERE f.event_id=e.event_id)"
            )
        take(time, "e.seq", 0, "e.event_id", f"{table} e", raw_scope)
        if grouped:
            take(
                "CAST(ROUND(a.created_at*1000) AS INTEGER)",
                "a.rowid",
                3,
                "a.event_id",
                "audio_transcripts a",
                scope.replace("e.", "a."),
            )
    if grouped:
        for scope in ("s.conversation_scope=:c", "s.conversation_scope=''"):
            source_scope = "s.user_id=:u AND " + scope
            take(group_time, "s.rowid", 4, "s.group_id", "screenshot_groups s", source_scope)
            take(
                "CAST(ROUND(s.created_at*1000) AS INTEGER)",
                "s.seq",
                2,
                "s.item_id",
                "slice_stream s",
                source_scope + " AND NOT (json_array_length(s.data,'$.source_event_ids')=1 "
                "AND EXISTS(SELECT 1 FROM screenshot_frames f WHERE f.event_id="
                "json_extract(s.data,'$.source_event_ids[0]')))",
            )
    params = {
        "oldest": oldest,
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
    projected: dict[str, Projection] = {}
    contexts: dict[str, list[MessageRef]] = {}
    page = rows.fetchall()
    if view == "perception" and (ids := [row[3] for row in page if row[2] == 0]):
        contexts = message_refs(db, user, conversation, ids, policy)
        projected = projections(db, user, conversation, ids)
    for moment, number, part, ref in page:
        if part == 5:
            summary_row = db.execute(
                "SELECT s.*,COALESCE(r.original_summary,b.ai_text) AS ai_text,"
                "r.devices,r.duration_seconds "
                "FROM perception_summary_history s "
                "JOIN timeline_batches b USING(batch_id) JOIN perception_rounds r USING(batch_id) "
                "WHERE s.batch_id=?",
                (ref,),
            ).fetchone()
            link = summary_ref(summary_row, policy)
            entry = HistoryItem(
                link["id"],
                "event",
                "AI 摘要",
                link["occurred_at"],
                summary_row["ai_text"],
                summary_round=link,
            )
        elif part == 4:
            row = db.execute("SELECT * FROM screenshot_groups WHERE group_id=?", (ref,)).fetchone()
            info = ScreenshotGroupInfo(
                id=ref,
                app=row["app"],
                subject=row["subject"],
                started_at=timestamp_text(datetime.fromtimestamp(row["started_at"], UTC)),
                ended_at=timestamp_text(datetime.fromtimestamp(row["last_at"], UTC)),
                frame_count=row["frame_count"],
                change_count=row["change_count"],
            )
            entry = HistoryItem(
                ref,
                "event",
                origin_text("定时截图", row["device_id"], row["source_id"]),
                info["started_at"],
                row["latest_fact"],
                group=info,
            )
        elif part == 2:
            data = db.execute("SELECT data FROM slice_stream WHERE item_id=?", (ref,)).fetchone()
            summary = SliceStreamItem.model_validate_json(data[0])
            entry = HistoryItem(
                ref,
                "event",
                origin_text("切片摘要", summary.device_id, summary.source_id),
                timestamp_text(summary.created_at),
                summary.text,
                _media(db, user, conversation, summary.source_event_ids),
            )
        else:
            fields = "data,filter_reason" if view == "perception" else "data"
            data = db.execute(f"SELECT {fields} FROM {table} WHERE event_id=?", (ref,)).fetchone()
            event = EventRecord.model_validate_json(data[0])
            if part == 3:
                transcript = db.execute(
                    "SELECT text,created_at FROM audio_transcripts WHERE event_id=?", (ref,)
                ).fetchone()
                entry = HistoryItem(
                    f"transcript-{ref}",
                    "event",
                    origin_text("录音转写", event.device_id, event.source_id),
                    timestamp_text(datetime.fromtimestamp(transcript[1], UTC)),
                    transcript[0],
                    (event,),
                )
            else:
                part_info = projected.get(ref, Projection())
                entry = _event(
                    event, threshold=part_info.threshold, deduct_short=part_info.deduct_short
                )
                if view == "slice" and not grouped:
                    detail = db.execute(
                        "SELECT t.text, json_extract(s.data,'$.text'),p.item_id,p.frame_done,"
                        "p.retry_at "
                        "FROM slice_processing p LEFT JOIN audio_transcripts t ON "
                        "t.event_id=p.event_id "
                        "LEFT JOIN slice_stream s ON s.item_id=p.item_id WHERE p.event_id=?",
                        (ref,),
                    ).fetchone()
                    content = entry.text
                    if detail:
                        if event.type == "slice.audio" and detail[0] is not None:
                            content = detail[0] or "未识别到语音"
                        elif event.type == "slice.image":
                            content = detail[1] or ("画面无变化" if detail[3] else "")
                    entry = replace(
                        entry,
                        text=content,
                        material_type=event.type,
                        layer="slice",
                        processing=(
                            "ready"
                            if detail and (detail[2] or detail[3])
                            else "retrying"
                            if detail and detail[4]
                            else "pending"
                        ),
                    )
                if view == "perception":
                    value = event.payload.get("value")
                    entry = replace(
                        entry,
                        filter_result="filtered" if data["filter_reason"] else entry.filter_result,
                        context_messages=tuple(contexts.get(ref, [])),
                        pending_context=part_info.pending,
                        usage=visit_usage(
                            part_info.tokens,
                            policy.delivery_tokens,
                            event.device_id,
                            value.get("duration_seconds") if isinstance(value, dict) else None,
                        ),
                    )
        yield (moment, number, part), entry


def screenshot_frames(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    group_id: str,
    cursor: str | None,
    limit: int,
) -> tuple[list[HistoryItem], str | None]:
    if not 1 <= limit <= 100:
        raise ValueError("Invalid page size")
    if (
        db.execute(
            "SELECT 1 FROM screenshot_groups WHERE group_id=? AND user_id=? "
            "AND conversation_scope IN (?, '')",
            (group_id, user, conversation),
        ).fetchone()
        is None
    ):
        raise LookupError("Unknown screenshot group")
    before: tuple[float, int] = (float("inf"), _MAX)
    if cursor is not None:
        match = re.fullmatch(r"frame:([1-9][0-9]{0,18})", cursor)
        if not match or int(match[1]) > _MAX:
            raise ValueError("Invalid frame cursor")
        last = db.execute(
            "SELECT occurred_at,rowid FROM screenshot_frames WHERE group_id=? AND rowid=?",
            (group_id, int(match[1])),
        ).fetchone()
        if last is None:
            raise ValueError("Unknown frame cursor")
        before = (last[0], last[1])
    rows = db.execute(
        "SELECT f.rowid,f.fact,e.data FROM screenshot_frames f JOIN slice_events e USING(event_id) "
        "WHERE f.group_id=? AND (f.occurred_at,f.rowid)<(?,?) "
        "AND e.user_id=? AND (e.conversation_id=? OR e.conversation_id IS NULL) "
        "ORDER BY f.occurred_at DESC,f.rowid DESC LIMIT ?",
        (group_id, *before, user, conversation, limit + 1),
    ).fetchall()
    items = []
    for _, fact, data in rows[:limit]:
        event = EventRecord.model_validate_json(data)
        items.append(
            HistoryItem(
                event.event_id,
                "event",
                origin_text("定时截图", event.device_id, event.source_id),
                timestamp_text(event.occurred_at),
                fact or "",
                (event,),
            )
        )
    return items, f"frame:{rows[limit - 1][0]}" if len(rows) > limit else None


def _event(
    event: EventRecord, *, threshold: int | None = None, deduct_short: bool = True
) -> HistoryItem:
    when = timestamp_text(event.occurred_at)
    if event.layer in {"conversation", "notification"}:
        msg = event_message(event)
        return HistoryItem(
            event.event_id,
            "user",
            msg.origin or event.device_id,
            when,
            str(event.payload["text"]) if event.layer == "conversation" else msg.text,
            media=(event,)
            if event.payload.get("image_hashes") or event.payload.get("documents")
            else (),
        )
    if event.layer == "perception":
        activity = perception_activity(event)
        msg = activity_message(activity)
        result = "unprojected"
        if threshold is not None:
            decisions: list[RuleDecision] = []
            if (
                filter_perception(
                    activity, decisions, threshold=threshold, deduct_short=deduct_short
                )
                is None
            ):
                result = "filtered"
            elif any(
                not row["kept"] or (row["kind"] == "input" and row["action"] == "redacted")
                for row in decisions
            ):
                result = "partial"
            else:
                result = "kept"
        return HistoryItem(
            event.event_id,
            "event",
            origin_text("感知", event.device_id, event.source_id),
            when,
            msg.text,
            media=(event,),
            raw=event,
            filter_result=result,
        )
    source = origin_text("切片", event.device_id, event.source_id)
    if event.type == "slice.recorded":
        text = str(event.payload["text"])
        if event.payload.get("metadata"):
            text += "\n" + body_text(event.payload["metadata"])
        return HistoryItem(event.event_id, "event", source, when, text)
    capture = event.payload.get("capture")
    return HistoryItem(
        event.event_id, "event", source, when, body_text(capture) if capture else "", (event,)
    )
