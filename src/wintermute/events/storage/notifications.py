"""Durable, per-device notification inbox and immutable file snapshots."""

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict
from uuid import uuid4

from wintermute.events.core.schedule import ScheduleEntry
from wintermute.events.storage.schedules import stamp

MAX_FILES = 4
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class FileSnapshot:
    name: str
    content_type: str
    data: bytes


class AttachmentInfo(TypedDict):
    id: str
    name: str
    size_bytes: int
    content_type: str


class NotificationItem(TypedDict):
    id: str
    title: str
    body: str
    created_at: str | None
    notify_at: str
    targets: list[str]
    status: str
    in_inbox: bool
    expires_at: str | None
    expired: bool
    received_at: str | None
    read_at: str | None
    attachments: list[AttachmentInfo]


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS user_notifications (seq INTEGER PRIMARY KEY,"
        "id TEXT NOT NULL UNIQUE,user_id TEXT NOT NULL,conversation_id TEXT NOT NULL "
        "REFERENCES conversations,title TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,"
        "expires_at TEXT,request_key TEXT NOT NULL,fingerprint TEXT NOT NULL,"
        "UNIQUE(user_id,conversation_id,request_key))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS notification_deliveries (device_id TEXT NOT NULL "
        "REFERENCES devices,notification_seq INTEGER NOT NULL REFERENCES user_notifications,"
        "received_at TEXT,read_at TEXT, PRIMARY KEY(device_id,notification_seq))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS notification_pending ON notification_deliveries"
        "(device_id,notification_seq) WHERE received_at IS NULL"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS notification_files (seq INTEGER PRIMARY KEY,"
        "id TEXT UNIQUE NOT NULL,notification_seq INTEGER REFERENCES user_notifications NOT NULL,"
        "name TEXT NOT NULL,content_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,"
        "data BLOB NOT NULL)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS notification_files_parent "
        "ON notification_files(notification_seq)"
    )


def add_notification_time(db: sqlite3.Connection) -> None:
    if "notify_at" in {r[1] for r in db.execute("PRAGMA table_info(user_notifications)")}:
        return
    db.execute("ALTER TABLE user_notifications ADD COLUMN notify_at TEXT")
    db.execute(
        "CREATE INDEX notification_calendar ON user_notifications"
        "(user_id,conversation_id,coalesce(notify_at,created_at),id)"
    )
    db.execute(
        "CREATE INDEX notification_target_devices ON notification_deliveries"
        "(notification_seq,device_id)"
    )
    # Result events also retain the planned time after a triggered calendar entry is deleted.
    for id, instant in db.execute(
        "SELECT json_extract(data,'$.payload.notification_id'),"
        "json_extract(data,'$.payload.notify_at') "
        "FROM notification_events WHERE json_extract(data,'$.type')='device_notification.result' "
        "AND json_extract(data,'$.device_id') GLOB 'wintermute:internal:schedule:*'"
    ):
        db.execute(
            "UPDATE user_notifications SET notify_at=? WHERE id=?",
            (stamp(datetime.fromisoformat(instant)), id),
        )


def previous(
    db: sqlite3.Connection, user: str, conversation: str, key: str, fingerprint: str
) -> str | None:
    row = db.execute(
        "SELECT id,fingerprint FROM user_notifications WHERE user_id=? AND conversation_id=? "
        "AND request_key=?",
        (user, conversation, key),
    ).fetchone()
    if row is None:
        return None
    if row["fingerprint"] != fingerprint:
        raise ValueError("工具调用 ID 已用于另一条通知")
    return str(row["id"])


def insert(
    db: sqlite3.Connection,
    user: str,
    conversation: str,
    targets: tuple[str, ...],
    title: str,
    body: str,
    now: str,
    expires: str | None,
    files: list[FileSnapshot],
    key: str,
    fingerprint: str,
    *,
    notification_id: str | None = None,
    notify_at: str | None = None,
) -> str:
    duplicate = previous(db, user, conversation, key, fingerprint)
    if duplicate is not None:
        return duplicate
    if expires and expires <= now:
        raise ValueError("通知的过期时间已过")
    validate_delivery(targets, files)
    notification_id = notification_id or "ntf_" + uuid4().hex
    seq = db.execute(
        "INSERT INTO user_notifications(id,user_id,conversation_id,title,body,created_at,"
        "expires_at,request_key,fingerprint,notify_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            notification_id,
            user,
            conversation,
            title,
            body,
            now,
            expires,
            key,
            fingerprint,
            notify_at,
        ),
    ).lastrowid
    db.executemany(
        "INSERT INTO notification_deliveries(device_id,notification_seq) VALUES (?,?)",
        [(target, seq) for target in targets],
    )
    for file in files:
        db.execute(
            "INSERT INTO notification_files(id,notification_seq,name,content_type,size_bytes,data)"
            " VALUES(?,?,?,?,?,?)",
            ("nf_" + uuid4().hex, seq, file.name, file.content_type, len(file.data), file.data),
        )
    return notification_id


def validate_delivery(targets: Sequence[str], files: Sequence[FileSnapshot]) -> None:
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("请选择一个或多个不同的接收设备")
    if (
        len(files) > MAX_FILES
        or any(len(f.data) > MAX_FILE_BYTES for f in files)
        or (sum(len(f.data) for f in files) > MAX_TOTAL_BYTES)
    ):
        raise ValueError("附件超过通知大小上限")


