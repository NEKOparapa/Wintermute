"""Conversation-scoped, chronological compression of filtered observations and slice facts."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

from wintermute.agent import UserMessage
from wintermute.events.core.activity import REVISION_NOTE, ActivityItem, timeline_line
from wintermute.events.core.perception_rules import perception_line
from wintermute.events.core.perception_tokens import count_tokens
from wintermute.events.core.presentation import timestamp_text
from wintermute.events.core.timeline_compression import TimelinePolicy


def initialize(db: sqlite3.Connection, policy: TimelinePolicy) -> None:
    db.execute(
        "CREATE TABLE merged_batches(id INTEGER PRIMARY KEY AUTOINCREMENT,room TEXT NOT NULL,"
        "start INTEGER NOT NULL,first_seq INTEGER NOT NULL,end INTEGER NOT NULL,last_seq "
        "INTEGER NOT NULL,"
        "size INTEGER NOT NULL,tokens INTEGER NOT NULL,synopsis TEXT NOT NULL,ai TEXT,"
        "attempts INTEGER NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0)"
    )
    db.execute("CREATE INDEX merged_intervals ON merged_batches(room,end,last_seq)")
    db.execute("CREATE INDEX merged_jobs ON merged_batches(retry_at,id) WHERE ai IS NULL")
    db.execute(
        "CREATE TABLE merged_entries(room TEXT NOT NULL,seq INTEGER NOT NULL REFERENCES "
        "activity_items(seq),"
        "at INTEGER NOT NULL,layer TEXT NOT NULL,slice_key TEXT,line TEXT NOT NULL,tokens "
        "INTEGER NOT NULL,"
        "batch INTEGER REFERENCES merged_batches(id) ON DELETE SET NULL,PRIMARY KEY(room,seq))"
    )
    db.execute("CREATE INDEX merged_unbatched ON merged_entries(room,at,seq) WHERE batch IS NULL")
    db.execute("CREATE INDEX merged_order ON merged_entries(room,at,seq)")
    db.execute("CREATE INDEX merged_members ON merged_entries(batch,at,seq)")
    db.execute(
        "CREATE UNIQUE INDEX merged_slice ON merged_entries(room,slice_key) WHERE slice_key "
        "IS NOT NULL"
    )
    # Preserve delivered transcripts. Rebuild the live projection from scoped source receipts.
    for row in db.execute(
        "SELECT d.conversation_id,i.seq,i.data,m.perception_line "
        "FROM activity_items i JOIN activity_deliveries d ON d.activity_seq=i.seq "
        "LEFT JOIN timeline_batch_members m ON m.activity_seq=i.seq ORDER BY i.seq"
    ):
        item = ActivityItem.model_validate_json(row["data"])
        line = row["perception_line"]
        if line is None:
            line = perception_line(item) if item.kind == "perception" else timeline_line(item)
        add(db, row["conversation_id"], row["seq"], item, line)
    for (room,) in db.execute("SELECT DISTINCT room FROM merged_entries").fetchall():
        prepare(db, room, policy)


def add(db: sqlite3.Connection, room: str, seq: int, item: ActivityItem, line: str) -> None:
    if not line:
        return
    at = round(item.occurred_at.timestamp() * 1_000_000)
    key = item.activity_key if item.kind == "slice" else None
    if key is not None:
        db.execute(
            "DELETE FROM merged_batches WHERE id=(SELECT batch FROM merged_entries WHERE "
            "room=? AND slice_key=?)",
            (room, key),
        )
        db.execute("DELETE FROM merged_entries WHERE room=? AND slice_key=?", (room, key))
    # Invalidate a frozen interval before inserting a late event into it. An in-flight
    # result has a never-reused ID, so finishing it after invalidation is harmless.
    db.execute(
        "DELETE FROM merged_batches WHERE room=? AND (start,first_seq)<(?,?) AND (end,"
        "last_seq)>(?,?)",
        (room, at, seq, at, seq),
    )
    label = "感知" if item.kind == "perception" else "切片"
    # Some short window descriptions carry only a clock time. Include the date for
    # cross-device, multi-day model input and avoid repeating existing source labels.
    instant = timestamp_text(item.occurred_at)
    if not line.startswith(instant):
        line = f"{instant} | {label} · {item.terminal_id} | {line}"
    db.execute(
        "INSERT INTO merged_entries VALUES(?,?,?,?,?,?,?,NULL)",
        (room, seq, at, item.kind, key, line, count_tokens(line)),
    )


def excerpt(text: str, limit: int) -> str:
    if count_tokens(text) <= limit:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens(text[:mid] + "…") <= limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def prepare(db: sqlite3.Connection, room: str, policy: TimelinePolicy) -> None:
    newest = db.execute(
        "SELECT seq FROM merged_entries WHERE room=? ORDER BY at DESC,seq DESC LIMIT 1", (room,)
    ).fetchone()
    if newest is None:
        return
    rows = db.execute(
        "SELECT e.seq,e.at,e.tokens,(SELECT b.id FROM merged_batches b WHERE b.room=e.room "
        "AND (b.end,b.last_seq)<(e.at,e.seq) ORDER BY b.end DESC,b.last_seq DESC LIMIT 1) barrier "
        "FROM merged_entries e WHERE room=? AND batch IS NULL AND seq!=? ORDER BY at,seq",
        (room, newest[0]),
    ).fetchall()
    group: list[sqlite3.Row] = []
    total = 0
    for row in rows:
        if group and group[-1]["barrier"] != row["barrier"]:
            group, total = [], 0
        group.append(row)
        total += row["tokens"]
        if total < min(policy.raw_tokens, policy.delivery_tokens):
            continue
        ids = json.dumps([r["seq"] for r in group])
        lines = [
            r[0]
            for r in db.execute(
                "SELECT line FROM merged_entries WHERE room=? AND seq IN (SELECT value FROM "
                "json_each(?)) ORDER BY at,seq",
                (room, ids),
            )
        ]
        budget = min(policy.summary_tokens, policy.delivery_tokens // 2)
        # Keep facts from both ends in the model-free preview, with explicit omissions.
        per_line = max(1, budget // len(lines))
        synopsis = excerpt("\n".join(excerpt(line, per_line) for line in lines), budget)
        batch = db.execute(
            "INSERT INTO merged_batches(room,start,first_seq,end,last_seq,size,tokens,"
            "synopsis) VALUES(?,?,?,?,?,?,?,?)",
            (
                room,
                group[0]["at"],
                group[0]["seq"],
                row["at"],
                row["seq"],
                len(group),
                total,
                synopsis,
            ),
        ).lastrowid
        db.execute(
            "UPDATE merged_entries SET batch=? WHERE room=? AND seq IN (SELECT value FROM "
            "json_each(?))",
            (batch, room, ids),
        )
        group, total = [], 0


def stamp(at: int) -> str:
    return timestamp_text(datetime.fromtimestamp(at / 1_000_000, UTC))


def history_rows(
    db: sqlite3.Connection, room: str, before: tuple[int, int, int], limit: int
) -> Iterator[sqlite3.Row]:
    return iter(
        db.execute(
            "SELECT e.*,i.activity_id,json_extract(i.data,'$.terminal_id') terminal,"
            "b.size,COALESCE(b.ai,b.synopsis) summary FROM merged_entries e "
            "JOIN activity_items i ON i.seq=e.seq LEFT JOIN merged_batches b ON b.id=e.batch "
            "WHERE e.room=? AND (e.at,e.seq)<(?,?) AND (e.batch IS NULL OR e.seq=b.last_seq) "
            "ORDER BY e.at DESC,e.seq DESC LIMIT ?",
            (room, before[0], before[1], limit),
        )
    )


def pending(
    db: sqlite3.Connection,
    room: str,
    query: str,
    scope: tuple[str | float | int, ...],
    revised: bool,
    policy: TimelinePolicy,
) -> UserMessage | None:
    # Use a summary only when the entire interval belongs to this delivery. Otherwise
    # show exactly its eligible members (e.g. when part was read by an earlier run).
    rows = db.execute(
        "WITH eligible AS (SELECT e.*,COUNT(*) OVER(PARTITION BY e.batch) n FROM merged_entries e "
        "JOIN (SELECT i.seq " + query + ") v ON v.seq=e.seq WHERE e.room=?) "
        "SELECT at,CASE WHEN n=b.size THEN '合并摘要 · '||COALESCE(b.ai,b.synopsis) "
        "ELSE line END text "
        "FROM eligible e LEFT JOIN merged_batches b ON b.id=e.batch WHERE e.batch IS NULL "
        "OR n!=b.size OR e.seq=b.last_seq ORDER BY at DESC,seq DESC",
        (*scope, room),
    )
    newest = None
    parts: list[str] = []
    budget = policy.delivery_tokens
    for row in rows:
        if newest is None:
            newest = stamp(row["at"])
            header = UserMessage(
                (REVISION_NOTE if revised else "") + "\n…", "timeline", newest, "活动时间线"
            )
            budget -= count_tokens(header.model_text()) + 4
        cost = count_tokens(row["text"]) + 1
        if cost > budget:
            if not parts and budget > 0:
                parts.append(excerpt(row["text"], budget))
            parts.append("…")
            break
        parts.append(row["text"])
        budget -= cost
    if newest is None:
        return None
    if revised:
        parts.append(REVISION_NOTE)
    return UserMessage("\n".join(reversed(parts)), "timeline", newest, "活动时间线")
