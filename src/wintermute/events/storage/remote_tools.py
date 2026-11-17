"""Durable, at-most-once dispatch of commands to conversation-bound terminals."""

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, JsonValue

from wintermute.events.core.types import EventError, EventInput, Identifier, Terminal, _Record

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore

type ClientStatus = Literal["succeeded", "failed", "timed_out"]
type CallStatus = Literal["queued", "running", "succeeded", "failed", "timed_out", "expired"]
_QUEUE_SECONDS = 15
_RECEIPT_GRACE_SECONDS = 30


class RemoteCallConflict(ValueError):
    """An invocation or its receipt conflicts with the stored operation."""


class _Request(_Record):
    id: Identifier
    tool: Identifier
    arguments: dict[str, JsonValue]
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=120)]


class RemoteClaim(_Request):
    claim_token: str = Field(repr=False)


class RemoteCall(_Request):
    terminal_id: str
    status: CallStatus
    created_at: AwareDatetime
    started_at: AwareDatetime | None
    completed_at: AwareDatetime | None
    stdout: str
    stderr: str
    exit_code: int | None
    truncated: bool


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS remote_tool_calls ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, "
        "user_id TEXT NOT NULL, conversation_id TEXT NOT NULL REFERENCES conversations, "
        "terminal_id TEXT NOT NULL REFERENCES terminals,"
        "reply_terminal_id TEXT REFERENCES terminals,"
        "run_id TEXT NOT NULL, call_id TEXT NOT NULL, tool TEXT NOT NULL, "
        "arguments TEXT NOT NULL, timeout_seconds INTEGER NOT NULL, status TEXT NOT NULL, "
        "created_at REAL NOT NULL, deadline REAL, started_at REAL, completed_at REAL, "
        "claim_hash BLOB, stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '', "
        "exit_code INTEGER, truncated INTEGER NOT NULL DEFAULT 0, "
        "UNIQUE(user_id,conversation_id,run_id,call_id))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS remote_tool_queue ON remote_tool_calls "
        "(terminal_id,user_id,conversation_id,seq) WHERE status='queued'"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS remote_tool_deadlines ON remote_tool_calls(deadline) "
        "WHERE deadline IS NOT NULL"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS remote_tool_history ON remote_tool_calls "
        "(user_id,conversation_id,seq DESC)"
    )


def _record(row: sqlite3.Row) -> RemoteCall:
    fields = {key: row[key] for key in RemoteCall.model_fields}
    fields["arguments"] = json.loads(row["arguments"])
    return RemoteCall.model_validate(fields)


def _origin(user_id: str) -> Terminal:
    return Terminal(
        terminal_id="wintermute:internal:remote-tool:"
        + hashlib.sha256(user_id.encode()).hexdigest(),
        user_id=user_id,
        allowed_layers=("notification",),
    )


def _notice(row: sqlite3.Row, now: datetime, text: str | None = None) -> EventInput:
    if text is None:
        text = f"远程调用 {row['id']} 已结束，详情见终端工具调用记录。"
    return EventInput(
        terminal_id=_origin(row["user_id"]).terminal_id,
        conversation_id=row["conversation_id"],
        layer="notification",
        type="terminal_tool.result",
        source_id="terminal_tool",
        producer_event_id=row["id"],
        correlation_id=row["id"],
        reply_terminal_id=row["reply_terminal_id"],
        occurred_at=now,
        payload={"text": text},
    )


class RemoteCallStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store
        self._db = store._connection

    def create(
        self,
        *,
        user_id: str,
        conversation_id: str,
        terminal_id: str,
        tool: str,
        arguments: dict[str, JsonValue],
        timeout_seconds: int,
        run_id: str,
        call_id: str,
        reply_terminal_id: str | None = None,
        available: bool = True,
    ) -> RemoteCall:
        request = _Request(
            id="rc_" + uuid4().hex, tool=tool, arguments=arguments, timeout_seconds=timeout_seconds
        )
        args = json.dumps(
            request.arguments,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(args.encode()) > 16 * 1024:
            raise ValueError("Tool arguments exceed 16 KiB")
        if not all(isinstance(k, str) and 0 < len(k) <= 256 for k in (call_id, run_id)):
            raise ValueError("A run and tool call identifier are required")
        with self._store._transaction():
            self._store._check_scope(conversation_id, user_id)
            for target in (terminal_id, reply_terminal_id):
                if target is not None and self._store.get_terminal(target).user_id != user_id:
                    raise ValueError("Terminal belongs to a different user")
            row = self._db.execute(
                "SELECT * FROM remote_tool_calls WHERE user_id=? AND conversation_id=? "
                "AND run_id=? AND call_id=?",
                (user_id, conversation_id, run_id, call_id),
            ).fetchone()
            if row is not None:
                if (
                    row["terminal_id"],
                    row["tool"],
                    row["arguments"],
                    row["timeout_seconds"],
                    row["reply_terminal_id"],
                ) != (terminal_id, tool, args, timeout_seconds, reply_terminal_id):
                    raise RemoteCallConflict("Tool call identifier was reused with different input")
                return _record(row)
            if not available:
                raise ValueError("此终端当前未提供该工具，请使用 list_targets 查看可用工具")
            self._store._register_terminal(_origin(user_id))
            instant = self._store.now_utc()
            now = instant.timestamp()
            row = self._db.execute(
                "INSERT INTO remote_tool_calls(id,user_id,conversation_id,terminal_id,tool,"
                "arguments,timeout_seconds,run_id,call_id,reply_terminal_id,status,created_at,"
                "deadline) VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?) RETURNING *",
                (
                    request.id,
                    user_id,
                    conversation_id,
                    terminal_id,
                    tool,
                    args,
                    timeout_seconds,
                    run_id,
                    call_id,
                    reply_terminal_id,
                    now,
                    now + _QUEUE_SECONDS,
                ),
            ).fetchone()
            # Do not dispatch if even the smallest possible result would be undeliverable.
            self._store._check_event_size(_notice(row, instant.replace(microsecond=999999)))
            return _record(row)

    def claim(
        self,
        *,
        user_id: str,
        conversation_id: str,
        terminal_id: str,
        tools: Sequence[str],
    ) -> RemoteClaim | None:
        if not tools:
            return None
        if isinstance(tools, str) or len(tools) > 64:
            raise ValueError("Invalid terminal tool list")
        with self._store._transaction():
            now = self._store.now_utc().timestamp()
            slots = ",".join("?" for _ in tools)
            row = self._db.execute(
                "SELECT * FROM remote_tool_calls WHERE user_id=? AND conversation_id=? "
                f"AND terminal_id=? AND status='queued' AND deadline>? AND tool IN ({slots}) "
                "ORDER BY seq LIMIT 1",
                (user_id, conversation_id, terminal_id, now, *tools),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            self._db.execute(
                "UPDATE remote_tool_calls SET status='running', started_at=?,deadline=?,"
                "claim_hash=? WHERE seq=?",
                (
                    now,
                    now + row["timeout_seconds"] + _RECEIPT_GRACE_SECONDS,
                    hashlib.sha256(token.encode()).digest(),
                    row["seq"],
                ),
            )
            return RemoteClaim(
                id=row["id"],
                tool=row["tool"],
                arguments=json.loads(row["arguments"]),
                timeout_seconds=row["timeout_seconds"],
                claim_token=token,
            )

    def complete(
        self,
        *,
        user_id: str,
        conversation_id: str,
        terminal_id: str,
        id: str,
        claim_token: str,
        status: ClientStatus,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        truncated: bool = False,
    ) -> RemoteCall:
        if status not in ("succeeded", "failed", "timed_out"):
            raise ValueError("Invalid execution status")
        if any(len(stream.encode()) > 8192 for stream in (stdout, stderr)):
            raise ValueError("Each output stream must fit within 8 KiB")
        if exit_code is not None and (
            type(exit_code) is not int or not -(2**63) <= exit_code < 2**63
        ):
            raise ValueError("Invalid process exit code")
        if type(truncated) is not bool:
            raise ValueError("Invalid output truncation flag")
        with self._store._transaction():
            row = self._db.execute(
                "SELECT * FROM remote_tool_calls WHERE user_id=? AND conversation_id=? "
                "AND terminal_id=? AND id=?",
                (user_id, conversation_id, terminal_id, id),
            ).fetchone()
            digest = hashlib.sha256(claim_token.encode()).digest()
            if (
                row is None
                or row["claim_hash"] is None
                or not hmac.compare_digest(row["claim_hash"], digest)
            ):
                raise LookupError("Unknown terminal tool claim")
            now = self._store.now_utc()
            if row["status"] == "running" and row["deadline"] <= now.timestamp():
                self._expire_row(row, now)
            elif row["status"] == "running":
                return self._finish(row, now, status, stdout, stderr, exit_code, truncated)
            elif (
                row["status"],
                row["stdout"],
                row["stderr"],
                row["exit_code"],
                bool(row["truncated"]),
            ) == (status, stdout, stderr, exit_code, truncated):
                return _record(row)
        raise RemoteCallConflict("Call has expired or a different result was already recorded")

    def _finish(
        self,
        row: sqlite3.Row,
        now: datetime,
        status: CallStatus,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        truncated: bool,
    ) -> RemoteCall:
        row = self._db.execute(
            "UPDATE remote_tool_calls SET status=?,stdout=?,stderr=?,exit_code=?,truncated=?,"
            "completed_at=?,deadline=NULL WHERE seq=? RETURNING *",
            (status, stdout, stderr, exit_code, truncated, now.timestamp(), row["seq"]),
        ).fetchone()
        call = _record(row)
        # Keep the correlation visible even if results arrive after a restart or out of order.
        prefix = f"远程调用 {call.id}，终端 {call.terminal_id}，工具 {call.tool}，状态 {status}。"
        summary = f"{prefix}\n退出码：{exit_code}" if exit_code is not None else prefix
        content = summary + f"\n标准输出：\n{stdout}\n标准错误：\n{stderr}"
        if truncated:
            content += "\n终端已截断输出。"
        event = _notice(row, now, content)
        try:
            self._store._check_event_size(event)
        except EventError:
            # The full receipt remains inspectable when the deployment limits event size.
            event = _notice(row, now)
            self._store._check_event_size(event)
        self._store._append_event(event)
        return call

    def _expire_row(self, row: sqlite3.Row, now: datetime) -> None:
        reason = (
            "终端未及时领取，命令未执行。"
            if row["status"] == "queued"
            else "终端已领取，未及时回传结果，执行结果未知。"
        )
        self._finish(row, now, "expired", "", reason, None, False)

    def expire(self) -> int:
        now = self._store.now_utc()
        if not self._db.execute(
            "SELECT 1 FROM remote_tool_calls WHERE deadline<=? LIMIT 1", (now.timestamp(),)
        ).fetchone():
            return 0
        with self._store._transaction():
            rows = self._db.execute(
                "SELECT * FROM remote_tool_calls WHERE deadline<=? ORDER BY deadline LIMIT 100",
                (now.timestamp(),),
            ).fetchall()
            for row in rows:
                self._expire_row(row, now)
            return len(rows)

    def get(self, *, user_id: str, conversation_id: str, id: str) -> RemoteCall:
        row = self._db.execute(
            "SELECT * FROM remote_tool_calls WHERE user_id=? AND conversation_id=? AND id=?",
            (user_id, conversation_id, id),
        ).fetchone()
        if row is None:
            raise LookupError("Unknown remote tool call")
        return _record(row)

    def list(
        self,
        *,
        user_id: str,
        conversation_id: str,
        limit: int = 30,
        cursor: str | None = None,
    ) -> tuple[list[RemoteCall], str | None]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        where = "user_id=? AND conversation_id=?"
        params: list[str | int] = [user_id, conversation_id]
        if cursor is not None:
            if (
                not cursor.isascii()
                or not cursor.isdigit()
                or len(cursor) > 19
                or not 0 < int(cursor) < 2**63
            ):
                raise ValueError("Invalid remote tool call cursor")
            where += " AND seq<?"
            params.append(int(cursor))
        rows = self._db.execute(
            f"SELECT * FROM remote_tool_calls WHERE {where} ORDER BY seq DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        next_cursor = str(rows[limit - 1]["seq"]) if len(rows) > limit else None
        return [_record(row) for row in rows[:limit]], next_cursor
