"""Incremental, device-scoped summaries. Conversation delivery owns a separate read cursor."""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from wintermute.agent.core.types import UserMessage
from wintermute.events.core.activity import REVISION_NOTE, ActivityItem, timeline_line
from wintermute.events.core.presentation import timestamp_text
from wintermute.events.core.timeline_compression import (
    BatchText,
    Segment,
    TimelinePolicy,
    batch_text,
    clip,
    estimate,
    raw_size,
    render_timeline,
)
from wintermute.events.storage import timeline_rollups


@dataclass(frozen=True, slots=True)
class TimelineBatch:
    batch_id: int
    rule_text: str
    attempts: int
    output_budget: int | None = None
    previous_prefix_chars: int = 0


def initialize(db: sqlite3.Connection, policy: TimelinePolicy) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS timeline_batches("
        "batch_id INTEGER PRIMARY KEY,user_id TEXT NOT NULL,scope TEXT NOT NULL,"
        "terminal_id TEXT NOT NULL,day TEXT NOT NULL,first_at REAL NOT NULL,last_at REAL NOT NULL,"
        "last_known REAL NOT NULL,item_count INTEGER NOT NULL DEFAULT 0,"
        "raw_tokens INTEGER NOT NULL DEFAULT 0,rule_text TEXT NOT NULL DEFAULT '',"
        "stats_text TEXT NOT NULL DEFAULT '',rule_tokens INTEGER NOT NULL DEFAULT 0,"
        "sealed INTEGER NOT NULL DEFAULT 0,ai_text TEXT,attempts INTEGER NOT NULL DEFAULT 0,"
        "retry_at REAL NOT NULL DEFAULT 0)"
    )
    enable_screenshot_threshold(db)
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS timeline_open_scope "
        "ON timeline_batches(user_id,scope,terminal_id) WHERE sealed=0"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS timeline_idle ON timeline_batches(last_known) WHERE sealed=0"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS timeline_ai_pending ON timeline_batches(rule_tokens,retry_at) "
        "WHERE sealed=1 AND ai_text IS NULL"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS timeline_batch_members("
        "activity_seq INTEGER PRIMARY KEY REFERENCES activity_items(seq),"
        "batch_id INTEGER NOT NULL REFERENCES timeline_batches(batch_id),is_slice INTEGER NOT NULL,"
        "raw_tokens INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS timeline_batch_contents ON timeline_batch_members(batch_id)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS timeline_delivery_heads("
        "conversation_id TEXT PRIMARY KEY,max_at REAL NOT NULL)"
    )
    db.execute(
        "INSERT OR IGNORE INTO timeline_delivery_heads SELECT d.conversation_id,MAX(i.occurred_at) "
        "FROM activity_deliveries d JOIN activity_items i ON i.seq=d.activity_seq WHERE applied=1 "
        "GROUP BY d.conversation_id"
    )
    timeline_rollups.initialize_rollups(db)
    # Only unconsumed activity benefits from a backfill. Committed transcripts remain immutable.
    for row in db.execute(
        "SELECT i.seq,i.data FROM (SELECT DISTINCT activity_seq FROM activity_deliveries "
        "WHERE applied=0) p JOIN activity_items i ON i.seq=p.activity_seq WHERE NOT EXISTS("
        "SELECT 1 FROM timeline_batch_members m WHERE m.activity_seq=i.seq) ORDER BY i.seq"
    ):
        append(db, row["seq"], ActivityItem.model_validate_json(row["data"]), policy)


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


def enable_screenshot_threshold(db: sqlite3.Connection) -> None:
    if "screenshot_fact_bytes" not in {
        r[1] for r in db.execute("PRAGMA table_info(timeline_batches)")
    }:
        db.execute("ALTER TABLE timeline_batches ADD COLUMN screenshot_fact_bytes INTEGER")


