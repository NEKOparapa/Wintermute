"""Persist device notification requests until their calendar entry is dispatched."""

import json
import sqlite3
from collections.abc import Callable, Collection, Sequence
from datetime import datetime
from typing import TypedDict
from uuid import uuid4

from purin.events.core.schedule import ScheduleEntry, due_notification
from purin.events.core.types import EventInput
from purin.events.storage import notifications, schedules

type NotificationTargets = Callable[[str, str], Collection[str]]

UNBOUND = "设备已解绑或不属于当前会话"
EXPIRED = "通知已过期"
INBOX_ERROR = "写入设备收件箱失败"


class NotificationReceipt(TypedDict):
    id: str
    schedule_id: str | None
    notify_at: str | None
    status: str


def previous(
    db: sqlite3.Connection, user: str, conversation: str, key: str, fingerprint: str
) -> NotificationReceipt | None:
    row = db.execute(
        "SELECT n.id,n.schedule_id,n.status,n.fingerprint,s.trigger_at "
        "FROM scheduled_notifications n LEFT JOIN schedules s ON s.id=n.schedule_id "
        "WHERE n.user_id=? AND n.conversation_id=? AND n.request_key=?",
        (user, conversation, key),
    ).fetchone()
    if row is None:
        return None
    if row["fingerprint"] != fingerprint:
        raise ValueError("工具调用 ID 已用于另一条通知")
    status = row["status"]
    if status == "scheduled" and row["schedule_id"] is None:
        status = "cancelled"
    return NotificationReceipt(
        id=row["id"], schedule_id=row["schedule_id"], notify_at=row["trigger_at"], status=status
    )


def save(
    db: sqlite3.Connection,
    entry: ScheduleEntry,
    user: str,
    conversation: str,
    key: str,
    fingerprint: str,
    targets: Sequence[str],
    expires_at: datetime | None,
    files: Sequence[notifications.FileSnapshot],
) -> NotificationReceipt:
    notifications.validate_delivery(targets, files)
    id = "ntf_" + uuid4().hex
    db.execute(
        "INSERT INTO scheduled_notifications(id,schedule_id,user_id,conversation_id,request_key,"
        "fingerprint,targets,expires_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            id,
            entry.id,
            user,
            conversation,
            key,
            fingerprint,
            json.dumps(targets),
            schedules.stamp(expires_at) if expires_at else None,
        ),
    )
    db.executemany(
        "INSERT INTO scheduled_notification_files(schedule_id,name,content_type,data) "
        "VALUES(?,?,?,?)",
        ((entry.id, f.name, f.content_type, f.data) for f in files),
    )
    return NotificationReceipt(
        id=id, schedule_id=entry.id, notify_at=schedules.stamp(entry.trigger_at), status="scheduled"
    )


def validate_plan(
    db: sqlite3.Connection,
    entry: ScheduleEntry,
    user: str,
    conversation: str,
    check_event_size: Callable[[EventInput], None],
) -> None:
    row = db.execute(
        "SELECT id,targets,expires_at FROM scheduled_notifications WHERE schedule_id=?", (entry.id,)
    ).fetchone()
    if row["expires_at"] and schedules.stamp(entry.trigger_at) >= row["expires_at"]:
        raise ValueError("通知时间必须早于过期时间")
    if len(entry.content) > 4000:
        raise ValueError("通知正文最多 4000 字")
    if (
        not entry.content.strip()
        and not db.execute(
            "SELECT 1 FROM scheduled_notification_files WHERE schedule_id=? LIMIT 1", (entry.id,)
        ).fetchone()
    ):
        raise ValueError("通知需要正文或附件")
    # Every recipient appears exactly once in the final receipt. Reserve room for any
    # combination of outcomes before accepting a plan, including after a calendar edit.
    check_event_size(
        _result_event(
            entry,
            user,
            conversation,
            entry.trigger_at.replace(microsecond=999999),
            row["id"],
            "partial",
            [],
            {UNBOUND: json.loads(row["targets"]), EXPIRED: [], INBOX_ERROR: []},
        )
    )


def _result_event(
    entry: ScheduleEntry,
    user: str,
    conversation: str,
    now: datetime,
    notification_id: str,
    status: str,
    sent: list[str],
    failures: dict[str, list[str]],
) -> EventInput:
    return due_notification(entry, user, conversation).model_copy(
        update={
            "type": "device_notification.result",
            "occurred_at": now,
            "payload": {
                "title": entry.title,
                "notification_id": notification_id,
                "schedule_id": entry.id,
                "notify_at": entry.trigger_at.isoformat(),
                "status": status,
                "sent": sent,
                "failed": failures,
            },
        }
    )


def dispatch(
    db: sqlite3.Connection,
    entry: ScheduleEntry,
    user: str,
    conversation: str,
    now: datetime,
    current_targets: NotificationTargets,
) -> EventInput:
    plan = db.execute(
        "SELECT * FROM scheduled_notifications WHERE schedule_id=?", (entry.id,)
    ).fetchone()
    targets: list[str] = json.loads(plan["targets"])
    now_text = schedules.stamp(now)
    failures: dict[str, list[str]] = {}
    sent: list[str] = []
    if plan["expires_at"] and plan["expires_at"] <= now_text:
        failures[EXPIRED] = targets
    else:
        eligible = current_targets(user, conversation)
        sent = [target for target in targets if target in eligible]
        if unbound := [target for target in targets if target not in eligible]:
            failures[UNBOUND] = unbound

    if sent:
        db.execute("SAVEPOINT device_inbox")
        try:
            inbox_id = notifications.insert(
                db,
                user,
                conversation,
                tuple(sent),
                entry.title,
                entry.content,
                now_text,
                plan["expires_at"],
                [],
                plan["request_key"],
                plan["fingerprint"],
                notification_id=plan["id"],
                notify_at=schedules.stamp(entry.trigger_at),
            )
            if inbox_id != plan["id"]:
                raise ValueError("调用已关联到其他通知")
            db.execute(
                "INSERT INTO notification_files(id,notification_seq,name,content_type,"
                "size_bytes,data) SELECT 'nf_' || lower(hex(randomblob(16))),n.seq,f.name,"
                "f.content_type,length(f.data),f.data FROM scheduled_notification_files f "
                "JOIN user_notifications n ON n.id=? WHERE f.schedule_id=? ORDER BY f.seq",
                (plan["id"], entry.id),
            )
        except (sqlite3.DatabaseError, ValueError):
            if not db.in_transaction:
                raise
            db.execute("ROLLBACK TO device_inbox")
            failures[INBOX_ERROR], sent = sent, []
        db.execute("RELEASE device_inbox")

    status = "partial" if sent and failures else "sent" if sent else "failed"
    db.execute("UPDATE scheduled_notifications SET status=? WHERE id=?", (status, plan["id"]))
    db.execute("DELETE FROM scheduled_notification_files WHERE schedule_id=?", (entry.id,))
    return _result_event(entry, user, conversation, now, plan["id"], status, sent, failures)
