"""A conversation's unread perception consists of one checkpoint and an append-only tail."""

import json
import sqlite3
from datetime import UTC, datetime

from purin.events.core.activity import ActivityItem
from purin.events.core.perception_tokens import count_tokens
from purin.events.core.presentation import timestamp_text
from purin.events.core.timeline_compression import TimelinePolicy, clip, raw_size


def append(
    db: sqlite3.Connection,
    seq: int,
    item: ActivityItem,
    line: str,
    threshold: int,
    deduct_short: bool,
) -> None:
    at = item.occurred_at.timestamp()
    cost = raw_size(item)
    tokens = count_tokens(line)
    batch = db.execute(
        "INSERT INTO timeline_batches(user_id,scope,device_id,day,first_at,last_at,last_known,"
        "item_count,raw_tokens,rule_text,sealed) VALUES(?,?,?,?,?,?,?,1,?,?,1)",
        (
            item.user_id,
            item.conversation_id or "",
            item.device_id,
            item.occurred_at.date().isoformat(),
            at,
            at,
            item.known_at.timestamp(),
            cost,
            line,
        ),
    ).lastrowid
    db.execute(
        "INSERT INTO timeline_batch_members(activity_seq,batch_id,is_slice,raw_tokens,"
        "perception_line,dwell_threshold,deduct_short,perception_tokens) VALUES(?,?,?,?,?,?,?,?)",
        (
            seq,
            batch,
            item.kind == "slice",
            cost,
            line if item.kind == "perception" else None,
            threshold,
            deduct_short,
            tokens,
        ),
    )