def append(
    db: sqlite3.Connection,
    seq: int,
    item: ActivityItem,
    policy: TimelinePolicy,
    screenshot_fact_bytes: int | None = None,
) -> None:
    scope = (item.user_id, item.conversation_id or "", item.terminal_id)
    head = db.execute(
        "SELECT * FROM timeline_batches WHERE user_id=? AND scope=? AND terminal_id=? AND sealed=0",
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
            "INSERT INTO timeline_batches(user_id,scope,terminal_id,day,first_at,last_at,"
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
# Pre-migration, already-consumed observations may lack batch membership. Rebuild them
# only inside this suffix, in bounded groups of at most 32 rows.
_GROUP = "COALESCE(m.batch_id, -1-(i.seq/32))"
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
_BATCH_FROM = """
    FROM timeline_batch_members m INDEXED BY timeline_batch_contents
    CROSS JOIN activity_items i ON i.seq=m.activity_seq
    CROSS JOIN activity_deliveries d ON d.activity_seq=i.seq
    """
_LEGACY_FROM = """
    FROM activity_items i NOT INDEXED
    CROSS JOIN activity_deliveries d ON d.activity_seq=i.seq
    LEFT JOIN timeline_batch_members m ON m.activity_seq=i.seq
    """


def _load_items(
    db: sqlite3.Connection,
    revised: bool,
    scope: tuple[str | float | int, ...],
    batch_id: int | None = None,
    *,
    slices_only: bool = False,
) -> list[ActivityItem]:
    if batch_id is None:
        query = _SUFFIX if revised else _VISIBLE
    elif batch_id > 0:
        # Seek the batch, then point-lookup this conversation's delivery receipts.
        query = _BATCH_FROM + (_SUFFIX_WHERE if revised else _UNREAD_WHERE) + " AND m.batch_id=?"
        scope = (*scope, batch_id)
    else:
        lower = (-1 - batch_id) * 32
        query = _LEGACY_FROM + _SUFFIX_WHERE + " AND i.seq BETWEEN ? AND ? AND m.batch_id IS NULL"
        scope = (*scope, lower, lower + 31)
    if slices_only:
        query += " AND m.perception_line IS NULL"
    rows = db.execute("SELECT i.data " + query + " ORDER BY i.occurred_at,i.seq", scope)
    return [ActivityItem.model_validate_json(row[0]) for row in rows]


def pending(
    db: sqlite3.Connection, conversation: str, user: str, policy: TimelinePolicy
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

    if db.execute(
        "SELECT 1 " + query + " AND m.perception_line IS NOT NULL LIMIT 1", scope
    ).fetchone():
        return _with_perception(db, conversation, query, scope, revised, policy)
    return render_timeline(
        _slice_segments(db, query, scope, revised, policy), policy, revision=revised
    )


def _with_perception(
    db: sqlite3.Connection,
    conversation: str,
    query: str,
    scope: tuple[str | float | int, ...],
    revised: bool,
    policy: TimelinePolicy,
) -> UserMessage:
    checkpoint = db.execute(
        "SELECT s.covered,b.last_at,b.ai_text FROM perception_buffers s JOIN timeline_batches b "
        "ON b.batch_id=s.previous_id WHERE s.conversation_id=?",
        (conversation,),
    ).fetchone()
    covered = checkpoint["covered"] if checkpoint else 0
    # In a revision, previously read rows after the insertion point still belong in the suffix.
    # The checkpoint covers only this generation's unread perception, never those rows or slices.
    parts: list[tuple[float, int, str]] = []
    length = 0
    for row in db.execute(
        "SELECT i.occurred_at,i.seq,substr(m.perception_line,1,?) "
        + query
        + " AND m.perception_line IS NOT NULL AND (d.applied=1 OR i.seq>?) "
        "ORDER BY i.occurred_at DESC,i.seq DESC LIMIT ?",
        (policy.delivery_tokens + 1, *scope, covered, policy.delivery_tokens + 1),
    ):
        parts.append((row[0], row[1], row[2]))
        length += estimate(row[2]) + 1
        if length > policy.delivery_tokens:
            break
    if checkpoint:
        parts.append((checkpoint["last_at"], -1, "感知摘要：" + checkpoint["ai_text"]))
    for segment in _slice_segments(db, query, scope, revised, policy, slices_only=True):
        if isinstance(segment, BatchText):
            block = render_timeline([segment], policy)
            assert block is not None
            parts.append((segment.end.timestamp(), 0, block.text))
        else:
            # Equal timestamps from different layers keep the original ingestion order.
            items = segment[0]
            sequences = dict(
                db.execute(
                    "SELECT activity_id,seq FROM activity_items WHERE activity_id IN "
                    "(SELECT value FROM json_each(?))",
                    (json.dumps([i.activity_id for i in items]),),
                )
            )
            parts.extend(
                (i.occurred_at.timestamp(), sequences[i.activity_id], timeline_line(i))
                for i in items
            )
    parts.sort(key=lambda p: (p[0], p[1]))
    stamp = timestamp_text(datetime.fromtimestamp(parts[-1][0], UTC))
    note = REVISION_NOTE + "\n" if revised else ""

    def message(body: str) -> UserMessage:
        return UserMessage(note + body, "timeline", stamp, "活动时间线")

    if revised and estimate(message("").model_text()) + 48 > policy.delivery_tokens:
        note = "顺序修订：\n"
    budget = policy.delivery_tokens - estimate(message("").model_text())
    body = "\n".join(p[2] for p in parts)
    if estimate(body) <= budget:
        return message(body)
    # During inference/outage keep the latest details and the completed checkpoint, within
    # the same hard limit. Projecting this fallback must never advance the worker's cursor.
    marker = "…"
    remaining = budget - estimate(marker) - 1
    selected = []
    if checkpoint:
        at = next(i for i, p in enumerate(parts) if p[1] == -1)
        excerpt = clip(parts[at][2], remaining // 2 if len(parts) > 1 else remaining)
        selected.append((at, excerpt))
        remaining -= estimate(excerpt) + 1
    for index in reversed(range(len(parts))):
        if parts[index][1] == -1:
            continue
        line = clip(parts[index][2], remaining)
        if line:
            selected.append((index, line))
            remaining -= estimate(line) + 1
        if remaining <= 0:
            break
    return message("\n".join([marker, *(s for _, s in sorted(selected))]))


def _slice_segments(
    db: sqlite3.Connection,
    query: str,
    scope: tuple[str | float | int, ...],
    revised: bool,
    policy: TimelinePolicy,
    *,
    slices_only: bool = False,
) -> list[Segment]:
    if slices_only:
        query += " AND m.perception_line IS NULL"
    # Group visibility in SQL, not in a Python list of all unread observations. Only
    # complete groups can use a cached summary. An oversized rule cannot fit the prompt;
    # its full text stays available to the worker, its latest facts to the renderer.
    rows = db.execute(
        f"WITH visible AS (SELECT {_GROUP} batch_id,COUNT(*) visible_count,"
        "SUM(m.raw_tokens) raw_tokens " + query + f" GROUP BY {_GROUP}) "
        "SELECT v.*,b.terminal_id,b.item_count,b.first_at,b.last_at,b.ai_text,b.stats_text,"
        "CASE WHEN b.ai_text IS NULL THEN substr(b.rule_text,1,?) ELSE '' END rule_text "
        "FROM visible v LEFT JOIN timeline_batches b USING(batch_id) "
        "ORDER BY b.first_at,b.last_at,b.batch_id",
        (*scope, policy.delivery_tokens + 1),
    ).fetchall()
    if sum(
        b["raw_tokens"] or policy.delivery_tokens + 1 for b in rows
    ) <= policy.delivery_tokens and not any(
        b["ai_text"] and b["item_count"] == b["visible_count"] for b in rows
    ):
        return [(_load_items(db, revised, scope, slices_only=slices_only), None)] if rows else []

    pieces: list[tuple[int | None, BatchText]] = []
    for b in rows:
        if b["visible_count"] == b["item_count"]:
            pieces.append(
                (
                    b["batch_id"],
                    BatchText(
                        datetime.fromtimestamp(b["first_at"], UTC),
                        datetime.fromtimestamp(b["last_at"], UTC),
                        b["item_count"],
                        b["rule_text"],
                        b["ai_text"],
                        b["stats_text"],
                        terminal_id=b["terminal_id"],
                    ),
                )
            )
        else:
            # A conversation may have joined or read part of this batch before it was sealed.
            # A revised slice can also make only part of the original batch eligible.
            items = _load_items(db, revised, scope, b["batch_id"])
            block = batch_text(items, policy)
            pieces.append(
                (None, replace(block, rule_text=block.rule_text[: policy.delivery_tokens + 1]))
            )
    pieces.sort(key=lambda p: (p[1].start, p[1].end))
    return list(timeline_rollups.collapse_rollups(db, pieces, policy))
