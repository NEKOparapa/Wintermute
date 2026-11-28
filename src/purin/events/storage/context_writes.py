"""Immutable entries for timeline messages actually committed to the main Agent's context."""

import json
import sqlite3
from datetime import datetime

from purin.agent import UserMessage
from purin.events.core.presentation import timestamp_text
from purin.events.storage.merged_timeline import SPAN_COLUMNS, activity_span


def save(
    db: sqlite3.Connection,
    room: str,
    history_seq: int,
    slot: int,
    written: datetime,
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
            round(written.timestamp() * 1_000_000),
            message.model_text(),
            timestamp_text(min(start for start, _ in spans)) if spans else None,
            timestamp_text(max(end for _, end in spans)) if spans else None,
        ),
    )
