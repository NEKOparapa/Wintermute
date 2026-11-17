"""Trace a visible journal record or activity back to its material and stored extraction."""

import json
import re
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, TypedDict

from wintermute.events.core.activity import ActivityItem, activity_message, perception_activity
from wintermute.events.core.slices import SliceStreamItem
from wintermute.events.core.types import EventRecord
from wintermute.events.storage.read_snapshot import read_snapshot


class Extraction(TypedDict):
    status: Literal["ready", "no_change", "pending", "retrying", "not_recorded"]
    text: str | None
    strategy: str | None
    source_count: int


@dataclass(frozen=True)
class SourceRecord:
    event: EventRecord
    # An empty transcript means ASR finished and heard silence.
    transcript: str | None


@dataclass(frozen=True)
class SourcePage:
    items: list[SourceRecord]
    next_cursor: str | None
    extraction: Extraction


def _raw_event(
    db: sqlite3.Connection, user: str, conversation: str, source_id: str
) -> EventRecord | None:
    row = db.execute(
        "SELECT data FROM perception_events p WHERE p.event_id=?1 AND p.user_id=?2 "
        "AND (p.conversation_id=?3 OR p.conversation_id IS NULL) "
        "UNION ALL SELECT data FROM slice_events WHERE event_id=?1 AND user_id=?2 "
        "AND (conversation_id=?3 OR conversation_id IS NULL)",
        (source_id, user, conversation),
    ).fetchone()
    return EventRecord.model_validate_json(row[0]) if row else None


def _visible_summary(
    db: sqlite3.Connection, user: str, conversation: str, identifier: str | int
) -> tuple[int, SliceStreamItem]:
    key = "seq" if isinstance(identifier, int) else "item_id"
    row = db.execute(
        f"SELECT seq,data FROM slice_stream WHERE {key}=? AND user_id=? "
        "AND conversation_scope IN (?, '')",
        (identifier, user, conversation),
    ).fetchone()
    if row is None:
        raise LookupError("Unknown source")
    summary = SliceStreamItem.model_validate_json(row[1])
    if summary.user_id != user or summary.conversation_id not in (None, conversation):
        raise LookupError("Unknown source")
    # Check every contributor before exposing the combined text, including on later pages.
    count = db.execute(
        "SELECT COUNT(*) FROM slice_events WHERE user_id=? AND conversation_id IS ? "
        "AND event_id IN (SELECT value FROM json_each(?))",
        (user, summary.conversation_id, json.dumps(summary.source_event_ids)),
    ).fetchone()[0]
    if count != len(summary.source_event_ids):
        raise LookupError("Unknown source")
    return row[0], summary


def _summary_page(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    item_id: str,
    selected: str | None,
    cursor: str | None,
    limit: int,
) -> SourcePage:
    # A raw event normally opens its latest extraction. A continuation always reopens
    # the immutable version seen on the first page, even after a background revision.
    offset = 0
    identifier: int | str = item_id
    start_id = selected or item_id
    token = sha256(start_id.encode()).hexdigest()[:24]
    if cursor is not None:
        match = re.fullmatch(rf"source:{token}:([1-9][0-9]{{0,18}}):([1-9][0-9]{{0,17}})", cursor)
        if not match or int(match[1]) > 2**63 - 1:
            raise ValueError("Invalid source cursor")
        identifier, offset = int(match[1]), int(match[2])
    seq, summary = _visible_summary(db, user, conversation, identifier)
    if selected is None and summary.item_id != item_id:
        raise ValueError("Invalid source cursor")
    ids = list(summary.source_event_ids)
    if offset >= len(ids):
        raise ValueError("Invalid source cursor")
    if selected:
        if selected not in ids:
            raise LookupError("Unknown source")
        ids.remove(selected)
        ids.insert(0, selected)
    rows = db.execute(
        "SELECT e.data,t.text FROM json_each(?) j JOIN slice_events e ON e.event_id=j.value "
        "LEFT JOIN audio_transcripts t ON t.event_id=e.event_id "
        "WHERE e.user_id=? AND e.conversation_id IS ? ORDER BY CAST(j.key AS INTEGER)",
        (json.dumps(ids[offset : offset + limit]), user, summary.conversation_id),
    )
    return SourcePage(
        [SourceRecord(EventRecord.model_validate_json(row[0]), row[1]) for row in rows],
        f"source:{token}:{seq}:{offset + limit}" if offset + limit < len(ids) else None,
        Extraction(
            status="ready", text=summary.text, strategy=summary.strategy, source_count=len(ids)
        ),
    )


def _read(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    source_id: str,
    activity: bool,
    cursor: str | None,
    limit: int,
) -> SourcePage:
    if not db.execute(
        "SELECT 1 FROM conversations WHERE conversation_id=? AND user_id=?", (conversation, user)
    ).fetchone():
        raise LookupError("Unknown source")
    item = None
    if activity:
        row = db.execute(
            "SELECT i.data FROM activity_items i "
            "JOIN activity_deliveries d ON d.activity_seq=i.seq "
            "WHERE i.activity_id=? AND i.user_id=? AND d.conversation_id=?",
            (source_id, user, conversation),
        ).fetchone()
        if row is None:
            raise LookupError("Unknown source")
        item = ActivityItem.model_validate_json(row[0])
        if item.kind == "slice":
            return _summary_page(db, user, conversation, source_id, None, cursor, limit)
    original = item.source_event_ids[0] if item is not None else source_id
    event = _raw_event(db, user, conversation, original)
    if event is None:
        if not activity and source_id.startswith("transcript-"):
            raw = _raw_event(db, user, conversation, source_id.removeprefix("transcript-"))
            if (
                raw
                and raw.type == "slice.audio"
                and db.execute(
                    "SELECT 1 FROM audio_transcripts WHERE event_id=?", (raw.event_id,)
                ).fetchone()
            ):
                event = raw
        if event is None:
            if activity:
                raise LookupError("Unknown source")
            return _summary_page(db, user, conversation, source_id, None, cursor, limit)
    extraction = Extraction(status="not_recorded", text=None, strategy=None, source_count=1)
    transcript = None
    if event.layer == "perception":
        if item is None and "image_recognition" in event.payload:
            item = perception_activity(event)
        if item is None:
            row = db.execute(
                "SELECT data FROM activity_items WHERE activity_id=? AND user_id=?",
                (event.event_id, user),
            ).fetchone()
            if row:
                item = ActivityItem.model_validate_json(row[0])
        if item is not None:
            extraction["status"] = "ready"
            extraction["text"] = activity_message(item).text
    else:
        status = db.execute(
            "SELECT p.item_id,p.frame_done,p.retry_at,t.text FROM slice_processing p "
            "LEFT JOIN audio_transcripts t ON t.event_id=p.event_id WHERE p.event_id=?",
            (event.event_id,),
        ).fetchone()
        if status:
            item_id, frame_done, retry_at, transcript = status
            if item_id is not None:
                return _summary_page(db, user, conversation, item_id, event.event_id, cursor, limit)
            if frame_done:
                extraction["status"] = "no_change"
            elif retry_at is not None:
                extraction["status"] = "retrying"
            else:
                extraction["status"] = "pending"
    if cursor is not None:
        raise ValueError("Invalid source cursor")
    return SourcePage([SourceRecord(event, transcript)], None, extraction)


def browse_sources(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    source_id: str,
    *,
    activity: bool = False,
    cursor: str | None = None,
    limit: int = 20,
) -> SourcePage:
    if not 1 <= limit <= 100:
        raise ValueError("Invalid page size")
    with read_snapshot(db):
        return _read(db, user, conversation, source_id, activity, cursor, limit)
