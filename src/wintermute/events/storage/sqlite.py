"""使用 SQLite 分层记录事件，并原子保存调度工作和终端输出。"""

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Self
from uuid import uuid4
from zoneinfo import ZoneInfo

from wintermute.agent.core.types import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from wintermute.events.core.slices import SliceProcessingStatus, SliceStreamItem
from wintermute.events.core.types import (
    DuplicateEventError,
    EventError,
    EventInput,
    EventRecord,
    JobRecord,
    JobStatus,
    Layer,
    OutputRecord,
    Receipt,
    SchedulerBusyError,
    Terminal,
    utc_now,
)

_TABLES: dict[Layer, str] = {
    "conversation": "conversation_events",
    "notification": "notification_events",
    "perception": "perception_events",
    "slice": "slice_events",
}
_JOB_FIELDS = "job_id,event_id,user_id,conversation_id,layer,status,priority,attempts,error"
_OUTPUT_FIELDS = (
    "output_id,event_id,job_id,user_id,conversation_id,terminal_id,layer,text,status,error"
)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _messages_json(messages: tuple[Message, ...]) -> str:
    """保留工具调用与结果的完整结构，避免恢复时只剩文本。"""
    pending: dict[str, str] = {}
    for message in messages:
        if isinstance(message, ToolResultMessage):
            if pending.get(message.tool_call_id) != message.name:
                raise EventError("Transcript contains an unmatched tool result")
            del pending[message.tool_call_id]
        else:
            if pending:
                raise EventError("Transcript contains tool calls without results")
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls:
                    if not call.id or not call.name or call.id in pending:
                        raise EventError("Transcript contains invalid tool calls")
                    pending[call.id] = call.name
            elif not isinstance(message, UserMessage):
                raise EventError("Transcript contains an unsupported message")
    if pending:
        raise EventError("Transcript ends with unfinished tool calls")
    return _json([asdict(message) for message in messages])


def _messages_from_json(value: str) -> tuple[Message, ...]:
    result: list[Message] = []
    for item in json.loads(value):
        role = item.pop("role")
        if role == "user":
            result.append(UserMessage(**item))
        elif role == "assistant":
            item["tool_calls"] = tuple(ToolCall(**call) for call in item["tool_calls"])
            result.append(AssistantMessage(**item))
        elif role == "tool":
            result.append(ToolResultMessage(**item))
        else:
            raise EventError("Stored transcript contains an unsupported role")
    return tuple(result)


