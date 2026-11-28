"""Conversation-scoped, chronological compression of filtered observations and slice facts."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

from purin.agent import UserMessage
from purin.events.core.activity import REVISION_NOTE, ActivityItem
from purin.events.core.perception_tokens import count_tokens
from purin.events.core.presentation import timestamp_text
from purin.events.core.timeline_compression import TimelinePolicy


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
        line = f"{instant} | {label} · {item.device_id} | {line}"
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


SPAN_COLUMNS = (
    "CASE WHEN json_extract(i.data,'$.content.state_key') "
    "IN ('desktop.window_span','mobile.app_usage') THEN "
    "json_extract(i.data,'$.content.value.started_at') END started,"
    "COALESCE(json_extract(i.data,'$.ended_at'),CASE WHEN "
    "json_extract(i.data,'$.content.state_key') IN ('desktop.window_span','mobile.app_usage') THEN "
    "json_extract(i.data,'$.content.value.ended_at') END) ended"
)


def activity_span(row: sqlite3.Row) -> tuple[datetime, datetime]:
    at = datetime.fromtimestamp(row["at"] / 1_000_000, UTC)

    def endpoint(value: object) -> datetime:
        if isinstance(value, str):
            try:
                when = datetime.fromisoformat(value)
                if when.tzinfo is not None:
                    return when.astimezone(UTC)
            except (ValueError, OverflowError):
                pass
        return at

    return min(at, endpoint(row["started"])), max(at, endpoint(row["ended"]))


def history_rows(
    db: sqlite3.Connection, room: str, before: tuple[int, int, int], limit: int
) -> Iterator[sqlite3.Row]:
    return iter(
        db.execute(
            "SELECT e.at AS at,e.seq AS seq,0 part,e.layer,i.activity_id,"
            "json_extract(i.data,'$.device_id') "
            "device_id,e.line text,NULL batch,NULL generated_at,NULL is_ai,"
            "NULL covered_from,NULL covered_until FROM merged_entries e "
            "JOIN activity_items i ON i.seq=e.seq WHERE e.room=:room "
            "AND (e.at,e.seq,0)<(:at,:seq,:part) UNION ALL "
            "SELECT COALESCE(b.generated_at,b.end),b.last_seq,1,NULL,NULL,NULL,"
            "COALESCE(b.ai,b.synopsis),b.id,b.generated_at,b.ai IS NOT NULL,NULL,NULL "
            "FROM merged_batches b "
            "WHERE b.room=:room AND COALESCE(b.generated_at,b.end)<=:at "
            "AND (COALESCE(b.generated_at,b.end),b.last_seq,1)<(:at,:seq,:part) "
            "UNION ALL SELECT w.at,w.history_seq,w.slot+2,NULL,NULL,NULL,w.original,NULL,NULL,NULL,"
            "w.covered_from,w.covered_until FROM merged_context_writes w WHERE w.room=:room "
            "AND (w.at,w.history_seq,w.slot+2)<(:at,:seq,:part) "
            "ORDER BY at DESC,seq DESC,part DESC LIMIT :limit",
            dict(room=room, at=before[0], seq=before[1], part=before[2], limit=limit),
        )
    )


def pending(
    db: sqlite3.Connection,
    room: str,
    query: str,
    scope: tuple[str | float | int, ...],
    revised: bool,
    policy: TimelinePolicy,
    included: list[int] | None = None,
) -> UserMessage | None:
    # Use a summary only when the entire interval belongs to this delivery. Otherwise
    # show exactly its eligible members (e.g. when part was read by an earlier run).
    rows = db.execute(
        "WITH eligible AS (SELECT e.*,COUNT(*) OVER(PARTITION BY e.batch) n FROM merged_entries e "
        "JOIN (SELECT i.seq " + query + ") v ON v.seq=e.seq WHERE e.room=?) "
        "SELECT at,seq,CASE WHEN n=b.size THEN b.id END summary_id,"
        "CASE WHEN n=b.size THEN '合并摘要 · '||COALESCE(b.ai,b.synopsis) "
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
        piece = row["text"]
        if cost > budget:
            if parts or budget <= 0:
                parts.append("…")
                break
            piece = excerpt(piece, budget)
        parts.append(piece)
        if included is not None and piece != "…":
            if row["summary_id"] is None:
                included.append(row["seq"])
            else:
                included.extend(
                    r[0]
                    for r in db.execute(
                        "SELECT seq FROM merged_entries WHERE batch=?", (row["summary_id"],)
                    )
                )
        if cost > budget:
            parts.append("…")
            break
        budget -= cost
    if newest is None:
        return None
    if revised:
        parts.append(REVISION_NOTE)
    return UserMessage("\n".join(reversed(parts)), "timeline", newest, "活动时间线")