def output_budget(policy: TimelinePolicy) -> int:
    return min(policy.summary_tokens, policy.delivery_tokens // 2)


def source_totals(
    db: sqlite3.Connection, conversation: str, user: str, where: str, params: tuple[object, ...]
) -> tuple[list[str], float | None]:
    duration = "json_extract(i.data,'$.content.value.duration_seconds')"
    rows = db.execute(
        "SELECT json_extract(i.data,'$.device_id') AS device, SUM(CASE WHEN "
        "json_type(i.data,'$.content.value.duration_seconds') IN ('integer','real') "
        f"AND {duration}>=0 THEN {duration} END) "
        "FROM activity_deliveries d JOIN activity_items i ON i.seq=d.activity_seq "
        "JOIN timeline_batch_members m ON m.activity_seq=d.activity_seq "
        f"WHERE d.conversation_id=? AND i.user_id=? AND m.perception_line IS NOT '' AND {where} "
        "GROUP BY device ORDER BY device",
        (conversation, user, *params),
    ).fetchall()
    durations = [row[1] for row in rows if row[1] is not None]
    return [row[0] for row in rows], sum(durations) if durations else None


def _save_sources(
    db: sqlite3.Connection,
    batch: int,
    parent: int | None,
    room: str,
    user: str,
    after: int,
    through: int,
) -> None:
    devices, seconds = source_totals(
        db, room, user, "m.is_slice=0 AND d.activity_seq>? AND d.activity_seq<=?", (after, through)
    )
    if parent is not None:
        prior = db.execute(
            "SELECT devices,duration_seconds FROM perception_rounds WHERE batch_id=?", (parent,)
        ).fetchone()
        devices = sorted(set(devices).union(json.loads(prior["devices"])))
        if prior["duration_seconds"] is not None:
            seconds = (seconds or 0) + prior["duration_seconds"]
    db.execute(
        "UPDATE perception_rounds SET previous_batch_id=?,devices=?,duration_seconds=? "
        "WHERE batch_id=?",
        (parent, json.dumps(devices), seconds, batch),
    )


def next_round(
    db: sqlite3.Connection, policy: TimelinePolicy, now: float
) -> tuple[int, str, int, int] | None:
    state = db.execute(
        "SELECT s.*,r.original_summary AS previous,"
        "p.first_at AS previous_start,p.last_at AS previous_end,"
        "j.rule_text,j.attempts "
        "FROM perception_buffers s LEFT JOIN timeline_batches p ON p.batch_id=s.previous_id "
        "LEFT JOIN perception_rounds r ON r.batch_id=s.previous_id "
        "LEFT JOIN timeline_batches j ON j.batch_id=s.job_id "
        "WHERE (s.job_id IS NOT NULL AND j.retry_at<=?) OR "
        "(s.job_id IS NULL AND s.tail_cost>=?) "
        "ORDER BY COALESCE(s.job_id,s.previous_id,0),s.covered,s.conversation_id LIMIT 1",
        (now, policy.delivery_tokens),
    ).fetchone()
    if state is None:
        return None
    previous = state["previous"]
    prefix = f"已有摘要：\n{previous}\n新增观测（按发生时间）：\n" if previous else ""
    if state["job_id"] is not None:
        return state["job_id"], state["rule_text"], state["attempts"], len(prefix)
    room = state["conversation_id"]
    records = db.execute(
        "SELECT i.seq,i.user_id,i.occurred_at,m.perception_line,m.perception_tokens "
        "FROM activity_deliveries d "
        "JOIN timeline_batch_members m ON m.activity_seq=d.activity_seq JOIN activity_items i "
        "ON i.seq=d.activity_seq WHERE d.conversation_id=? AND d.applied=0 AND d.activity_seq>? "
        "AND m.perception_line!='' ORDER BY d.activity_seq",
        (room, state["covered"]),
    )
    lines = []
    consumed = 0
    for row in records:
        lines.append(row)
        consumed += row["perception_tokens"]
        if consumed >= policy.delivery_tokens:
            break
    through = lines[-1]["seq"]
    lines.sort(key=lambda r: (r["occurred_at"], r["seq"]))
    text = prefix + "\n".join(r["perception_line"] for r in lines)
    start = lines[0]["occurred_at"]
    end = lines[-1]["occurred_at"]
    if previous:
        start = min(start, state["previous_start"])
        end = max(end, state["previous_end"])
    batch = db.execute(
        "INSERT INTO timeline_batches(user_id,scope,device_id,day,first_at,last_at,last_known,"
        "rule_text,sealed) VALUES(?,?,'',?,?,?,?,?,1)",
        (
            lines[0]["user_id"],
            room,
            datetime.fromtimestamp(end, UTC).date().isoformat(),
            start,
            end,
            now,
            text,
        ),
    ).lastrowid
    assert batch is not None
    db.execute(
        "INSERT INTO perception_rounds(batch_id,conversation_id,after_seq,through_seq,consumed) "
        "VALUES(?,?,?,?,?)",
        (batch, room, state["covered"], through, consumed),
    )
    _save_sources(
        db,
        batch,
        state["previous_id"] if previous else None,
        room,
        lines[0]["user_id"],
        state["covered"],
        through,
    )
    db.execute("UPDATE perception_buffers SET job_id=? WHERE conversation_id=?", (batch, room))
    return batch, text, 0, len(prefix)


def finish(
    db: sqlite3.Connection, batch: int, text: str, policy: TimelinePolicy, now: datetime
) -> bool:
    job = db.execute("SELECT * FROM perception_rounds WHERE batch_id=?", (batch,)).fetchone()
    if job is None:
        return False
    saved = db.execute(
        "UPDATE timeline_batches SET ai_text=? WHERE batch_id=? AND ai_text IS NULL",
        (clip(text, output_budget(policy)), batch),
    )
    if not saved.rowcount:
        return True
    db.execute("UPDATE perception_rounds SET original_summary=? WHERE batch_id=?", (text, batch))
    db.execute(
        "INSERT INTO perception_summary_history(batch_id,conversation_id,position_ms,generated_at)"
        " VALUES(?,?,?,?)",
        (batch, job["conversation_id"], round(now.timestamp() * 1000), timestamp_text(now)),
    )
    # A read discards this generation. Late/repeated completions cannot move another cursor.
    db.execute(
        "UPDATE perception_buffers SET previous_id=?,covered=?,tail_cost=("
        "SELECT COALESCE(SUM(m.perception_tokens),0) "
        "FROM activity_deliveries d JOIN timeline_batch_members m ON m.activity_seq=d.activity_seq "
        "WHERE d.conversation_id=? AND d.applied=0 AND d.activity_seq>?),"
        "job_id=NULL WHERE conversation_id=? AND job_id=?",
        (
            batch,
            job["through_seq"],
            job["conversation_id"],
            job["through_seq"],
            job["conversation_id"],
            batch,
        ),
    )
    return True