class SQLiteStore:
    """同一线程内使用的短事务存储；模型和网络调用均在事务外执行。

    同一个数据库只允许一个持有有效租约的调度器。多个接收端可以使用
    各自的连接提交事件；工作项领取和事件去重由数据库事务协调。
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        max_event_bytes: int = 256 * 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(max_event_bytes, bool)
            or not isinstance(max_event_bytes, int)
            or max_event_bytes < 1
        ):
            raise ValueError("max_event_bytes must be positive")
        self.max_event_bytes = max_event_bytes
        self._clock = clock or utc_now
        self._owner: str | None = None
        self._closed = False
        self._connection = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._initialize()

    def _initialize(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self._connection.close()
            raise EventError("Unsupported event database schema version")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS terminals (
                terminal_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                terminal_id TEXT NOT NULL,
                producer_event_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                layer TEXT NOT NULL,
                job_id TEXT,
                UNIQUE(terminal_id,producer_event_id)
            );
            CREATE TABLE IF NOT EXISTS dispatch_jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                event_id TEXT UNIQUE NOT NULL REFERENCES receipts(event_id),
                user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                layer TEXT NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                messages TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS dispatch_ready
                ON dispatch_jobs(status,priority,seq);
            CREATE TABLE IF NOT EXISTS perception_state (
                user_id TEXT NOT NULL,
                conversation_scope TEXT NOT NULL,
                terminal_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                state_key TEXT NOT NULL,
                event_id TEXT NOT NULL REFERENCES receipts(event_id),
                valid_until REAL,
                PRIMARY KEY(user_id,conversation_scope,terminal_id,source_id,subject,state_key)
            );
            CREATE TABLE IF NOT EXISTS outputs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                output_id TEXT UNIQUE NOT NULL,
                event_id TEXT NOT NULL REFERENCES receipts(event_id),
                job_id TEXT UNIQUE NOT NULL REFERENCES dispatch_jobs(job_id),
                user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                terminal_id TEXT,
                layer TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS scheduler_lease (
                slot INTEGER PRIMARY KEY CHECK(slot=1),
                owner_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )
        for table in _TABLES.values():
            self._connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                "event_id TEXT UNIQUE NOT NULL REFERENCES receipts(event_id),"
                "user_id TEXT NOT NULL, conversation_id TEXT, data TEXT NOT NULL)"
            )
            self._connection.execute(
                f"CREATE INDEX IF NOT EXISTS {table}_scope ON {table}(user_id,conversation_id,seq)"
            )
        # 只新增切片相关表，保留旧库的所有事件、队列和终端权限。
        self._connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS slice_stream (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT UNIQUE NOT NULL,
                user_id TEXT NOT NULL,
                conversation_scope TEXT NOT NULL,
                day TEXT NOT NULL,
                timezone TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL,
                created_at REAL NOT NULL,
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS slice_stream_scope
                ON slice_stream(user_id,day,timezone,conversation_scope,ended_at);
            CREATE TABLE IF NOT EXISTS slice_processing (
                event_id TEXT PRIMARY KEY REFERENCES slice_events(event_id),
                item_id TEXT REFERENCES slice_stream(item_id),
                attempts INTEGER NOT NULL DEFAULT 0,
                error_type TEXT,
                retry_at REAL
            );
            CREATE INDEX IF NOT EXISTS slice_processing_pending
                ON slice_processing(item_id,retry_at);
            PRAGMA user_version=2;
            COMMIT;
            """
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Store clock must return timezone-aware datetimes")
        return value.astimezone(UTC)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Event store is closed")

    def register_terminal(self, terminal: Terminal) -> None:
        terminal = Terminal.model_validate(terminal.model_dump())
        with self._transaction():
            existing = self._connection.execute(
                "SELECT user_id FROM terminals WHERE terminal_id=?", (terminal.terminal_id,)
            ).fetchone()
            if existing is not None and existing["user_id"] != terminal.user_id:
                raise EventError("A terminal cannot change its owner")
            self._connection.execute(
                "INSERT INTO terminals(terminal_id,user_id,data) VALUES(?,?,?) "
                "ON CONFLICT(terminal_id) DO UPDATE SET data=excluded.data",
                (terminal.terminal_id, terminal.user_id, terminal.model_dump_json()),
            )

    def get_terminal(self, terminal_id: str) -> Terminal:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT data FROM terminals WHERE terminal_id=?", (terminal_id,)
        ).fetchone()
        if row is None:
            raise EventError(f"Unknown terminal: {terminal_id}")
        return Terminal.model_validate_json(row["data"])

    def register_conversation(self, conversation_id: str, user_id: str) -> None:
        if not conversation_id.strip() or not user_id.strip():
            raise EventError("Conversation and user IDs must be nonempty")
        with self._transaction():
            row = self._connection.execute(
                "SELECT user_id FROM conversations WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
            if row is not None and row["user_id"] != user_id:
                raise EventError("A conversation cannot change its owner")
            self._connection.execute(
                "INSERT OR IGNORE INTO conversations(conversation_id,user_id) VALUES(?,?)",
                (conversation_id, user_id),
            )

    def get_conversation_user(self, conversation_id: str) -> str:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT user_id FROM conversations WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise EventError(f"Unknown conversation: {conversation_id}")
        return str(row["user_id"])

    def _check_scope(self, conversation_id: str | None, user_id: str) -> None:
        if conversation_id is not None and self.get_conversation_user(conversation_id) != user_id:
            raise EventError("Conversation belongs to a different user")

    def append(self, event: EventInput) -> Receipt:
        # 冻结模型的嵌套 payload 仍可能被修改，因此持久化前重新验证快照。
        event = EventInput.model_validate(event.model_dump())
        encoded = _json(event.model_dump(mode="json"))
        if len(encoded.encode("utf-8")) > self.max_event_bytes:
            raise EventError("Event exceeds the configured byte limit")
        fingerprint_data = event.model_dump(mode="json", exclude={"occurred_at"})
        fingerprint = hashlib.sha256(_json(fingerprint_data).encode("utf-8")).hexdigest()
        with self._transaction():
            terminal = self.get_terminal(event.terminal_id)
            if event.layer not in terminal.allowed_layers:
                raise EventError("Terminal is not allowed to use this layer")
            self._check_scope(event.conversation_id, terminal.user_id)
            if event.reply_terminal_id is not None:
                reply = self.get_terminal(event.reply_terminal_id)
                if reply.user_id != terminal.user_id:
                    raise EventError("Reply terminal belongs to a different user")
            existing = self._connection.execute(
                "SELECT event_id,fingerprint,job_id FROM receipts "
                "WHERE terminal_id=? AND producer_event_id=?",
                (event.terminal_id, event.producer_event_id),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise DuplicateEventError("Producer event ID was reused with different content")
                return Receipt(
                    event=self.get_event(existing["event_id"]),
                    duplicate=True,
                    job_id=existing["job_id"],
                )
            record = EventRecord(
                **event.model_dump(),
                event_id=f"evt_{uuid4().hex}",
                user_id=terminal.user_id,
                received_at=self._now(),
            )
            job_id = (
                f"job_{uuid4().hex}" if event.layer in ("conversation", "notification") else None
            )
            self._connection.execute(
                "INSERT INTO receipts(event_id,terminal_id,producer_event_id,"
                "fingerprint,layer,job_id) "
                "VALUES(?,?,?,?,?,?)",
                (
                    record.event_id,
                    record.terminal_id,
                    record.producer_event_id,
                    fingerprint,
                    record.layer,
                    job_id,
                ),
            )
            table = _TABLES[record.layer]
            self._connection.execute(
                f"INSERT INTO {table}(event_id,user_id,conversation_id,data) VALUES(?,?,?,?)",
                (
                    record.event_id,
                    record.user_id,
                    record.conversation_id,
                    record.model_dump_json(),
                ),
            )
            if job_id is not None:
                self._connection.execute(
                    "INSERT INTO dispatch_jobs(job_id,event_id,user_id,conversation_id,layer,"
                    "status,priority) VALUES(?,?,?,?,?,'queued',?)",
                    (
                        job_id,
                        record.event_id,
                        record.user_id,
                        record.conversation_id,
                        record.layer,
                        0 if record.layer == "conversation" else 10,
                    ),
                )
            elif record.layer == "perception":
                self._project_state(record)
            elif record.layer == "slice":
                self._connection.execute(
                    "INSERT INTO slice_processing(event_id) VALUES(?)", (record.event_id,)
                )
            return Receipt(event=record, duplicate=False, job_id=job_id)

    def _project_state(self, event: EventRecord) -> None:
        subject = event.payload.get("subject")
        key = event.payload.get("state_key")
        if not isinstance(subject, str) or not isinstance(key, str) or not subject or not key:
            return
        scope = event.conversation_id or ""
        state_key = (event.user_id, scope, event.terminal_id, event.source_id, subject, key)
        row = self._connection.execute(
            "SELECT event_id FROM perception_state WHERE user_id=? AND conversation_scope=? "
            "AND terminal_id=? AND source_id=? AND subject=? AND state_key=?",
            state_key,
        ).fetchone()
        if row is not None:
            previous = self.get_event(row["event_id"])
            if event.source_seq is not None and previous.source_seq is not None:
                newer = event.source_seq > previous.source_seq
            else:
                newer = event.occurred_at >= previous.occurred_at
            if not newer:
                return
        valid_until = event.payload.get("valid_until")
        expiry = (
            datetime.fromisoformat(valid_until).timestamp()
            if isinstance(valid_until, str)
            else None
        )
        self._connection.execute(
            "INSERT INTO perception_state(user_id,conversation_scope,terminal_id,source_id,subject,"
            "state_key,event_id,valid_until) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,conversation_scope,terminal_id,source_id,subject,state_key) "
            "DO UPDATE SET event_id=excluded.event_id,valid_until=excluded.valid_until",
            (*state_key, event.event_id, expiry),
        )

    def get_event(self, event_id: str, *, user_id: str | None = None) -> EventRecord:
        self._ensure_open()
        receipt = self._connection.execute(
            "SELECT layer FROM receipts WHERE event_id=?", (event_id,)
        ).fetchone()
        if receipt is None:
            raise EventError(f"Unknown event: {event_id}")
        table = _TABLES[receipt["layer"]]
        row = self._connection.execute(
            f"SELECT data FROM {table} WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise EventError("Event receipt has no matching record")
        event = EventRecord.model_validate_json(row["data"])
        if user_id is not None and event.user_id != user_id:
            raise EventError("Event belongs to a different user")
        return event

    @staticmethod
    def _limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("limit must be an integer between 1 and 10000")
        return limit

    def list_events(
        self, layer: Layer, *, user_id: str, conversation_id: str | None = None, limit: int = 100
    ) -> tuple[EventRecord, ...]:
        self._ensure_open()
        self._check_scope(conversation_id, user_id)
        table = _TABLES[layer]
        where = "user_id=?"
        params: list[Any] = [user_id]
        if conversation_id is not None:
            where += " AND conversation_id=?"
            params.append(conversation_id)
        rows = self._connection.execute(
            f"SELECT data FROM {table} WHERE {where} ORDER BY seq DESC LIMIT ?",
            (*params, self._limit(limit)),
        ).fetchall()
        return tuple(EventRecord.model_validate_json(row["data"]) for row in reversed(rows))

    def list_states(
        self,
        *,
        user_id: str,
        conversation_id: str | None = None,
        as_of: datetime | None = None,
        limit: int = 32,
    ) -> tuple[EventRecord, ...]:
        self._ensure_open()
        self._check_scope(conversation_id, user_id)
        instant = as_of or self._now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        rows = self._connection.execute(
            "SELECT e.data FROM perception_state s "
            "JOIN perception_events e ON e.event_id=s.event_id "
            "WHERE s.user_id=? AND (s.conversation_scope='' OR s.conversation_scope=?) "
            "AND (s.valid_until IS NULL OR s.valid_until>?) ORDER BY e.seq DESC LIMIT ?",
            (user_id, conversation_id or "", instant.timestamp(), self._limit(limit)),
        ).fetchall()
        return tuple(EventRecord.model_validate_json(row["data"]) for row in rows)

    def pending_slices(self, *, limit: int = 100) -> tuple[EventRecord, ...]:
        """读取可处理原始切片；失败项到达重试时间后重新成为候选。"""
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT e.data FROM slice_events e JOIN slice_processing p ON p.event_id=e.event_id "
            "WHERE p.item_id IS NULL AND (p.retry_at IS NULL OR p.retry_at<=?) "
            "ORDER BY e.seq LIMIT ?",
            (self._now().timestamp(), self._limit(limit)),
        ).fetchall()
        return tuple(EventRecord.model_validate_json(row["data"]) for row in rows)

    def has_unprocessed_slices(self) -> bool:
        """包含仍在等待重试的事件，避免将暂时无候选误判为处理完成。"""
        self._ensure_open()
        return (
            self._connection.execute(
                "SELECT 1 FROM slice_processing WHERE item_id IS NULL LIMIT 1"
            ).fetchone()
            is not None
        )

    def commit_slice(self, item: SliceStreamItem, *, owner_id: str | None = None) -> None:
        """原子保存压缩结果及来源映射；重复处理不能覆盖已经完成的投影。"""
        item = SliceStreamItem.model_validate(item.model_dump())
        with self._transaction():
            if owner_id is not None:
                self._require_scheduler(owner_id)
            self._check_scope(item.conversation_id, item.user_id)
            existing = self._connection.execute(
                "SELECT data FROM slice_stream WHERE item_id=?", (item.item_id,)
            ).fetchone()
            if existing is not None:
                if SliceStreamItem.model_validate_json(existing["data"]) == item:
                    return
                raise EventError("Slice stream item ID was reused with different content")
            source_events: list[EventRecord] = []
            for event_id in item.source_event_ids:
                event = self.get_event(event_id, user_id=item.user_id)
                if (
                    event.layer != "slice"
                    or event.conversation_id != item.conversation_id
                    or event.terminal_id != item.terminal_id
                    or event.source_id != item.source_id
                ):
                    raise EventError("Slice stream sources must have matching ownership and scope")
                state = self._connection.execute(
                    "SELECT item_id FROM slice_processing WHERE event_id=?", (event_id,)
                ).fetchone()
                if state is None or state["item_id"] is not None:
                    raise EventError("Slice source is missing or already processed")
                source_events.append(event)
            times = [event.occurred_at for event in source_events]
            if min(times) != item.started_at or max(times) != item.ended_at:
                raise EventError("Slice stream range must match its source event timestamps")
            self._connection.execute(
                "INSERT INTO slice_stream(item_id,user_id,conversation_scope,day,timezone,"
                "started_at,ended_at,created_at,data) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    item.item_id,
                    item.user_id,
                    item.conversation_id or "",
                    item.day,
                    item.timezone,
                    item.started_at.timestamp(),
                    item.ended_at.timestamp(),
                    item.created_at.timestamp(),
                    item.model_dump_json(),
                ),
            )
            self._connection.executemany(
                "UPDATE slice_processing SET item_id=?,attempts=attempts+1,"
                "error_type=NULL,retry_at=NULL WHERE event_id=?",
                [(item.item_id, event_id) for event_id in item.source_event_ids],
            )

    def mark_slice_failed(
        self,
        event_ids: Sequence[str],
        error_type: str,
        *,
        retry_delay: float,
        owner_id: str | None = None,
    ) -> None:
        """保存失败次数和重试时间；取消操作应保留待处理状态而不调用本方法。"""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", error_type):
            raise ValueError("error_type must contain only an exception type name")
        if isinstance(retry_delay, bool) or not math.isfinite(retry_delay) or retry_delay < 0:
            raise ValueError("retry_delay must be finite and nonnegative")
        if not event_ids or len(set(event_ids)) != len(event_ids):
            raise ValueError("event_ids must be nonempty and unique")
        with self._transaction():
            if owner_id is not None:
                self._require_scheduler(owner_id)
            for event_id in event_ids:
                if self.get_event(event_id).layer != "slice":
                    raise EventError("Only slice events can record processing failure")
            retry_at = self._now().timestamp() + retry_delay
            self._connection.executemany(
                "UPDATE slice_processing SET attempts=attempts+1,error_type=?,retry_at=? "
                "WHERE event_id=? AND item_id IS NULL",
                [(error_type, retry_at, event_id) for event_id in event_ids],
            )

    def get_slice_status(
        self, event_id: str, *, user_id: str | None = None
    ) -> SliceProcessingStatus:
        """按事件查询处理结果；可通过用户归属限制调用方的查询范围。"""
        if self.get_event(event_id, user_id=user_id).layer != "slice":
            raise EventError("Only slice events have slice processing status")
        row = self._connection.execute(
            "SELECT * FROM slice_processing WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise EventError("Slice event has no processing status")
        retry_at = row["retry_at"]
        return SliceProcessingStatus(
            event_id=event_id,
            status=(
                "completed"
                if row["item_id"] is not None
                else "retrying"
                if retry_at is not None
                else "pending"
            ),
            attempts=row["attempts"],
            error_type=row["error_type"],
            retry_at=datetime.fromtimestamp(retry_at, UTC) if retry_at is not None else None,
            item_id=row["item_id"],
        )

    def list_slice_stream(
        self,
        *,
        user_id: str,
        day: str,
        timezone: str,
        conversation_id: str | None = None,
        limit: int = 100,
        as_of: datetime | None = None,
    ) -> tuple[SliceStreamItem, ...]:
        """按发生时间倒序读取指定日的全局及本会话切片流。"""
        self._ensure_open()
        self._check_scope(conversation_id, user_id)
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("day must use YYYY-MM-DD")
        ZoneInfo(timezone)
        where = (
            "user_id=? AND day=? AND timezone=? AND (conversation_scope='' OR conversation_scope=?)"
        )
        params: list[Any] = [user_id, day, timezone, conversation_id or ""]
        if as_of is not None:
            if as_of.tzinfo is None or as_of.utcoffset() is None:
                raise ValueError("as_of must include a timezone")
            where += " AND ended_at<=? AND created_at<=?"
            params.extend((as_of.timestamp(), as_of.timestamp()))
        rows = self._connection.execute(
            f"SELECT data FROM slice_stream WHERE {where} "
            "ORDER BY ended_at DESC,started_at DESC,seq DESC LIMIT ?",
            (*params, self._limit(limit)),
        ).fetchall()
        return tuple(SliceStreamItem.model_validate_json(row["data"]) for row in rows)

    def get_job(self, job_id: str, *, user_id: str | None = None) -> JobRecord:
        self._ensure_open()
        row = self._connection.execute(
            f"SELECT {_JOB_FIELDS} FROM dispatch_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise EventError(f"Unknown job: {job_id}")
        job = JobRecord(**dict(row))
        if user_id is not None and job.user_id != user_id:
            raise EventError("Job belongs to a different user")
        return job

    def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> tuple[JobRecord, ...]:
        self._ensure_open()
        if user_id is not None:
            self._check_scope(conversation_id, user_id)
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("user_id", user_id),
            ("conversation_id", conversation_id),
        ):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        clause = " AND ".join(where) or "1=1"
        rows = self._connection.execute(
            f"SELECT {_JOB_FIELDS} FROM dispatch_jobs WHERE {clause} ORDER BY seq LIMIT ?",
            (*params, self._limit(limit)),
        ).fetchall()
        return tuple(JobRecord(**dict(row)) for row in rows)

    def acquire_scheduler(self, owner_id: str, *, lease_seconds: float = 30) -> None:
        if not owner_id.strip() or not 0 < lease_seconds < float("inf"):
            raise ValueError("Scheduler owner and finite positive lease are required")
        now = self._now().timestamp()
        with self._transaction():
            row = self._connection.execute("SELECT * FROM scheduler_lease WHERE slot=1").fetchone()
            if row is not None and row["expires_at"] > now:
                if row["owner_id"] != owner_id or self._owner != owner_id:
                    raise SchedulerBusyError("Another scheduler owns this database")
            else:
                # 运行到一半的工具可能已产生外部效果，不自动重放遗留任务。
                self._connection.execute(
                    "UPDATE dispatch_jobs SET status='interrupted',error=? WHERE status='running'",
                    ("Previous scheduler stopped before recording completion",),
                )
            self._connection.execute(
                "INSERT INTO scheduler_lease(slot,owner_id,expires_at) VALUES(1,?,?) "
                "ON CONFLICT(slot) DO UPDATE SET owner_id=excluded.owner_id,"
                "expires_at=excluded.expires_at",
                (owner_id, now + lease_seconds),
            )
        self._owner = owner_id

    def _require_scheduler(self, owner_id: str | None = None) -> None:
        owner = owner_id or self._owner
        row = self._connection.execute("SELECT * FROM scheduler_lease WHERE slot=1").fetchone()
        if (
            owner is None
            or self._owner != owner
            or row is None
            or row["owner_id"] != owner
            or row["expires_at"] <= self._now().timestamp()
        ):
            raise SchedulerBusyError("Scheduler lease is missing or expired")

    def heartbeat_scheduler(self, owner_id: str, *, lease_seconds: float = 30) -> None:
        if not 0 < lease_seconds < float("inf"):
            raise ValueError("lease_seconds must be finite and positive")
        with self._transaction():
            self._require_scheduler(owner_id)
            self._connection.execute(
                "UPDATE scheduler_lease SET expires_at=? WHERE slot=1 AND owner_id=?",
                (self._now().timestamp() + lease_seconds, owner_id),
            )

    def release_scheduler(self, owner_id: str) -> None:
        with self._transaction():
            if self._owner != owner_id:
                # 同一连接可能已经被新调度器接管，旧实例清理时不得删除新租约。
                return
            self._connection.execute("DELETE FROM scheduler_lease WHERE owner_id=?", (owner_id,))
        self._owner = None

    def claim_next(
        self, excluded_conversations: Sequence[str] = (), *, owner_id: str | None = None
    ) -> JobRecord | None:
        with self._transaction():
            self._require_scheduler(owner_id)
            excluded = ""
            if excluded_conversations:
                placeholders = ",".join("?" for _ in excluded_conversations)
                excluded = f" AND conversation_id NOT IN ({placeholders})"
            row = self._connection.execute(
                "SELECT job_id FROM dispatch_jobs WHERE status='queued' "
                "AND conversation_id NOT IN "
                "(SELECT conversation_id FROM dispatch_jobs WHERE status='running')"
                f"{excluded} ORDER BY priority,seq LIMIT 1",
                tuple(excluded_conversations),
            ).fetchone()
            if row is None:
                return None
            self._connection.execute(
                "UPDATE dispatch_jobs SET status='running',attempts=attempts+1 WHERE job_id=?",
                (row["job_id"],),
            )
            return self.get_job(row["job_id"])

    def finish_job(
        self,
        job_id: str,
        *,
        messages: tuple[Message, ...],
        response: str,
        reply_terminal_id: str | None,
        owner_id: str | None = None,
    ) -> OutputRecord:
        transcript = _messages_json(messages)
        with self._transaction():
            self._require_scheduler(owner_id)
            job = self.get_job(job_id)
            if job.status != "running":
                raise EventError("Only a running job can finish")
            if reply_terminal_id is not None:
                terminal = self.get_terminal(reply_terminal_id)
                if terminal.user_id != job.user_id:
                    raise EventError("Output terminal belongs to a different user")
            output = OutputRecord(
                output_id=f"out_{uuid4().hex}",
                event_id=job.event_id,
                job_id=job_id,
                user_id=job.user_id,
                conversation_id=job.conversation_id,
                terminal_id=reply_terminal_id,
                layer=job.layer,
                text=response,
            )
            self._connection.execute(
                "UPDATE dispatch_jobs SET status='completed',messages=?,error=NULL WHERE job_id=?",
                (transcript, job_id),
            )
            self._connection.execute(
                "INSERT INTO outputs(output_id,event_id,job_id,user_id,conversation_id,"
                "terminal_id,layer,text) VALUES(?,?,?,?,?,?,?,?)",
                (
                    output.output_id,
                    output.event_id,
                    output.job_id,
                    output.user_id,
                    output.conversation_id,
                    output.terminal_id,
                    output.layer,
                    output.text,
                ),
            )
            return output

    def fail_job(
        self,
        job_id: str,
        error: str,
        *,
        messages: tuple[Message, ...] = (),
        interrupted: bool = False,
        owner_id: str | None = None,
    ) -> None:
        transcript = _messages_json(messages)
        with self._transaction():
            self._require_scheduler(owner_id)
            job = self.get_job(job_id)
            if job.status not in ("queued", "running"):
                raise EventError("Only queued or running jobs can fail")
            self._connection.execute(
                "UPDATE dispatch_jobs SET status=?,messages=?,error=? WHERE job_id=?",
                ("interrupted" if interrupted else "failed", transcript, error, job_id),
            )

    def history(self, conversation_id: str, user_id: str) -> tuple[Message, ...]:
        self._ensure_open()
        self._check_scope(conversation_id, user_id)
        rows = self._connection.execute(
            "SELECT messages FROM dispatch_jobs WHERE conversation_id=? AND user_id=? "
            "AND layer='conversation' AND status IN ('completed','failed','interrupted') "
            "ORDER BY seq",
            (conversation_id, user_id),
        ).fetchall()
        return tuple(message for row in rows for message in _messages_from_json(row["messages"]))

    def get_run_messages(self, job_id: str, *, user_id: str | None = None) -> tuple[Message, ...]:
        """查询某次执行的完整记录，包括没有写入交流历史的通知执行。"""
        self.get_job(job_id, user_id=user_id)
        row = self._connection.execute(
            "SELECT messages FROM dispatch_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        assert row is not None
        return _messages_from_json(row["messages"])

    def get_output(self, output_id: str, *, user_id: str | None = None) -> OutputRecord:
        self._ensure_open()
        row = self._connection.execute(
            f"SELECT {_OUTPUT_FIELDS} FROM outputs WHERE output_id=?", (output_id,)
        ).fetchone()
        if row is None:
            raise EventError(f"Unknown output: {output_id}")
        output = OutputRecord(**dict(row))
        if user_id is not None and output.user_id != user_id:
            raise EventError("Output belongs to a different user")
        return output

    def list_outputs(
        self,
        *,
        user_id: str | None = None,
        conversation_id: str | None = None,
        status: str | None = None,
        terminal_id: str | None = None,
        job_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OutputRecord, ...]:
        self._ensure_open()
        if user_id is not None:
            self._check_scope(conversation_id, user_id)
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("user_id", user_id),
            ("conversation_id", conversation_id),
            ("status", status),
            ("terminal_id", terminal_id),
            ("job_id", job_id),
        ):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        clause = " AND ".join(where) or "1=1"
        rows = self._connection.execute(
            f"SELECT {_OUTPUT_FIELDS} FROM outputs WHERE {clause} ORDER BY seq LIMIT ?",
            (*params, self._limit(limit)),
        ).fetchall()
        return tuple(OutputRecord(**dict(row)) for row in rows)

    def pending_outputs(
        self, user_id: str | None = None, *, terminal_id: str | None = None
    ) -> tuple[OutputRecord, ...]:
        return self.list_outputs(
            user_id=user_id, status="pending", terminal_id=terminal_id, limit=10000
        )

    def mark_output_delivered(self, output_id: str) -> None:
        with self._transaction():
            self.get_output(output_id)
            self._connection.execute(
                "UPDATE outputs SET status='delivered',error=NULL WHERE output_id=?", (output_id,)
            )

    def mark_output_failed(self, output_id: str, error: str) -> None:
        with self._transaction():
            self.get_output(output_id)
            self._connection.execute(
                "UPDATE outputs SET error=? WHERE output_id=? AND status='pending'",
                (error, output_id),
            )

    def close(self) -> None:
        if self._closed:
            return
        if self._owner is not None:
            self.release_scheduler(self._owner)
        self._connection.close()
        self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
