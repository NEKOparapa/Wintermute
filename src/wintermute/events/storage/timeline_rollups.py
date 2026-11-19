"""Background summaries of consecutive sealed batches from one device and local day."""

import json
import sqlite3
from datetime import UTC, datetime

from wintermute.events.core.timeline_compression import (
    BatchText,
    TimelinePolicy,
    estimate,
    group_rules,
    merge_stats,
)

# Leaves below this count remain individually readable. This also bounds each merge job.
_LEAVES_PER_GROUP = 6


def initialize_rollups(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS timeline_rollups(rollup_id INTEGER PRIMARY KEY,"
        "user_id TEXT NOT NULL,scope TEXT NOT NULL,device_id TEXT NOT NULL,day TEXT NOT NULL,"
        "sealed INTEGER NOT NULL DEFAULT 0,rule_text TEXT NOT NULL DEFAULT '',"
        "rule_tokens INTEGER NOT NULL DEFAULT 0,ai_text TEXT,attempts INTEGER NOT NULL DEFAULT 0,"
        "retry_at REAL NOT NULL DEFAULT 0)"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS timeline_rollup_open "
        "ON timeline_rollups(user_id,scope,device_id,day) WHERE sealed=0"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS timeline_rollup_pending "
        "ON timeline_rollups(rule_tokens,retry_at) "
        "WHERE sealed=1 AND ai_text IS NULL"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS timeline_rollup_members("
        "batch_id INTEGER PRIMARY KEY REFERENCES timeline_batches(batch_id),"
        "rollup_id INTEGER NOT NULL REFERENCES timeline_rollups(rollup_id))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS timeline_rollup_contents ON timeline_rollup_members(rollup_id)"
    )


def add_sealed_batch(db: sqlite3.Connection, batch_id: int, policy: TimelinePolicy) -> None:
    leaf = db.execute(
        "SELECT user_id,scope,device_id,day FROM timeline_batches WHERE batch_id=? AND sealed=1",
        (batch_id,),
    ).fetchone()
    if (
        leaf is None
        or db.execute(
            "SELECT 1 FROM timeline_rollup_members WHERE batch_id=?", (batch_id,)
        ).fetchone()
    ):
        return
    scope = tuple(leaf)
    parent = db.execute(
        "SELECT rollup_id FROM timeline_rollups "
        "WHERE user_id=? AND scope=? AND device_id=? AND day=? AND sealed=0",
        scope,
    ).fetchone()
    if parent is None:
        group = db.execute(
            "INSERT INTO timeline_rollups(user_id,scope,device_id,day) VALUES(?,?,?,?)",
            scope,
        ).lastrowid
    else:
        group = parent[0]
    db.execute("INSERT INTO timeline_rollup_members VALUES(?,?)", (batch_id, group))
    count = db.execute(
        "SELECT count(*) FROM timeline_rollup_members WHERE rollup_id=?", (group,)
    ).fetchone()[0]
    if count < _LEAVES_PER_GROUP:
        return
    rows = db.execute(
        "SELECT b.* FROM timeline_batches b JOIN timeline_rollup_members m USING(batch_id) "
        "WHERE m.rollup_id=? ORDER BY b.first_at,b.last_at,b.batch_id",
        (group,),
    ).fetchall()
    leaves = [
        BatchText(
            datetime.fromtimestamp(r["first_at"], UTC),
            datetime.fromtimestamp(r["last_at"], UTC),
            r["item_count"],
            r["rule_text"],
            stats_text=r["stats_text"],
        )
        for r in rows
    ]
    rules = group_rules(leaves, policy)
    rule_cost = estimate(rules)
    if all(row["screenshot_fact_bytes"] is not None for row in rows):
        rule_cost = sum(row["screenshot_fact_bytes"] for row in rows)
    db.execute(
        "UPDATE timeline_rollups SET sealed=1,rule_text=?,rule_tokens=? WHERE rollup_id=?",
        (rules, rule_cost, group),
    )


def next_rollup(
    db: sqlite3.Connection,
    policy: TimelinePolicy,
    now: float,
) -> tuple[int, str, int] | None:
    row = db.execute(
        "SELECT rollup_id,rule_text,attempts FROM timeline_rollups WHERE sealed=1 "
        "AND ai_text IS NULL AND rule_tokens>? AND retry_at<=? ORDER BY retry_at,rollup_id LIMIT 1",
        (policy.ai_threshold, now),
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def finish_rollup(db: sqlite3.Connection, rollup_id: int, text: str) -> None:
    db.execute(
        "UPDATE timeline_rollups SET ai_text=? WHERE rollup_id=? AND sealed=1 AND ai_text IS NULL",
        (text, rollup_id),
    )


def retry_rollup(db: sqlite3.Connection, rollup_id: int, retry_at: float) -> None:
    db.execute(
        "UPDATE timeline_rollups SET attempts=attempts+1,retry_at=? "
        "WHERE rollup_id=? AND sealed=1 AND ai_text IS NULL",
        (retry_at, rollup_id),
    )


def collapse_rollups(
    db: sqlite3.Connection,
    pieces: list[tuple[int | None, BatchText]],
    policy: TimelinePolicy,
) -> list[BatchText]:
    """Reuse a parent only if this conversation can see every leaf contiguously and in full."""
    ids = [key for key, _ in pieces if key is not None]
    if len(ids) < _LEAVES_PER_GROUP:
        return [block for _, block in pieces]
    parents: dict[int, tuple[int, str | None, str]] = {}
    for row in db.execute(
        "SELECT m.batch_id,m.rollup_id,p.ai_text,"
        "CASE WHEN p.ai_text IS NULL THEN substr(p.rule_text,1,?) "
        "ELSE '' END FROM timeline_rollup_members m JOIN timeline_rollups p USING(rollup_id) "
        "WHERE p.sealed=1 AND (p.ai_text IS NOT NULL OR p.rule_tokens<=?) "
        "AND m.batch_id IN (SELECT value FROM json_each(?))",
        (policy.delivery_tokens + 1, policy.ai_threshold, json.dumps(ids)),
    ):
        parents[row[0]] = (row[1], row[2], row[3])
    result: list[BatchText] = []
    i = 0
    while i < len(pieces):
        key, block = pieces[i]
        parent = parents.get(key) if key is not None else None
        group = pieces[i : i + _LEAVES_PER_GROUP] if parent else []
        if len(group) == _LEAVES_PER_GROUP and all(
            k is not None and parents.get(k) == parent for k, _ in group
        ):
            assert parent is not None
            leaves = [leaf for _, leaf in group]
            result.append(
                BatchText(
                    start=min(b.start for _, b in group),
                    end=max(b.end for _, b in group),
                    count=sum(b.count for _, b in group),
                    rule_text=parent[2],
                    ai_text=parent[1],
                    stats_text=merge_stats(leaves),
                    device_id=block.device_id,
                )
            )
            i += _LEAVES_PER_GROUP
        else:
            result.append(block)
            i += 1
    return result
