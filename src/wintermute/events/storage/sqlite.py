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
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from wintermute.agent.context.types import ContextCheckpoint, history_digest
from wintermute.agent.core.types import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from wintermute.events.core.activity import (
    ActivityItem,
    activity_message,
    perception_activity,
    slice_activity,
)
from wintermute.events.core.desktop import SNAPSHOT_STATE_KEY, is_desktop_snapshot
from wintermute.events.core.presentation import body_value, timestamp_text
from wintermute.events.core.slices import SliceProcessingStatus, SliceStreamItem
from wintermute.events.core.timeline_compression import TimelinePolicy
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
    event_message,
    utc_now,
)
from wintermute.events.storage import (
    compression_history,
    compression_sources,
    screenshot_groups,
    timeline_batches,
    timeline_rollups,
)
from wintermute.events.storage.history import (
    HistoryItem,
    browse,
    screenshot_frames,
    upgrade_history_storage,
)

if TYPE_CHECKING:
    from wintermute.events.slices.image import ScreenshotObservation


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


def _event_fingerprint(event: EventInput) -> str:
    fields = event.model_dump(mode="json", exclude={"occurred_at"})
    return hashlib.sha256(_json(fields).encode("utf-8")).hexdigest()


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
        timeline_policy: TimelinePolicy | None = None,
    ) -> None:
        if (
            isinstance(max_event_bytes, bool)
            or not isinstance(max_event_bytes, int)
            or max_event_bytes < 1
        ):
            raise ValueError("max_event_bytes must be positive")
        self.max_event_bytes = max_event_bytes
        self.timeline_policy = timeline_policy or TimelinePolicy()
        self._prefer_timeline_rollup = True
        self._clock = clock or utc_now
        self._owner: str | None = None
        self._history_bases: dict[str, int] = {}
        self._closed = False
        self._connection = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._initialize()
            self._connection.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            self._connection.close()
            raise

    def _initialize(self) -> None:
        version, has_tables = self._connection.execute(
            "SELECT user_version, EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name NOT GLOB 'sqlite_*') FROM pragma_user_version"
        ).fetchone()
        if version == 11:
            return
        if version in (3, 4, 5, 6, 7, 8, 9, 10):
            if version == 3:
                self._initialize_activity()
            if version < 5:
                self._upgrade_input_messages()
            if version < 6:
                self._upgrade_timeline_batches()
            self._upgrade_model_checkpoints()
            self._upgrade_history_browser()
            self._upgrade_desktop_snapshots()
            self._upgrade_continuous_timeline()
            self._upgrade_screenshot_groups()
            return
        if version != 0 or has_tables:
            raise EventError(
                f"Unsupported event database schema version {version}; use a new database"
            )
        event_tables = "\n".join(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "event_id TEXT UNIQUE NOT NULL REFERENCES receipts(event_id),"
            "user_id TEXT NOT NULL, conversation_id TEXT, data TEXT NOT NULL);"
            f"CREATE INDEX IF NOT EXISTS {table}_scope ON {table}(user_id,conversation_id,seq);"
            for table in _TABLES.values()
        )
        self._connection.executescript(
            f"""
            BEGIN IMMEDIATE;
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
                messages TEXT NOT NULL DEFAULT '[]',
                run_id TEXT REFERENCES dispatch_jobs(job_id),
                input_run_id TEXT REFERENCES dispatch_jobs(job_id),
                input_index INTEGER CHECK(input_index >= 0),
                UNIQUE(input_run_id,input_index)
            );
            CREATE INDEX IF NOT EXISTS dispatch_ready
                ON dispatch_jobs(status,conversation_id,seq);
            CREATE INDEX IF NOT EXISTS dispatch_run ON dispatch_jobs(run_id,seq);
            CREATE TABLE IF NOT EXISTS tool_batches (
                run_id TEXT PRIMARY KEY REFERENCES dispatch_jobs(job_id),
                declaration TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tool_batch_calls (
                run_id TEXT NOT NULL REFERENCES tool_batches(run_id) ON DELETE CASCADE,
                call_id TEXT NOT NULL,
                name TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('declared','started','settled')),
                result TEXT,
                PRIMARY KEY(run_id,call_id)
            );
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
            {event_tables}
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
            CREATE TABLE IF NOT EXISTS audio_transcripts (
                event_id TEXT PRIMARY KEY REFERENCES slice_events(event_id),
                text TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            PRAGMA user_version=3;
            COMMIT;
            """
        )

        self._initialize_activity()
        self._upgrade_input_messages()
        self._upgrade_timeline_batches()
        self._upgrade_model_checkpoints()
        self._upgrade_history_browser()
        self._upgrade_desktop_snapshots()
        self._upgrade_continuous_timeline()
        self._upgrade_screenshot_groups()

    def _upgrade_screenshot_groups(self) -> None:
        with self._transaction():
            if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 11:
                return
            timeline_batches.enable_screenshot_threshold(self._connection)
            screenshot_groups.initialize(self._connection)
            self._connection.execute("PRAGMA user_version=11")

    def _upgrade_continuous_timeline(self) -> None:
        with self._transaction():
            if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 10:
                return
            timeline_batches.initialize(self._connection, self.timeline_policy)
            self._connection.execute("PRAGMA user_version=10")

    def _upgrade_desktop_snapshots(self) -> None:
        with self._transaction():
            if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 9:
                return
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS desktop_snapshot_receipts ("
                "terminal_id TEXT NOT NULL, producer_event_id TEXT NOT NULL, "
                "conversation_id TEXT NOT NULL, event_id TEXT UNIQUE NOT NULL, "
                "fingerprint TEXT NOT NULL, PRIMARY KEY(terminal_id,producer_event_id))"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS desktop_snapshot_scope "
                "ON desktop_snapshot_receipts(terminal_id,conversation_id)"
            )
            rows = self._connection.execute(
                "SELECT e.data,r.fingerprint FROM perception_events e JOIN receipts r "
                "ON e.event_id=r.event_id WHERE e.conversation_id IS NOT NULL AND "
                "json_extract(e.data,'$.payload.state_key')=?",
                (SNAPSHOT_STATE_KEY,),
            )
            for row in rows:
                event = EventRecord.model_validate_json(row["data"])
                payload = event.payload
                if (
                    event.source_id == "default"
                    and event.type == "state.observed"
                    and event.source_seq is None
                    and event.correlation_id is None
                    and event.reply_terminal_id is None
                    and payload.keys() == {"subject", "state_key", "value"}
                    and payload["subject"] == f"device:{event.terminal_id}"
                    and is_desktop_snapshot(payload["state_key"], payload["value"])
                ):
                    self._connection.execute(
                        "INSERT OR IGNORE INTO desktop_snapshot_receipts VALUES (?,?,?,?,?)",
                        (
                            event.terminal_id,
                            event.producer_event_id,
                            event.conversation_id,
                            event.event_id,
                            row["fingerprint"],
                        ),
                    )
            # Old conversations retain the exact text they received. Remove only outstanding
            # deliveries so an upgrade cannot inject a stale foreground window into a new turn.
            self._connection.execute(
                "DELETE FROM activity_deliveries WHERE applied=0 AND activity_seq IN ("
                "SELECT i.seq FROM desktop_snapshot_receipts s "
                "JOIN activity_items i ON s.event_id=i.activity_id)"
            )
            self._connection.execute("PRAGMA user_version=9")

    def _upgrade_history_browser(self) -> None:
        with self._transaction():
            if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 8:
                return
            upgrade_history_storage(self._connection)
            self._connection.execute("PRAGMA user_version=8")

    def _upgrade_model_checkpoints(self) -> None:
        with self._transaction():
            if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 7:
                return
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS session_model_state ("
                "conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),"
                "read_count INTEGER NOT NULL CHECK(read_count>=0),"
                "summary TEXT NOT NULL DEFAULT '',"
                "summarized_messages INTEGER NOT NULL DEFAULT 0 "
                "CHECK(summarized_messages BETWEEN 0 AND read_count),"
                "prefix_digest TEXT NOT NULL DEFAULT '',compactions INTEGER NOT NULL DEFAULT 0)"
            )
            # 旧版只能从完成的主运行推断已处理边界；其后失败或中断的输入仍须原样发送。
            self._connection.execute(
                "INSERT OR IGNORE INTO session_model_state(conversation_id,read_count) "
                "SELECT h.conversation_id,SUM(COALESCE(json_array_length(j.messages),1)) "
                "FROM (SELECT s.conversation_id,MAX(s.seq) AS last_completed "
                "FROM session_history s JOIN dispatch_jobs r ON r.job_id=s.run_id "
                "WHERE r.status='completed' GROUP BY s.conversation_id) b "
                "JOIN session_history h ON h.conversation_id=b.conversation_id "
                "AND h.seq<=b.last_completed "
                "LEFT JOIN dispatch_jobs j ON j.job_id=h.run_id GROUP BY h.conversation_id"
            )
            self._connection.execute("PRAGMA user_version=7")

    def _upgrade_timeline_batches(self) -> None:
        with self._transaction():
            if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 6:
                return
            # 多个来源项可以在同一模型输入槽位被确认。
            self._connection.execute("DROP INDEX activity_input_slot")
            self._connection.execute(
                "CREATE INDEX activity_input_slot ON activity_deliveries(input_run_id,input_index)"
            )
            self._connection.execute("PRAGMA user_version=6")

    def _initialize_activity(self) -> None:
        # v3 的会话历史只补上排序位置；已有观测和摘要不回填到会话。
        self._connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS activity_items (
                seq INTEGER PRIMARY KEY, activity_id TEXT UNIQUE NOT NULL,
                after_receipt INTEGER NOT NULL, user_id TEXT NOT NULL, activity_key TEXT NOT NULL,
                occurred_at REAL NOT NULL, content TEXT NOT NULL, data TEXT NOT NULL,
                canonical_change INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS activity_timeline
                ON activity_items(user_id,occurred_at,seq);
            CREATE TABLE IF NOT EXISTS activity_heads (
                user_id TEXT NOT NULL, activity_key TEXT NOT NULL,
                source_seq INTEGER, occurred_at REAL NOT NULL, content TEXT NOT NULL,
                PRIMARY KEY(user_id,activity_key)
            );
            CREATE TABLE IF NOT EXISTS activity_deliveries (
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                activity_seq INTEGER NOT NULL REFERENCES activity_items(seq),
                activity_key TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(conversation_id,activity_seq)
            );
            CREATE INDEX IF NOT EXISTS activity_baseline
                ON activity_deliveries(conversation_id,activity_key,activity_seq);
            CREATE INDEX IF NOT EXISTS activity_pending
                ON activity_deliveries(conversation_id,activity_seq) WHERE applied=0;
            CREATE TABLE IF NOT EXISTS session_history (
                seq INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                run_id TEXT UNIQUE REFERENCES dispatch_jobs(job_id),
                activity_seq INTEGER REFERENCES activity_items(seq),
                UNIQUE(conversation_id,activity_seq),
                CHECK((run_id IS NULL) != (activity_seq IS NULL))
            );
            CREATE INDEX IF NOT EXISTS history_order ON session_history(conversation_id,seq);
            CREATE TABLE IF NOT EXISTS slice_revisions (
                previous_id TEXT PRIMARY KEY REFERENCES slice_stream(item_id),
                replacement_id TEXT UNIQUE NOT NULL REFERENCES slice_stream(item_id),
                known_at REAL NOT NULL
            );
            INSERT OR IGNORE INTO session_history(conversation_id,run_id)
                SELECT conversation_id,job_id FROM dispatch_jobs WHERE job_id=run_id ORDER BY seq;
            COMMIT;
            """
        )

    def _upgrade_input_messages(self) -> None:
        """v4 的输入位置和活动投递记录决定身份；用户输入中的封套只是原文。"""
        with self._transaction():
            # 另一连接可能已经在等锁期间完成这次升级。
            if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 5:
                return
            self._connection.execute(
                "ALTER TABLE activity_deliveries ADD COLUMN input_run_id TEXT "
                "REFERENCES dispatch_jobs(job_id)"
            )
            self._connection.execute(
                "ALTER TABLE activity_deliveries ADD COLUMN input_index INTEGER "
                "CHECK(input_index>=0)"
            )
            self._connection.execute(
                "CREATE UNIQUE INDEX activity_input_slot "
                "ON activity_deliveries(input_run_id,input_index)"
            )
            conversations = self._connection.execute(
                "SELECT DISTINCT conversation_id FROM session_history WHERE run_id IS NOT NULL"
            ).fetchall()
            for row in conversations:
                self._upgrade_conversation_inputs(row["conversation_id"])
            self._connection.execute("PRAGMA user_version=5")

    def _upgrade_conversation_inputs(self, conversation_id: str) -> None:
        # v4 仅保存了活动 applied 标志。非空闲投递是本会话可匹配的唯一来源。
        deliveries = {
            row["activity_id"]: row
            for row in self._connection.execute(
                "SELECT i.activity_id,i.seq,i.data FROM activity_deliveries d "
                "JOIN activity_items i ON i.seq=d.activity_seq WHERE d.conversation_id=? "
                "AND d.applied=1 AND NOT EXISTS(SELECT 1 FROM session_history h "
                "WHERE h.conversation_id=d.conversation_id AND h.activity_seq=d.activity_seq)",
                (conversation_id,),
            )
        }
        runs = self._connection.execute(
            "SELECT j.job_id,j.messages FROM session_history h "
            "JOIN dispatch_jobs j ON j.job_id=h.run_id WHERE h.conversation_id=? ORDER BY h.seq",
            (conversation_id,),
        ).fetchall()
        for run in runs:
            run_id = run["job_id"]
            events = {
                row["input_index"]: row["event_id"]
                for row in self._connection.execute(
                    "SELECT input_index,event_id FROM dispatch_jobs WHERE input_run_id=?", (run_id,)
                )
            }
            transcript = list(_messages_from_json(run["messages"]))
            for index, message in enumerate(transcript):
                if not isinstance(message, UserMessage):
                    continue
                if index in events:
                    transcript[index] = event_message(self.get_event(events[index]))
                elif message.source == "timeline":
                    # 仅已标记为 timeline 的旧消息有资格匹配。包内 ID 只作查找提示，
                    # 来源、时间、正文始终从本会话实际接收的持久活动项重新投影。
                    # 非标准 SDK 写入、找不到原始活动的旧消息仍保留原文。
                    transcript[index] = UserMessage(message.text)
                    lines = message.text.split("\n", 2)
                    if len(lines) != 3:
                        continue
                    try:
                        envelope = json.loads(lines[1])
                    except ValueError:
                        continue
                    item_id = envelope.get("activity_id") if isinstance(envelope, dict) else None
                    delivery = deliveries.pop(item_id, None) if isinstance(item_id, str) else None
                    if delivery is None:
                        continue
                    item = ActivityItem.model_validate_json(delivery["data"])
                    transcript[index] = activity_message(item)
                    self._connection.execute(
                        "UPDATE activity_deliveries SET input_run_id=?,input_index=? "
                        "WHERE conversation_id=? AND activity_seq=?",
                        (run_id, index, conversation_id, delivery["seq"]),
                    )
            self._connection.execute(
                "UPDATE dispatch_jobs SET messages=? WHERE job_id=?",
                (_messages_json(tuple(transcript)), run_id),
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

    def received_event(self, terminal_id: str, producer_event_id: str) -> EventRecord | None:
        """Resolve a terminal's previous submission before persisting a large upload."""
        self._ensure_open()
        row = self._connection.execute(
            "SELECT event_id FROM receipts WHERE terminal_id=? AND producer_event_id=?",
            (terminal_id, producer_event_id),
        ).fetchone()
        return self.get_event(row["event_id"]) if row is not None else None

    def _check_event_size(self, event: EventInput) -> None:
        encoded = _json(event.model_dump(mode="json"))
        if len(encoded.encode("utf-8")) > self.max_event_bytes:
            raise EventError("Event exceeds the configured byte limit")

    def acknowledge_desktop_snapshot(
        self,
        terminal_id: str,
        conversation_id: str,
        producer_event_id: str,
        value: JsonValue,
        occurred_at: datetime | None,
    ) -> tuple[str, bool]:
        """Keep just the collector's idempotency receipt, without retaining the window data."""
        event = EventInput(
            terminal_id=terminal_id,
            conversation_id=conversation_id,
            producer_event_id=producer_event_id,
            layer="perception",
            type="state.observed",
            payload={
                "subject": f"device:{terminal_id}",
                "state_key": SNAPSHOT_STATE_KEY,
                "value": value,
            },
            occurred_at=occurred_at or self._now(),
        )
        self._check_event_size(event)
        fingerprint = _event_fingerprint(event)
        with self._transaction():
            terminal = self.get_terminal(terminal_id)
            if "perception" not in terminal.allowed_layers:
                raise EventError("Terminal is not allowed to use this layer")
            self._check_scope(conversation_id, terminal.user_id)
            existing = self._connection.execute(
                "SELECT event_id,fingerprint FROM desktop_snapshot_receipts "
                "WHERE terminal_id=?1 AND producer_event_id=?2 UNION ALL "
                "SELECT event_id,fingerprint FROM receipts WHERE terminal_id=?1 "
                "AND producer_event_id=?2 LIMIT 1",
                (terminal_id, producer_event_id),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise DuplicateEventError("Producer event ID was reused with different content")
                return str(existing["event_id"]), True
            event_id = f"evt_{uuid4().hex}"
            self._connection.execute(
                "INSERT INTO desktop_snapshot_receipts VALUES(?,?,?,?,?)",
                (terminal_id, producer_event_id, conversation_id, event_id, fingerprint),
            )
            return event_id, False

    def append(self, event: EventInput) -> Receipt:
        # 冻结模型的嵌套 payload 仍可能被修改，因此持久化前重新验证快照。
        event = EventInput.model_validate(event.model_dump())
        self._check_event_size(event)
        fingerprint = _event_fingerprint(event)
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
            if self._connection.execute(
                "SELECT 1 FROM desktop_snapshot_receipts "
                "WHERE terminal_id=? AND producer_event_id=?",
                (event.terminal_id, event.producer_event_id),
            ).fetchone():
                raise DuplicateEventError(
                    "Producer event ID was already used by a desktop snapshot"
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
                        0,
                    ),
                )
            elif record.layer == "perception":
                if self._project_state(record):
                    self._project_activity(
                        perception_activity(record), source_seq=record.source_seq
                    )
            elif record.layer == "slice":
                self._connection.execute(
                    "INSERT INTO slice_processing(event_id) VALUES(?)", (record.event_id,)
                )
            return Receipt(event=record, duplicate=False, job_id=job_id)

    def _project_state(self, event: EventRecord) -> bool:
        subject = event.payload.get("subject")
        key = event.payload.get("state_key")
        if not isinstance(subject, str) or not isinstance(key, str) or not subject or not key:
            return True
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
                return False
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
        return True

    def _project_activity(
        self,
        item: ActivityItem,
        *,
        source_seq: int | None = None,
        screenshot_fact_bytes: int | None = None,
    ) -> None:
        """在接收或摘要提交事务内比较每个会话最近收到的同类活动。"""
        content = _json(body_value(item.content))
        if item.valid_until is not None:
            content = _json([body_value(item.content), timestamp_text(item.valid_until)])
        head = self._connection.execute(
            "SELECT source_seq,occurred_at,content FROM activity_heads "
            "WHERE user_id=? AND activity_key=?",
            (item.user_id, item.activity_key),
        ).fetchone()
        instant = item.occurred_at.timestamp()
        if head is not None and item.kind == "perception":
            if source_seq is not None and head["source_seq"] is not None:
                if source_seq <= head["source_seq"]:
                    return
            elif instant < head["occurred_at"]:
                return
        changed = head is None or head["content"] != content
        self._connection.execute(
            "INSERT INTO activity_heads(user_id,activity_key,source_seq,occurred_at,content) "
            "VALUES(?,?,?,?,?) ON CONFLICT(user_id,activity_key) DO UPDATE SET "
            "source_seq=excluded.source_seq,occurred_at=excluded.occurred_at,content=excluded.content",
            (item.user_id, item.activity_key, source_seq, instant, content),
        )
        targets = self._connection.execute(
            "SELECT c.conversation_id FROM conversations c WHERE user_id=? "
            "AND (? IS NULL OR c.conversation_id=?) AND (SELECT i.content "
            "FROM activity_deliveries d JOIN activity_items i ON i.seq=d.activity_seq "
            "WHERE d.conversation_id=c.conversation_id AND d.activity_key=? "
            "ORDER BY d.activity_seq DESC LIMIT 1) IS NOT ?",
            (item.user_id, item.conversation_id, item.conversation_id, item.activity_key, content),
        ).fetchall()
        if not changed and not targets:
            return
        cursor = self._connection.execute(
            "INSERT INTO activity_items(activity_id,after_receipt,user_id,activity_key,occurred_at,"
            "content,data,canonical_change) VALUES(?,(SELECT COALESCE(MAX(seq),0) FROM receipts),"
            "?,?,?,?,?,?)",
            (
                item.activity_id,
                item.user_id,
                item.activity_key,
                instant,
                content,
                item.model_dump_json(),
                changed,
            ),
        )
        for target in targets:
            conversation = target["conversation_id"]
            self._connection.execute(
                "INSERT INTO activity_deliveries(conversation_id,activity_seq,activity_key) "
                "VALUES(?,?,?)",
                (conversation, cursor.lastrowid, item.activity_key),
            )
        assert cursor.lastrowid is not None
        timeline_batches.append(
            self._connection, cursor.lastrowid, item, self.timeline_policy, screenshot_fact_bytes
        )

    def pending_timeline(self, conversation_id: str, user_id: str) -> UserMessage | None:
        """预览待发送时间线的变化，不确认投递或修改已有历史。"""
        self._ensure_open()
        standalone = not self._connection.in_transaction
        if standalone:
            self._connection.execute("BEGIN")
        try:
            self._check_scope(conversation_id, user_id)
            return timeline_batches.pending(
                self._connection, conversation_id, user_id, self.timeline_policy
            )
        finally:
            if standalone:
                self._connection.rollback()  # release the read snapshot

    def seal_idle_timeline_batches(self) -> None:
        with self._transaction():
            timeline_batches.seal_idle(
                self._connection, self.timeline_policy, self._now().timestamp()
            )

    def next_timeline_batch(self) -> timeline_batches.TimelineBatch | None:
        with self._transaction():
            now = self._now().timestamp()
            timeline_batches.seal_idle(self._connection, self.timeline_policy, now)
            if not self._prefer_timeline_rollup:
                leaf = timeline_batches.next_batch(self._connection, self.timeline_policy, now)
                if leaf is not None:
                    return leaf
            parent = timeline_rollups.next_rollup(self._connection, self.timeline_policy, now)
            if parent is not None:
                batch_id, text, attempts = parent
                return timeline_batches.TimelineBatch(-batch_id, text, attempts)
            if self._prefer_timeline_rollup:
                return timeline_batches.next_batch(self._connection, self.timeline_policy, now)
            return None

    def finish_timeline_batch(self, batch_id: int, text: str) -> None:
        """只提交冻结片段的摘要；不改变任何会话的投递位置。"""
        if not text.strip():
            raise ValueError("Timeline summary must not be empty")
        with self._transaction():
            if batch_id < 0:
                timeline_rollups.finish_rollup(self._connection, -batch_id, text)
            else:
                self._connection.execute(
                    "UPDATE timeline_batches SET ai_text=? WHERE batch_id=? "
                    "AND sealed=1 AND ai_text IS NULL",
                    (text, batch_id),
                )
            self._prefer_timeline_rollup = batch_id >= 0

    def retry_timeline_batch(self, batch_id: int, *, delay_seconds: float) -> None:
        with self._transaction():
            retry_at = self._now().timestamp() + delay_seconds
            if batch_id < 0:
                timeline_rollups.retry_rollup(self._connection, -batch_id, retry_at)
            else:
                self._connection.execute(
                    "UPDATE timeline_batches SET attempts=attempts+1,retry_at=? "
                    "WHERE batch_id=? AND sealed=1 AND ai_text IS NULL",
                    (retry_at, batch_id),
                )

    def list_activity(
        self, *, user_id: str, conversation_id: str | None = None, limit: int = 100
    ) -> tuple[ActivityItem, ...]:
        """最新 N 个变化按发生时间正序返回；可按会话查询其实际投递范围。"""
        self._ensure_open()
        self._check_scope(conversation_id, user_id)
        rows = self._connection.execute(
            "SELECT i.data FROM activity_items i WHERE i.user_id=? AND "
            "((? IS NULL AND i.canonical_change=1) OR EXISTS (SELECT 1 FROM activity_deliveries d "
            "WHERE d.activity_seq=i.seq AND d.conversation_id=?)) "
            "ORDER BY i.occurred_at DESC,i.seq DESC LIMIT ?",
            (user_id, conversation_id, conversation_id, self._limit(limit)),
        ).fetchall()
        return tuple(ActivityItem.model_validate_json(row["data"]) for row in reversed(rows))

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

    def has_desktop_snapshot(self, *, user_id: str, conversation_id: str, terminal_id: str) -> bool:
        """Bind legacy window-text recognition to a known collector in this conversation."""
        self._ensure_open()
        return (
            self._connection.execute(
                "SELECT 1 FROM desktop_snapshot_receipts r JOIN terminals t USING(terminal_id) "
                "WHERE t.user_id=? AND r.conversation_id=? AND r.terminal_id=? LIMIT 1",
                (user_id, conversation_id, terminal_id),
            ).fetchone()
            is not None
        )

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
            "SELECT e.data FROM slice_processing p INDEXED BY slice_waiting "
            "JOIN slice_events e ON p.event_id=e.event_id "
            "WHERE p.item_id IS NULL AND p.frame_done=0 AND (p.retry_at IS NULL OR p.retry_at<=?) "
            "ORDER BY e.seq LIMIT ?",
            (self._now().timestamp(), self._limit(limit)),
        ).fetchall()
        return tuple(EventRecord.model_validate_json(row["data"]) for row in rows)

    def has_unprocessed_slices(self) -> bool:
        """包含仍在等待重试的事件，避免将暂时无候选误判为处理完成。"""
        self._ensure_open()
        return (
            self._connection.execute(
                "SELECT 1 FROM slice_processing INDEXED BY slice_waiting "
                "WHERE item_id IS NULL AND frame_done=0 LIMIT 1"
            ).fetchone()
            is not None
        )

    def commit_slice(self, item: SliceStreamItem, *, owner_id: str | None = None) -> None:
        """原子保存摘要及来源；同一组来源的修订保留旧版及其历史。"""
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
            previous_ids: set[str | None] = set()
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
                if state is None:
                    raise EventError("Slice source is missing")
                previous_ids.add(state["item_id"])
                source_events.append(event)
            if len(previous_ids) != 1:
                raise EventError("Slice sources overlap previously processed groups")
            previous = previous_ids.pop()
            if previous is not None:
                stored = self._connection.execute(
                    "SELECT data FROM slice_stream WHERE item_id=?", (previous,)
                ).fetchone()
                assert stored is not None
                original = SliceStreamItem.model_validate_json(stored["data"])
                if set(original.source_event_ids) != set(item.source_event_ids):
                    raise EventError("Slice revision must include all previously processed sources")
            times = [event.occurred_at for event in source_events]
            if min(times) != item.started_at or max(times) != item.ended_at:
                raise EventError("Slice stream range must match its source event timestamps")
            self._write_slice(item, source_events, previous)
            if (
                len(source_events) == 1
                and source_events[0].type == "slice.image"
                and "capture" in source_events[0].payload
            ):
                screenshot_groups.record_existing_item(self._connection, source_events[0], item)

    def _write_slice(
        self,
        item: SliceStreamItem,
        source_events: list[EventRecord],
        previous: str | None = None,
        *,
        screenshot_fact_bytes: int | None = None,
    ) -> None:
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
        known_at = self._now()
        if previous is not None:
            self._connection.execute(
                "INSERT INTO slice_revisions(previous_id,replacement_id,known_at) VALUES(?,?,?)",
                (previous, item.item_id, known_at.timestamp()),
            )
        self._project_activity(
            slice_activity(item, source_events, known_at=known_at),
            screenshot_fact_bytes=screenshot_fact_bytes,
        )

    def preceding_screenshot(
        self, event: EventRecord, day: str
    ) -> screenshot_groups.PreviousScreenshot | None:
        self._ensure_open()
        return screenshot_groups.previous_capture(self._connection, event, day)

    def commit_screenshot(
        self,
        event: EventRecord,
        previous: screenshot_groups.PreviousScreenshot | None,
        observation: "ScreenshotObservation",
        *,
        timezone: str,
        text: str,
        owner_id: str | None = None,
    ) -> None:
        """Commit a processed capture, including unchanged frames that have no timeline item."""
        with self._transaction():
            if owner_id is not None:
                self._require_scheduler(owner_id)
            source = self.get_event(event.event_id, user_id=event.user_id)
            if source != event or event.type != "slice.image" or "capture" not in event.payload:
                raise EventError("Expected a stored periodic screenshot")
            status = self.get_slice_status(event.event_id)
            if status.status == "completed":
                return
            day = event.occurred_at.astimezone(ZoneInfo(timezone)).date().isoformat()
            # A lease change or second processor may have committed a preceding frame during
            # inference. Retry against the new facts instead of storing a stale comparison.
            if previous != self.preceding_screenshot(event, day):
                raise EventError("Screenshot reference changed during recognition")
            item_id: str | None = None
            if observation.facts:
                item = SliceStreamItem(
                    item_id=f"slice_{uuid4().hex}",
                    user_id=event.user_id,
                    conversation_id=event.conversation_id,
                    terminal_id=event.terminal_id,
                    source_id=event.source_id,
                    day=day,
                    timezone=timezone,
                    started_at=event.occurred_at,
                    ended_at=event.occurred_at,
                    source_event_ids=(event.event_id,),
                    text=text,
                    strategy="image_description",
                    created_at=self._now(),
                )
                self._write_slice(
                    item,
                    [event],
                    screenshot_fact_bytes=sum(
                        len(fact.encode("utf-8")) for fact in observation.facts
                    ),
                )
                item_id = item.item_id
            else:
                self._connection.execute(
                    "UPDATE slice_processing SET frame_done=1,attempts=attempts+1,"
                    "error_type=NULL,retry_at=NULL WHERE event_id=?",
                    (event.event_id,),
                )
            screenshot_groups.save_capture(
                self._connection, event, day, previous, observation, item_id
            )

    def get_audio_transcript(self, event_id: str, *, user_id: str | None = None) -> str | None:
        """返回完整转录；None 表示未转录，空字符串表示已确认静音。"""
        if self.get_event(event_id, user_id=user_id).type != "slice.audio":
            raise EventError("Only audio slices have transcripts")
        row = self._connection.execute(
            "SELECT text FROM audio_transcripts WHERE event_id=?", (event_id,)
        ).fetchone()
        return str(row["text"]) if row is not None else None

    def save_audio_transcript(
        self, event_id: str, text: str, *, owner_id: str | None = None
    ) -> None:
        """ASR 成功后独立提交，使总结失败和服务重启不会重新付费转录。"""
        if not isinstance(text, str):
            raise ValueError("Transcript must be text")
        with self._transaction():
            if owner_id is not None:
                self._require_scheduler(owner_id)
            existing = self.get_audio_transcript(event_id)
            if existing is not None:
                if existing != text:
                    raise EventError("Audio transcript is immutable")
                return
            self._connection.execute(
                "INSERT INTO audio_transcripts(event_id,text,created_at,user_id,conversation_id) "
                "SELECT event_id,?,?,user_id,conversation_id FROM slice_events WHERE event_id=?",
                (text, self._now().timestamp(), event_id),
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
                "WHERE event_id=? AND item_id IS NULL AND frame_done=0",
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
                if row["item_id"] is not None or row["frame_done"]
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
        previous = "v.previous_id=s.item_id"
        if as_of is not None:
            previous += " AND v.known_at<=?"
            where += (
                " AND NOT EXISTS (SELECT 1 FROM slice_revisions v "
                "WHERE v.replacement_id=s.item_id AND v.known_at>?)"
            )
            params.append(as_of.timestamp())
            params.append(as_of.timestamp())
        where += f" AND NOT EXISTS (SELECT 1 FROM slice_revisions v WHERE {previous})"
        rows = self._connection.execute(
            f"SELECT data FROM slice_stream s WHERE {where} "
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
                leaders = self._connection.execute(
                    "SELECT job_id FROM dispatch_jobs WHERE status='running' AND run_id=job_id"
                ).fetchall()
                for leader in leaders:
                    self._fail_run(
                        leader["job_id"],
                        "Previous scheduler stopped before recording completion",
                        (),
                        interrupted=True,
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
        self,
        excluded_conversations: Sequence[str] = (),
        *,
        owner_id: str | None = None,
        defer_input: bool = False,
    ) -> JobRecord | None:
        with self._transaction():
            self._require_scheduler(owner_id)
            excluded = ""
            if excluded_conversations:
                placeholders = ",".join("?" for _ in excluded_conversations)
                excluded = f" AND conversation_id NOT IN ({placeholders})"
            row = self._connection.execute(
                "SELECT job_id,event_id,input_run_id FROM dispatch_jobs WHERE status='queued' "
                "AND conversation_id NOT IN "
                "(SELECT conversation_id FROM dispatch_jobs WHERE status='running')"
                f"{excluded} ORDER BY seq LIMIT 1",
                tuple(excluded_conversations),
            ).fetchone()
            if row is None:
                return None
            job_id = row["job_id"]
            job = self.get_job(job_id)
            self._connection.execute(
                "INSERT INTO session_history(conversation_id,run_id) VALUES(?,?)",
                (job.conversation_id, job_id),
            )
            if row["input_run_id"] is None and not defer_input:
                initial = event_message(self.get_event(row["event_id"]))
                self._connection.execute(
                    "UPDATE dispatch_jobs SET input_run_id=job_id,input_index=0,messages=? "
                    "WHERE job_id=?",
                    (_messages_json((initial,)), job_id),
                )
            self._connection.execute(
                "UPDATE dispatch_jobs SET status='running',attempts=attempts+1,run_id=job_id "
                "WHERE job_id=?",
                (job_id,),
            )
            return self.get_job(row["job_id"])

    def job_input_applied(self, job_id: str) -> bool:
        """该 job 的输入是否已在另一个执行中进入历史。"""
        row = self._connection.execute(
            "SELECT input_run_id FROM dispatch_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise EventError(f"Unknown job: {job_id}")
        return row["input_run_id"] is not None and row["input_run_id"] != job_id

    def _leader(self, run_id: str) -> JobRecord:
        row = self._connection.execute(
            f"SELECT {_JOB_FIELDS} FROM dispatch_jobs WHERE job_id=? AND run_id=job_id "
            "AND status='running'",
            (run_id,),
        ).fetchone()
        if row is None:
            raise EventError("Run is no longer active")
        return JobRecord(**dict(row))

    def has_pending_inputs(self, run_id: str) -> bool:
        """只有交流或通知会中断生成、阻止尚未开始的工具。"""
        row = self._connection.execute(
            "SELECT 1 FROM dispatch_jobs r WHERE r.job_id=? AND r.status='running' "
            "AND r.run_id=r.job_id AND EXISTS(SELECT 1 FROM dispatch_jobs j "
            "WHERE j.conversation_id=r.conversation_id AND j.status='queued' "
            "AND j.input_run_id IS NULL)",
            (run_id,),
        ).fetchone()
        return row is not None

    def _write_prefix(self, run_id: str, messages: tuple[Message, ...]) -> None:
        saved = self._connection.execute(
            "SELECT messages FROM dispatch_jobs WHERE job_id=?",
            (run_id,),
        ).fetchone()
        assert saved is not None
        transcript = _messages_json(messages)
        previous = json.loads(_messages_json(_messages_from_json(saved["messages"])))
        if json.loads(transcript)[: len(previous)] != previous:
            raise EventError("Checkpoint would replace committed history")
        self._connection.execute(
            "UPDATE dispatch_jobs SET messages=? WHERE job_id=?",
            (transcript, run_id),
        )

    def absorb_session_inputs(
        self,
        run_id: str,
        *,
        messages: tuple[Message, ...],
        owner_id: str | None = None,
    ) -> tuple[EventRecord | UserMessage, ...]:
        with self._transaction():
            self._require_scheduler(owner_id)
            leader = self._leader(run_id)
            rows = self._connection.execute(
                "SELECT j.job_id,j.event_id "
                "FROM dispatch_jobs j JOIN receipts r ON r.event_id=j.event_id "
                "WHERE j.conversation_id=? AND (j.status='queued' OR j.job_id=?) "
                "AND j.input_run_id IS NULL ORDER BY r.seq",
                (leader.conversation_id, run_id),
            ).fetchall()
            timeline = self.pending_timeline(leader.conversation_id, leader.user_id)
            if (timeline or rows) and self._connection.execute(
                "SELECT 1 FROM tool_batches WHERE run_id=?", (run_id,)
            ).fetchone():
                raise EventError("Cannot insert input into an unfinished tool batch")
            target = self.get_event(leader.event_id).reply_terminal_id
            prefix = list(messages)
            inputs: list[EventRecord | UserMessage] = []
            timeline_batches.mark_read(self._connection, leader.conversation_id)
            self._connection.execute(
                "UPDATE activity_deliveries SET applied=1,input_run_id=?,input_index=? "
                "WHERE conversation_id=? AND applied=0",
                (run_id, len(prefix) if timeline else None, leader.conversation_id),
            )
            if timeline is not None:
                prefix.append(timeline)
                inputs.append(timeline)
            for row in rows:
                event = self.get_event(row["event_id"])
                same_target = event.reply_terminal_id == target
                self._connection.execute(
                    "UPDATE dispatch_jobs SET input_run_id=?,input_index=?,run_id=?,status=?,"
                    "attempts=attempts+? WHERE job_id=?",
                    (
                        run_id,
                        len(prefix),
                        run_id if same_target else None,
                        "running" if same_target else "queued",
                        int(same_target and row["job_id"] != run_id),
                        row["job_id"],
                    ),
                )
                prefix.append(event_message(event))
                inputs.append(event)
            if inputs:
                self._write_prefix(run_id, tuple(prefix))
            return tuple(inputs)

    def absorb_inputs(
        self, run_id: str, *, messages: tuple[Message, ...], owner_id: str | None = None
    ) -> tuple[JobRecord, ...]:
        """旧接口只返回带 job 的输入；会话完整前缀可通过 get_run_messages 取得。"""
        items = self.absorb_session_inputs(run_id, messages=messages, owner_id=owner_id)
        return tuple(
            self.get_job(
                self._connection.execute(
                    "SELECT job_id FROM receipts WHERE event_id=?", (item.event_id,)
                ).fetchone()["job_id"]
            )
            for item in items
            if isinstance(item, EventRecord)
        )

    def try_finish_reply(
        self,
        run_id: str,
        *,
        messages: tuple[Message, ...],
        response: str,
        owner_id: str | None = None,
    ) -> bool:
        return (
            self.finish_reply_attempt(
                run_id, messages=messages, response=response, owner_id=owner_id
            )
            == "completed"
        )

    def finish_reply_attempt(
        self,
        run_id: str,
        *,
        messages: tuple[Message, ...],
        response: str,
        check_activity: bool = True,
        owner_id: str | None = None,
    ) -> Literal["completed", "input", "timeline"]:
        """在同一事务内决定提交回复，或告知 Runner 哪种新输入需要重取。"""
        if not messages or not isinstance(messages[-1], AssistantMessage):
            raise EventError("A final answer must end with an assistant message")
        if messages[-1].tool_calls or messages[-1].text != response:
            raise EventError("Final response differs from candidate message")
        with self._transaction():
            self._require_scheduler(owner_id)
            leader = self._leader(run_id)
            if self.has_pending_inputs(run_id):
                return "input"
            if check_activity:
                if self.pending_timeline(leader.conversation_id, leader.user_id):
                    return "timeline"
                timeline_batches.mark_read(self._connection, leader.conversation_id)
                self._connection.execute(
                    "UPDATE activity_deliveries SET applied=1 "
                    "WHERE conversation_id=? AND applied=0",
                    (leader.conversation_id,),
                )
            target = self.get_event(leader.event_id).reply_terminal_id
            self._finish_run(leader, messages, response, target)
            return "completed"

    def register_tool_batch(
        self,
        run_id: str,
        *,
        messages: tuple[Message, ...],
        owner_id: str | None = None,
    ) -> None:
        if (
            not messages
            or not isinstance(messages[-1], AssistantMessage)
            or not messages[-1].tool_calls
        ):
            raise EventError("Tool batch must end with a complete declaration")
        declaration = messages[-1]
        with self._transaction():
            self._require_scheduler(owner_id)
            self._leader(run_id)
            self._write_prefix(run_id, messages[:-1])
            self._connection.execute(
                "INSERT INTO tool_batches(run_id,declaration) VALUES(?,?)",
                (run_id, _json([asdict(declaration)])),
            )
            self._connection.executemany(
                "INSERT INTO tool_batch_calls(run_id,call_id,name,state) VALUES(?,?,?,'declared')",
                ((run_id, call.id, call.name) for call in declaration.tool_calls),
            )

    def try_start_tool(self, run_id: str, call_id: str, *, owner_id: str | None = None) -> bool:
        with self._transaction():
            self._require_scheduler(owner_id)
            self._leader(run_id)
            if self.has_pending_inputs(run_id):
                return False
            updated = self._connection.execute(
                "UPDATE tool_batch_calls SET state='started' WHERE run_id=? "
                "AND call_id=? AND state='declared'",
                (run_id, call_id),
            )
            if updated.rowcount != 1:
                raise EventError("Tool is unknown or has already been started")
            return True

    def finish_tool(
        self,
        run_id: str,
        result: ToolResultMessage,
        *,
        owner_id: str | None = None,
    ) -> None:
        with self._transaction():
            self._require_scheduler(owner_id)
            self._leader(run_id)
            updated = self._connection.execute(
                "UPDATE tool_batch_calls SET state='settled',result=? WHERE run_id=? "
                "AND call_id=? AND name=? AND (state='started' OR (state='declared' AND ?))",
                (
                    _json([asdict(result)]),
                    run_id,
                    result.tool_call_id,
                    result.name,
                    result.is_error,
                ),
            )
            if updated.rowcount != 1:
                raise EventError("Tool result does not match a pending call")
            outstanding = self._connection.execute(
                "SELECT 1 FROM tool_batch_calls WHERE run_id=? AND state!='settled' LIMIT 1",
                (run_id,),
            ).fetchone()
            if outstanding is None:
                self._close_tool_batch(run_id)

    def _close_tool_batch(self, run_id: str) -> bool:
        batch = self._connection.execute(
            "SELECT declaration FROM tool_batches WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if batch is None:
            return False
        (declaration,) = _messages_from_json(batch["declaration"])
        assert isinstance(declaration, AssistantMessage)
        rows = self._connection.execute(
            "SELECT call_id,state,result FROM tool_batch_calls WHERE run_id=?",
            (run_id,),
        ).fetchall()
        calls = {row["call_id"]: row for row in rows}
        messages: list[Message] = [declaration]
        for call in declaration.tool_calls:
            row = calls[call.id]
            if row["state"] == "settled":
                messages.extend(_messages_from_json(row["result"]))
            else:
                reason = (
                    "Execution outcome unknown after interruption; inspect effects before retrying"
                    if row["state"] == "started"
                    else "Tool was not started before interruption"
                )
                messages.append(ToolResultMessage(call.id, call.name, reason, True))
        self._write_prefix(run_id, (*self.get_run_messages(run_id), *messages))
        self._connection.execute("DELETE FROM tool_batches WHERE run_id=?", (run_id,))
        return True

    def finish_job(
        self,
        job_id: str,
        *,
        messages: tuple[Message, ...],
        response: str,
        reply_terminal_id: str | None,
        owner_id: str | None = None,
    ) -> OutputRecord:
        with self._transaction():
            self._require_scheduler(owner_id)
            if messages:
                # 老 SDK 可以只回传用户原话。仅在同一已提交槽位且正文一致时
                # 补齐存储侧已认证的来源，其他改写仍由前缀检查拒绝。
                prefix = self.get_run_messages(job_id)
                supplied = list(messages)
                for index, (previous, current) in enumerate(zip(prefix, messages, strict=False)):
                    if (
                        isinstance(previous, UserMessage)
                        and isinstance(current, UserMessage)
                        and current == UserMessage(previous.text)
                    ):
                        supplied[index] = previous
                messages = tuple(supplied)
            return self._finish_run(self._leader(job_id), messages, response, reply_terminal_id)

    def _finish_run(
        self,
        job: JobRecord,
        messages: tuple[Message, ...],
        response: str,
        reply_terminal_id: str | None,
    ) -> OutputRecord:
        if reply_terminal_id is not None:
            if self.get_terminal(reply_terminal_id).user_id != job.user_id:
                raise EventError("Output terminal belongs to a different user")
        if self._connection.execute(
            "SELECT 1 FROM tool_batches WHERE run_id=?", (job.job_id,)
        ).fetchone():
            raise EventError("Cannot finish with unsettled tools")
        self._write_prefix(job.job_id, messages or self.get_run_messages(job.job_id))
        self._connection.execute(
            "UPDATE dispatch_jobs SET status='completed',error=NULL WHERE run_id=?",
            (job.job_id,),
        )
        self._history_bases.pop(job.job_id, None)
        output = OutputRecord(
            output_id=f"out_{uuid4().hex}",
            event_id=job.event_id,
            job_id=job.job_id,
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            terminal_id=reply_terminal_id,
            layer=job.layer,
            text=response,
        )
        self._connection.execute(
            "INSERT INTO outputs(output_id,event_id,job_id,user_id,conversation_id,terminal_id,"
            "layer,text) VALUES(?,?,?,?,?,?,?,?)",
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
        with self._transaction():
            self._require_scheduler(owner_id)
            job = self.get_job(job_id)
            if job.status == "queued":
                self._connection.execute(
                    "UPDATE dispatch_jobs SET run_id=job_id WHERE job_id=?",
                    (job_id,),
                )
                self._connection.execute(
                    "INSERT INTO session_history(conversation_id,run_id) VALUES(?,?)",
                    (job.conversation_id, job_id),
                )
            else:
                self._leader(job_id)
            self._fail_run(job_id, error, messages, interrupted=interrupted)

    def _fail_run(
        self,
        run_id: str,
        error: str,
        messages: tuple[Message, ...],
        *,
        interrupted: bool,
    ) -> None:
        # 工具可能已产生外部效果。恢复使用持久化的逐项回执，不能覆盖为笼统的取消占位。
        if not self._close_tool_batch(run_id) and messages:
            saved = self.get_run_messages(run_id)
            if messages[: len(saved)] == saved:
                self._write_prefix(run_id, messages)
        self._connection.execute(
            "UPDATE dispatch_jobs SET status=?,error=? WHERE run_id=?",
            ("interrupted" if interrupted else "failed", error, run_id),
        )
        self._history_bases.pop(run_id, None)
        # 受控运行在第一次取时间线/触发消息之前中止时，仍记录原始用户输入。
        row = self._connection.execute(
            "SELECT event_id FROM dispatch_jobs WHERE job_id=? AND input_run_id IS NULL",
            (run_id,),
        ).fetchone()
        if row:
            prefix = self.get_run_messages(run_id)
            self._connection.execute(
                "UPDATE dispatch_jobs SET input_run_id=job_id,input_index=? WHERE job_id=?",
                (len(prefix), run_id),
            )
            self._write_prefix(run_id, (*prefix, event_message(self.get_event(row["event_id"]))))

    def search_activity(
        self,
        *,
        user_id: str,
        conversation_id: str,
        start: datetime,
        end: datetime,
        terminal: str | None = None,
        keyword: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[tuple[ActivityItem, ...], bool]:
        """按发生时间正序检索本会话可见的原始感知和切片文字。"""
        self._check_scope(conversation_id, user_id)
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise ValueError("Expected a nonempty timezone-aware time interval")
        if not 1 <= limit <= 20 or offset < 0:
            raise ValueError("Invalid activity page")
        rows = self._connection.execute(
            "SELECT i.data FROM activity_items i INDEXED BY activity_timeline "
            "JOIN activity_deliveries d ON d.activity_seq=i.seq AND d.conversation_id=? "
            "WHERE i.user_id=? AND i.occurred_at>=? AND i.occurred_at<? "
            "AND (? IS NULL OR json_extract(i.data,'$.terminal_id')=?) "
            "AND (? IS NULL OR instr(lower(i.content),lower(?))>0) "
            "AND NOT EXISTS(SELECT 1 FROM desktop_snapshot_receipts s "
            "WHERE s.event_id=i.activity_id) "
            "AND NOT EXISTS(SELECT 1 FROM slice_revisions r WHERE r.previous_id=i.activity_id) "
            "ORDER BY i.occurred_at,i.seq LIMIT ? OFFSET ?",
            (
                conversation_id,
                user_id,
                start.timestamp(),
                end.timestamp(),
                terminal,
                terminal,
                keyword,
                keyword,
                limit + 1,
                offset,
            ),
        ).fetchall()
        items = tuple(ActivityItem.model_validate_json(r[0]) for r in rows[:limit])
        return items, len(rows) > limit

    def browse_history(
        self,
        *,
        user_id: str,
        conversation_id: str,
        view: str,
        cursor: str | None = None,
        limit: int = 50,
        grouped: bool = False,
    ) -> tuple[list[HistoryItem], str | None]:
        self._check_scope(conversation_id, user_id)
        return browse(
            self._connection, user_id, conversation_id, view, cursor, limit, grouped=grouped
        )

    def browse_screenshot_group(
        self,
        *,
        user_id: str,
        conversation_id: str,
        group_id: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[HistoryItem], str | None]:
        self._check_scope(conversation_id, user_id)
        return screenshot_frames(
            self._connection, user_id, conversation_id, group_id, cursor, limit
        )

    def browse_compression_batches(
        self,
        *,
        user_id: str,
        conversation_id: str,
        layer: str,
        cursor: str | None,
        limit: int,
        ai_enabled: bool,
    ) -> dict[str, Any]:
        self._check_scope(conversation_id, user_id)
        return compression_history.browse_batches(
            self._connection,
            user_id,
            conversation_id,
            layer,
            cursor,
            limit,
            self.timeline_policy,
            ai_enabled,
        )

    def browse_compression_batch(
        self,
        *,
        user_id: str,
        conversation_id: str,
        batch_id: str,
        cursor: str | None,
        limit: int,
        ai_enabled: bool,
    ) -> dict[str, Any]:
        self._check_scope(conversation_id, user_id)
        return compression_history.batch_detail(
            self._connection,
            user_id,
            conversation_id,
            batch_id,
            cursor,
            limit,
            self.timeline_policy,
            ai_enabled,
        )

    def browse_compression_sources(
        self,
        *,
        user_id: str,
        conversation_id: str,
        source_id: str,
        activity: bool,
        cursor: str | None,
        limit: int,
    ) -> compression_sources.SourcePage:
        self._check_scope(conversation_id, user_id)
        return compression_sources.browse_sources(
            self._connection,
            user_id,
            conversation_id,
            source_id,
            activity=activity,
            cursor=cursor,
            limit=limit,
        )

    def history(
        self, conversation_id: str, user_id: str, *, exclude_run_id: str | None = None
    ) -> tuple[Message, ...]:
        """读取会话已提交的消息；装配本轮初始上下文时可排除本轮。"""
        self._ensure_open()
        self._check_scope(conversation_id, user_id)
        rows = self._connection.execute(
            "SELECT j.messages,i.data FROM session_history h "
            "LEFT JOIN dispatch_jobs j ON j.job_id=h.run_id "
            "LEFT JOIN activity_items i ON i.seq=h.activity_seq "
            "WHERE h.conversation_id=? AND (? IS NULL OR h.run_id IS NOT ?) ORDER BY h.seq",
            (conversation_id, exclude_run_id, exclude_run_id),
        ).fetchall()
        history: list[Message] = []
        for row in rows:
            if row["data"] is None:
                history.extend(_messages_from_json(row["messages"]))
            else:
                history.append(activity_message(ActivityItem.model_validate_json(row["data"])))
        return tuple(history)

    def get_run_messages(self, job_id: str, *, user_id: str | None = None) -> tuple[Message, ...]:
        """由任一关联 job 查询本次实际执行的唯一消息序列。"""
        self.get_job(job_id, user_id=user_id)
        row = self._connection.execute(
            "SELECT COALESCE(r.messages,'[]') AS messages FROM dispatch_jobs j "
            "LEFT JOIN dispatch_jobs r ON j.run_id=r.job_id WHERE j.job_id=?",
            (job_id,),
        ).fetchone()
        assert row is not None
        return _messages_from_json(row["messages"])

    def load_model_read_count(self, conversation_id: str, user_id: str) -> int:
        """返回该会话已由主模型确认的连续原始消息数。"""
        self._check_scope(conversation_id, user_id)
        row = self._connection.execute(
            "SELECT read_count FROM session_model_state WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return int(row["read_count"]) if row is not None else 0

    def load_context_checkpoint(
        self, conversation_id: str, user_id: str
    ) -> ContextCheckpoint | None:
        self._check_scope(conversation_id, user_id)
        row = self._connection.execute(
            "SELECT summary,summarized_messages,prefix_digest,compactions "
            "FROM session_model_state WHERE conversation_id=? AND summarized_messages>0",
            (conversation_id,),
        ).fetchone()
        return ContextCheckpoint(**dict(row)) if row is not None else None

    def record_model_read(
        self,
        run_id: str,
        read_count: int,
        checkpoint: ContextCheckpoint | None,
        *,
        messages: tuple[Message, ...],
        owner_id: str | None = None,
    ) -> None:
        """仅由普通推理完成时调用。当前运行的原文、已读位置和新摘要一起提交。"""
        if type(read_count) is not int or read_count < 0:
            raise EventError("Model read count must be a nonnegative integer")
        with self._transaction():
            self._require_scheduler(owner_id)
            leader = self._leader(run_id)
            self._write_prefix(run_id, messages)
            # 同会话只有一个活跃运行。运行之前的历史长度固定，工具循环只需数当前前缀。
            if run_id not in self._history_bases:
                self._history_bases[run_id] = self._connection.execute(
                    "SELECT COALESCE(SUM(COALESCE(json_array_length(j.messages),1)),0) "
                    "FROM session_history h LEFT JOIN dispatch_jobs j ON j.job_id=h.run_id "
                    "WHERE h.conversation_id=? AND h.seq<(SELECT seq FROM session_history "
                    "WHERE run_id=?)",
                    (leader.conversation_id, run_id),
                ).fetchone()[0]
            if read_count > self._history_bases[run_id] + len(messages):
                raise EventError("Model read count exceeds stored conversation history")
            self._connection.execute(
                "INSERT INTO session_model_state(conversation_id,read_count) VALUES(?,?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET read_count=excluded.read_count "
                "WHERE excluded.read_count>read_count",
                (leader.conversation_id, read_count),
            )
            if checkpoint is None:
                return
            cut = checkpoint.summarized_messages
            if cut > read_count:
                return
            previous = self._connection.execute(
                "SELECT summarized_messages,compactions,read_count FROM session_model_state "
                "WHERE conversation_id=?",
                (leader.conversation_id,),
            ).fetchone()
            if read_count < previous["read_count"] or (
                cut,
                checkpoint.compactions,
            ) <= (previous["summarized_messages"], previous["compactions"]):
                return
            # transform_context 可能改变 SDK 侧消息顺序和内容。只保存能在会话原文上重建的摘要。
            history = self.history(leader.conversation_id, leader.user_id)
            if cut > len(history) or history_digest(history[:cut]) != checkpoint.prefix_digest:
                return
            self._connection.execute(
                "UPDATE session_model_state SET summary=?,summarized_messages=?,"
                "prefix_digest=?,compactions=? WHERE conversation_id=?",
                (
                    checkpoint.summary,
                    cut,
                    checkpoint.prefix_digest,
                    checkpoint.compactions,
                    leader.conversation_id,
                ),
            )

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
        ):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        if job_id is not None:
            where.append("job_id=(SELECT run_id FROM dispatch_jobs WHERE job_id=?)")
            params.append(job_id)
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
