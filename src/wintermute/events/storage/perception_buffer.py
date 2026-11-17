"""A conversation's unread perception consists of one checkpoint and an append-only tail."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from wintermute.events.core.activity import ActivityItem
from wintermute.events.core.timeline_compression import TimelinePolicy, clip, estimate, raw_size


def add_dwell_threshold(db: sqlite3.Connection) -> None:
    columns = {r[1] for r in db.execute("PRAGMA table_info(timeline_batch_members)")}
    if "dwell_threshold" not in columns:
        # Previously formatted lines were evaluated with the three-second rule.
        db.execute(
            "ALTER TABLE timeline_batch_members ADD COLUMN dwell_threshold INTEGER DEFAULT 3"
        )


def initialize(
    db: sqlite3.Connection, format_line: Callable[[ActivityItem], tuple[str, int]]
) -> None:
    columns = {r[1] for r in db.execute("PRAGMA table_info(timeline_batch_members)")}
    if "perception_line" not in columns:
        db.execute("ALTER TABLE timeline_batch_members ADD COLUMN perception_line TEXT")
    db.execute(
        "CREATE TABLE IF NOT EXISTS perception_buffers(conversation_id TEXT PRIMARY KEY,"
        "covered INTEGER NOT NULL,tail_cost INTEGER NOT NULL,previous_id INTEGER,job_id INTEGER)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS perception_rounds(batch_id INTEGER PRIMARY KEY REFERENCES "
        "timeline_batches(batch_id),conversation_id TEXT NOT NULL,after_seq INTEGER NOT NULL,"
        "through_seq INTEGER NOT NULL,consumed INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS perception_round_scope ON perception_rounds("
        "conversation_id,through_seq)"
    )
    # Detach pre-upgrade perception from time-window caches. Preserve source data and
    # receipts. A formerly mixed leaf is now partial and cannot expose its old summary.
    db.execute(
        "UPDATE timeline_batches SET sealed=1,rule_tokens=0 WHERE batch_id IN "
        "(SELECT batch_id FROM timeline_batch_members WHERE is_slice=0 AND perception_line IS NULL)"
    )
    db.execute(
        "UPDATE timeline_rollups SET sealed=1,rule_tokens=0 WHERE rollup_id IN "
        "(SELECT rm.rollup_id FROM timeline_rollup_members rm JOIN timeline_batch_members m "
        "USING(batch_id) WHERE m.is_slice=0 AND m.perception_line IS NULL)"
    )
    db.execute(
        "DELETE FROM timeline_rollup_members WHERE rollup_id IN "
        "(SELECT rm.rollup_id FROM timeline_rollup_members rm JOIN timeline_batch_members m "
        "USING(batch_id) WHERE m.is_slice=0 AND m.perception_line IS NULL)"
    )
    for r in db.execute(
        "SELECT i.seq,i.data FROM activity_items i LEFT JOIN timeline_batch_members m "
        "ON m.activity_seq=i.seq WHERE json_extract(i.data,'$.kind')='perception' "
        "AND m.perception_line IS NULL ORDER BY i.seq"
    ):
        item = ActivityItem.model_validate_json(r["data"])
        db.execute("DELETE FROM timeline_batch_members WHERE activity_seq=?", (r["seq"],))
        append(db, r["seq"], item, *format_line(item))


def append(db: sqlite3.Connection, seq: int, item: ActivityItem, line: str, threshold: int) -> None:
    at = item.occurred_at.timestamp()
    cost = raw_size(item)
    batch = db.execute(
        "INSERT INTO timeline_batches(user_id,scope,terminal_id,day,first_at,last_at,last_known,"
        "item_count,raw_tokens,rule_text,sealed) VALUES(?,?,?,?,?,?,?,1,?,?,1)",
        (
            item.user_id,
            item.conversation_id or "",
            item.terminal_id,
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
        "perception_line,dwell_threshold) VALUES(?,?,0,?,?,?)",
        (seq, batch, cost, line, threshold),
    )
    if line:
        db.execute(
            "INSERT INTO perception_buffers(conversation_id,covered,tail_cost) "
            "SELECT conversation_id,?,? FROM activity_deliveries "
            "WHERE activity_seq=? AND applied=0 "
            "ON CONFLICT(conversation_id) DO UPDATE SET tail_cost=tail_cost+excluded.tail_cost",
            (seq - 1, estimate(line) + 1, seq),
        )


def output_budget(policy: TimelinePolicy) -> int:
    return min(policy.summary_tokens, policy.delivery_tokens // 2)


def next_round(
    db: sqlite3.Connection, policy: TimelinePolicy, now: float
) -> tuple[int, str, int] | None:
    state = db.execute(
        "SELECT s.*,p.ai_text AS previous,p.first_at AS previous_start,p.last_at AS previous_end,"
        "p.user_id,j.rule_text,j.attempts "
        "FROM perception_buffers s LEFT JOIN timeline_batches p ON p.batch_id=s.previous_id "
        "LEFT JOIN timeline_batches j ON j.batch_id=s.job_id "
        "WHERE (s.job_id IS NOT NULL AND j.retry_at<=?) OR "
        "(s.job_id IS NULL AND s.tail_cost+COALESCE(length(CAST(p.ai_text AS BLOB)),-1)>?) "
        "ORDER BY COALESCE(s.job_id,s.previous_id,0),s.covered,s.conversation_id LIMIT 1",
        (now, policy.delivery_tokens),
    ).fetchone()
    if state is None:
        return None
    if state["job_id"] is not None:
        return state["job_id"], state["rule_text"], state["attempts"]
    room = state["conversation_id"]
    records = db.execute(
        "SELECT i.seq,i.user_id,i.occurred_at,m.perception_line FROM activity_deliveries d "
        "JOIN timeline_batch_members m ON m.activity_seq=d.activity_seq JOIN activity_items i "
        "ON i.seq=d.activity_seq WHERE d.conversation_id=? AND d.applied=0 AND d.activity_seq>? "
        "AND m.perception_line!='' ORDER BY d.activity_seq",
        (room, state["covered"]),
    )
    lines = []
    consumed = 0
    for row in records:
        lines.append(row)
        consumed += estimate(row["perception_line"]) + 1
        if consumed >= max(16_384, policy.delivery_tokens):
            break
    through = lines[-1]["seq"] if lines else state["covered"]
    lines.sort(key=lambda r: (r["occurred_at"], r["seq"]))
    text = "\n".join(r["perception_line"] for r in lines)
    start = lines[0]["occurred_at"] if lines else state["previous_start"]
    end = lines[-1]["occurred_at"] if lines else state["previous_end"]
    if state["previous"]:
        # A lower budget after restart may require recompressing the checkpoint alone.
        prior = f"已有摘要：\n{state['previous']}"
        text = f"{prior}\n新增观测（按发生时间）：\n{text}" if lines else prior
        start = min(start, state["previous_start"])
        end = max(end, state["previous_end"])
    batch = db.execute(
        "INSERT INTO timeline_batches(user_id,scope,terminal_id,day,first_at,last_at,last_known,"
        "rule_text,sealed) VALUES(?,?,'',?,?,?,?,?,1)",
        (
            lines[0]["user_id"] if lines else state["user_id"],
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
        "INSERT INTO perception_rounds VALUES(?,?,?,?,?)",
        (batch, room, state["covered"], through, consumed),
    )
    db.execute("UPDATE perception_buffers SET job_id=? WHERE conversation_id=?", (batch, room))
    return batch, text, 0


def finish(db: sqlite3.Connection, batch: int, text: str, policy: TimelinePolicy) -> bool:
    job = db.execute("SELECT * FROM perception_rounds WHERE batch_id=?", (batch,)).fetchone()
    if job is None:
        return False
    db.execute(
        "UPDATE timeline_batches SET ai_text=? WHERE batch_id=? AND ai_text IS NULL",
        (clip(text, output_budget(policy)), batch),
    )
    # A read discards this generation. Late/repeated completions cannot move another cursor.
    db.execute(
        "UPDATE perception_buffers SET previous_id=?,covered=?,tail_cost=tail_cost-?,"
        "job_id=NULL WHERE conversation_id=? AND job_id=?",
        (batch, job["through_seq"], job["consumed"], job["conversation_id"], batch),
    )
    return True
