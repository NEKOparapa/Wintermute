"""Durable, per-device notification inbox and immutable file snapshots."""

import re
import sqlite3
from dataclasses import dataclass
from typing import TypedDict
from uuid import uuid4

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
    created_at: str
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
        "CREATE TABLE IF NOT EXISTS notification_deliveries (terminal_id TEXT NOT NULL "
        "REFERENCES terminals,notification_seq INTEGER NOT NULL REFERENCES user_notifications,"
        "received_at TEXT,read_at TEXT, PRIMARY KEY(terminal_id,notification_seq))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS notification_pending ON notification_deliveries"
        "(terminal_id,notification_seq) WHERE received_at IS NULL"
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
) -> str:
    duplicate = previous(db, user, conversation, key, fingerprint)
    if duplicate is not None:
        return duplicate
    if expires and expires <= now:
        raise ValueError("通知的过期时间已过")
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("请选择一个或多个不同的接收终端")
    if (
        len(files) > MAX_FILES
        or any(len(f.data) > MAX_FILE_BYTES for f in files)
        or (sum(len(f.data) for f in files) > MAX_TOTAL_BYTES)
    ):
        raise ValueError("附件超过通知大小上限")
    notification_id = "ntf_" + uuid4().hex
    seq = db.execute(
        "INSERT INTO user_notifications(id,user_id,conversation_id,title,body,created_at,"
        "expires_at,request_key,fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
        (notification_id, user, conversation, title, body, now, expires, key, fingerprint),
    ).lastrowid
    db.executemany(
        "INSERT INTO notification_deliveries(terminal_id,notification_seq) VALUES (?,?)",
        [(target, seq) for target in targets],
    )
    for file in files:
        db.execute(
            "INSERT INTO notification_files(id,notification_seq,name,content_type,size_bytes,data)"
            " VALUES(?,?,?,?,?,?)",
            ("nf_" + uuid4().hex, seq, file.name, file.content_type, len(file.data), file.data),
        )
    return notification_id


_SCOPED = (
    " FROM notification_deliveries d JOIN user_notifications n ON n.seq=d.notification_seq "
    "WHERE d.terminal_id=? AND n.user_id=? AND n.conversation_id=? "
)


def _file_info(row: sqlite3.Row) -> AttachmentInfo:
    return AttachmentInfo(
        id=row["id"],
        name=row["name"],
        size_bytes=row["size_bytes"],
        content_type=row["content_type"],
    )


def _item(db: sqlite3.Connection, row: sqlite3.Row, now: str) -> NotificationItem:
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
        expires_at=row["expires_at"],
        expired=row["expires_at"] is not None and row["expires_at"] <= now,
        received_at=row["received_at"],
        read_at=row["read_at"],
        attachments=[_file_info(f) for f in files],
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
        "read_at=COALESCE(read_at,?) WHERE terminal_id=? AND notification_seq=?",
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