_SCOPED = (
    " FROM notification_deliveries d JOIN user_notifications n ON n.seq=d.notification_seq "
    "WHERE d.device_id=? AND n.user_id=? AND n.conversation_id=? "
)


def _file_info(row: sqlite3.Row) -> AttachmentInfo:
    return AttachmentInfo(
        id=row["id"],
        name=row["name"],
        size_bytes=row["size_bytes"],
        content_type=row["content_type"],
    )


def _item(db: sqlite3.Connection, row: sqlite3.Row, now: str) -> NotificationItem:
    plan = db.execute(
        "SELECT targets,status FROM scheduled_notifications WHERE id=?", (row["id"],)
    ).fetchone()
    targets = (
        json.loads(plan["targets"])
        if plan
        else [
            target[0]
            for target in db.execute(
                "SELECT device_id FROM notification_deliveries WHERE notification_seq=? "
                "ORDER BY device_id",
                (row["seq"],),
            )
        ]
    )
    files = db.execute(
        "SELECT id,name,size_bytes,content_type FROM notification_files "
        "WHERE notification_seq=? ORDER BY seq",
        (row["seq"],),
    )
    return NotificationItem(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        created_at=row["created_at"],
        notify_at=row["notify_at"] or row["created_at"],
        targets=targets,
        status=plan["status"] if plan else "sent",
        in_inbox=True,
        expires_at=row["expires_at"],
        expired=row["expires_at"] is not None and row["expires_at"] <= now,
        received_at=row["received_at"],
        read_at=row["read_at"],
        attachments=[_file_info(f) for f in files],
    )


def calendar_item(
    db: sqlite3.Connection, scope: tuple[str, str, str], entry: ScheduleEntry, now: str
) -> NotificationItem:
    plan = db.execute(
        "SELECT id,targets,status,expires_at FROM scheduled_notifications "
        "WHERE schedule_id=? AND user_id=? AND conversation_id=?",
        (entry.id, *scope[1:]),
    ).fetchone()
    id = plan["id"] if plan else entry.id
    delivered = db.execute(
        "SELECT n.*,d.read_at,d.received_at" + _SCOPED + "AND n.id=?", (*scope, id)
    ).fetchone()
    if delivered:
        return _item(db, delivered, now)
    if plan is None:
        raise LookupError("找不到这条设备通知")
    return NotificationItem(
        id=id,
        title=entry.title,
        body=entry.content,
        created_at=None,
        notify_at=stamp(entry.trigger_at),
        targets=json.loads(plan["targets"]),
        status=plan["status"],
        in_inbox=False,
        received_at=None,
        read_at=None,
        expires_at=plan["expires_at"],
        expired=bool(plan["expires_at"] and plan["expires_at"] <= now),
        attachments=[],
    )


def page(
    db: sqlite3.Connection,
    scope: tuple[str, str, str],
    now: str,
    limit: int,
    cursor: str | None,
    unread: bool,
    pending: bool,
) -> tuple[list[NotificationItem], str | None]:
    if type(limit) is not int or not 1 <= limit <= 100 or (unread and pending):
        raise ValueError("Invalid notification list parameters")
    sql = "SELECT n.*,d.read_at,d.received_at" + _SCOPED
    params: list[str | int] = list(scope)
    if cursor is not None:
        if not re.fullmatch("[1-9][0-9]{0,18}", cursor) or int(cursor) > 2**63 - 1:
            raise ValueError("Invalid notification cursor")
        sql += "AND d.notification_seq<? "
        params.append(int(cursor))
    if pending:
        sql += "AND d.received_at IS NULL "
    if unread:
        sql += "AND d.read_at IS NULL AND (n.expires_at IS NULL OR n.expires_at>?) "
        params.append(now)
    rows = db.execute(
        sql + "ORDER BY d.notification_seq DESC LIMIT ?", (*params, limit + 1)
    ).fetchall()
    return (
        [_item(db, row, now) for row in rows[:limit]],
        str(rows[limit - 1]["seq"]) if len(rows) > limit else None,
    )


def acknowledge(
    db: sqlite3.Connection, scope: tuple[str, str, str], id: str, now: str, read: bool
) -> NotificationItem:
    select = "SELECT n.*,d.read_at,d.received_at" + _SCOPED + " AND n.id=?"
    row = db.execute(select, (*scope, id)).fetchone()
    if row is None:
        raise LookupError("通知不存在")
    db.execute(
        "UPDATE notification_deliveries SET received_at=COALESCE(received_at,?), "
        "read_at=COALESCE(read_at,?) WHERE device_id=? AND notification_seq=?",
        (now, now if read else None, scope[0], row["seq"]),
    )
    return _item(db, db.execute(select, (*scope, id)).fetchone(), now)


def file(
    db: sqlite3.Connection, scope: tuple[str, str, str], notification_id: str, file_id: str
) -> tuple[AttachmentInfo, int]:
    row = db.execute(
        "SELECT seq,id,name,size_bytes,content_type FROM notification_files "
        "WHERE id=? AND notification_seq=(SELECT n.seq" + _SCOPED + "AND n.id=?)",
        (file_id, *scope, notification_id),
    ).fetchone()
    if row is None:
        raise LookupError("附件不存在")
    return _file_info(row), row["seq"]
