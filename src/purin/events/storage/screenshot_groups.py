"""Persistence for periodic screenshot observations and the last recognized image in a group."""

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from purin.events.core.slices import SliceStreamItem
from purin.events.core.types import EventRecord, ScreenshotContext

if TYPE_CHECKING:
    from purin.events.slices.image import ScreenshotObservation, ScreenshotReference


@dataclass(frozen=True)
class PreviousScreenshot:
    group_id: str
    last_event: str
    reference: "ScreenshotReference"


def record_existing_item(db: sqlite3.Connection, event: EventRecord, item: SliceStreamItem) -> None:
    """Project an imported or explicitly revised summary without issuing a vision request."""
    existing = db.execute(
        "SELECT group_id,fact FROM screenshot_frames WHERE event_id=?", (event.event_id,)
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE screenshot_frames SET item_id=?,fact=? WHERE event_id=?",
            (item.item_id, item.text, event.event_id),
        )
        db.execute(
            "UPDATE screenshot_groups SET change_count=change_count+?,"
            "match_digest_only=1,facts='[]',"
            "latest_fact=(SELECT substr(f.fact,1,240) FROM screenshot_frames f "
            "WHERE f.group_id=screenshot_groups.group_id AND f.fact<>'' "
            "ORDER BY f.occurred_at DESC,f.rowid DESC LIMIT 1) WHERE group_id=?",
            (int(not existing["fact"]), existing["group_id"]),
        )
        return
    capture = ScreenshotContext.model_validate(event.payload["capture"])
    previous = _preceding(db, event, capture, item.day)
    digest = str(event.payload["sha256"])
    group = previous if previous and previous["digest"] == digest else None
    subject = capture.title or capture.app or ""
    fact = item.text if group is None else ""
    _save_frame(
        db,
        event,
        capture,
        item.day,
        subject,
        fact,
        digest,
        [],
        [],
        group,
        item.item_id,
        match_digest_only=True,
    )


def _key(capture: ScreenshotContext) -> str:
    return capture.model_dump_json(exclude_none=True)


def _preceding(
    db: sqlite3.Connection, event: EventRecord, capture: ScreenshotContext, day: str
) -> sqlite3.Row | None:
    # Look up the immediately preceding raw capture even when its processing failed.
    # An intervening app switch must end the group even if its pixels cannot be analyzed.
    row = db.execute(
        "SELECT g.* FROM (SELECT e.event_id FROM slice_events e "
        "WHERE e.user_id=? AND IFNULL(e.conversation_id,'')=? "
        "AND json_extract(e.data,'$.device_id')=? AND json_extract(e.data,'$.source_id')=? "
        "AND json_type(e.data,'$.payload.capture')='object' AND e.seq<"
        "(SELECT seq FROM slice_events WHERE event_id=?) ORDER BY e.seq DESC LIMIT 1) prior "
        "JOIN screenshot_frames f ON f.event_id=prior.event_id JOIN screenshot_groups g "
        "ON g.group_id=f.group_id AND g.last_event=f.event_id",
        (
            event.user_id,
            event.conversation_id or "",
            event.device_id,
            event.source_id,
            event.event_id,
        ),
    ).fetchone()
    if not row or row["context_key"] != _key(capture) or row["day"] != day:
        return None
    # Twice the longest client sampling interval allows ordinary timer drift.
    gap = event.occurred_at.timestamp() - row["last_at"]
    return row if 0 <= gap <= 1800 else None


def previous_capture(
    db: sqlite3.Connection, event: EventRecord, day: str
) -> PreviousScreenshot | None:
    from purin.events.slices.image import ScreenshotReference

    capture = ScreenshotContext.model_validate(event.payload["capture"])
    row = _preceding(db, event, capture, day)
    if not row or row["match_digest_only"] and row["digest"] != event.payload["sha256"]:
        return None
    memory = json.loads(row["facts"])
    if isinstance(memory, list):
        memory = {"facts": memory, "evidence_keys": []}
    return PreviousScreenshot(
        row["group_id"],
        row["last_event"],
        ScreenshotReference(
            row["digest"],
            row["subject"],
            tuple(memory["facts"]),
            tuple(tuple(rect) for rect in json.loads(row["regions"])),
            evidence_keys=tuple(memory["evidence_keys"]),
        ),
    )


def save_capture(
    db: sqlite3.Connection,
    event: EventRecord,
    day: str,
    prior: PreviousScreenshot | None,
    obs: "ScreenshotObservation",
    item_id: str | None,
) -> None:
    capture = ScreenshotContext.model_validate(event.payload["capture"])
    group = None
    if prior and not obs.changed_subject:
        group = db.execute(
            "SELECT * FROM screenshot_groups WHERE group_id=?", (prior.group_id,)
        ).fetchone()
    digest = str(event.payload["sha256"]) if obs.recognized or not group else group["digest"]
    reference = prior.reference if group is not None and prior is not None else None
    recent = (*reference.facts, *obs.facts) if reference else obs.facts
    keys = (*reference.evidence_keys, *obs.evidence_keys) if reference else obs.evidence_keys
    _save_frame(
        db,
        event,
        capture,
        day,
        obs.subject,
        "\n".join(obs.facts),
        digest,
        {"facts": list(recent[-9:]), "evidence_keys": list(dict.fromkeys(keys))[-24:]},
        list(obs.noise_regions),
        group,
        item_id,
        match_digest_only=bool(group and group["match_digest_only"] and not obs.recognized),
    )


def _save_frame(
    db: sqlite3.Connection,
    event: EventRecord,
    capture: ScreenshotContext,
    day: str,
    subject: str,
    fact: str,
    digest: str,
    facts: dict[str, list[str]] | list[str],
    regions: list[tuple[int, int, int, int]],
    group: sqlite3.Row | None,
    item_id: str | None,
    *,
    match_digest_only: bool,
) -> None:
    at = event.occurred_at.timestamp()
    group_id = group["group_id"] if group else event.event_id
    # Keep the previous human-readable progress when the latest frame contributes no fact.
    latest = fact[:240] if fact else (group["latest_fact"] if group else "")
    if group is None:
        db.execute(
            "INSERT INTO screenshot_groups(group_id,user_id,conversation_scope,device_id,"
            "source_id,"
            "platform,app,title,context_key,day,subject,started_at,last_at,frame_count,change_count,"
            "latest_fact,last_event,digest,facts,regions,match_digest_only) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
            (
                group_id,
                event.user_id,
                event.conversation_id or "",
                event.device_id,
                event.source_id,
                capture.platform,
                capture.app or "",
                capture.title or "",
                _key(capture),
                day,
                subject,
                at,
                at,
                int(bool(fact)),
                latest,
                event.event_id,
                digest,
                json.dumps(facts),
                json.dumps(regions),
                match_digest_only,
            ),
        )
    else:
        db.execute(
            "UPDATE screenshot_groups SET subject=?,last_at=?,frame_count=frame_count+1,"
            "change_count=change_count+?,latest_fact=?,last_event=?,digest=?,"
            "facts=?,regions=?,match_digest_only=? "
            "WHERE group_id=?",
            (
                subject,
                at,
                int(bool(fact)),
                latest,
                event.event_id,
                digest,
                json.dumps(facts),
                json.dumps(regions),
                match_digest_only,
                group_id,
            ),
        )
    db.execute(
        "INSERT INTO screenshot_frames(event_id,group_id,item_id,occurred_at,fact) "
        "VALUES(?,?,?,?,?)",
        (event.event_id, group_id, item_id, at, fact),
    )
