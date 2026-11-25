"""数据库结构初始化与历史数据升级；仅在打开存储时运行。"""

import json
from typing import TYPE_CHECKING

from purin.agent.core.types import (
    UserMessage,
)
from purin.events.core.activity import (
    ActivityItem,
    activity_message,
)
from purin.events.core.types import (
    EventError,
    event_message,
)
from purin.events.storage import (
    context_writes,
    merged_timeline,
    notifications,
    perception_buffer,
    profiles,
    scheduled_notifications,
    schedules,
    screenshot_groups,
    timeline_batches,
)
from purin.events.storage.codec import (
    EVENT_TABLES,
    decode_messages,
    encode_messages,
)
from purin.events.storage.history import (
    upgrade_dialogue_storage,
    upgrade_history_storage,
)
from purin.events.storage.repositories import remote_tools

if TYPE_CHECKING:
    from purin.events.storage.sqlite import SQLiteStore


class SchemaManager:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def initialize(self) -> None:
        version, has_tables = self._store._connection.execute(
            "SELECT user_version, EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name NOT GLOB 'sqlite_*') FROM pragma_user_version"
        ).fetchone()
        if version == 40:
            return
        if version > 40:
            raise EventError(f"Unsupported database version: {version}")
        if version < 37:
            self._initialize_through_37(version, has_tables)
        if version < 38:
            self._upgrade_outputs()
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] == 38:
                scheduled_notifications.initialize(self._store._connection)
                self._store._connection.execute("PRAGMA user_version=39")
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] == 39:
                notifications.add_notification_time(self._store._connection)
                self._store._connection.execute("PRAGMA user_version=40")

    def _initialize_through_37(self, version: int, has_tables: bool) -> None:
        if version != 36:
            self._initialize_through_36(version, has_tables)
        db = self._store._connection
        with self._store._transaction():
            if db.execute("PRAGMA user_version").fetchone()[0] != 36:
                return
            if db.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='http_device_credentials'"
            ).fetchone():
                db.execute(
                    "UPDATE http_device_credentials SET "
                    "binding=json_remove(binding, '$.manage_api_keys') "
                    "WHERE json_type(binding, '$.manage_api_keys') IS NOT NULL"
                )
            db.execute("PRAGMA user_version=37")

    def _upgrade_outputs(self) -> None:
        # Rebuild the parent table to remove the legacy UNIQUE(job_id). Keep output IDs
        # and sequence numbers so dialogue references, delivery acknowledgements and order survive.
        db = self._store._connection
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            with self._store._transaction():
                if db.execute("PRAGMA user_version").fetchone()[0] != 37:
                    return
                db.execute(
                    "CREATE TABLE outputs_new (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "output_id TEXT UNIQUE NOT NULL,"
                    "event_id TEXT NOT NULL REFERENCES receipts(event_id),"
                    "job_id TEXT NOT NULL REFERENCES dispatch_jobs(job_id),user_id TEXT NOT NULL,"
                    "conversation_id TEXT NOT NULL,device_id TEXT,layer TEXT NOT NULL,"
                    "text TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',error TEXT,"
                    "is_final INTEGER NOT NULL DEFAULT 1 CHECK(is_final IN (0,1)))"
                )
                columns = (
                    "seq,output_id,event_id,job_id,user_id,conversation_id,device_id,"
                    "layer,text,status,error"
                )
                db.execute(f"INSERT INTO outputs_new({columns}) SELECT {columns} FROM outputs")
                db.execute("DROP TABLE outputs")
                db.execute("ALTER TABLE outputs_new RENAME TO outputs")
                db.execute("CREATE INDEX history_output_event ON outputs(event_id)")
                db.execute("CREATE INDEX outputs_job ON outputs(job_id)")
                db.execute(
                    "CREATE UNIQUE INDEX final_output_job ON outputs(job_id) WHERE is_final=1"
                )
                if (
                    db.execute("PRAGMA foreign_key_check(outputs)").fetchone()
                    or db.execute("PRAGMA foreign_key_check(dialogue_entries)").fetchone()
                ):
                    raise EventError("Could not preserve output references during upgrade")
                db.execute("PRAGMA user_version=38")
        finally:
            db.execute("PRAGMA foreign_keys=ON")

    def _initialize_through_36(self, version: int, has_tables: bool) -> None:
        if version in range(3, 36):
            self._rename_device_identity()
        if version == 35:
            return
        if version in (32, 33, 34):
            self._upgrade_merged_timeline()
            return
        if version not in (19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31):
            if version != 18:
                self._initialize_through_desktop(version, has_tables)
            with self._store._transaction():
                remote_tools.initialize(self._store._connection)
                self._store._connection.execute("PRAGMA user_version=19")
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] >= 32:
                return
            perception_buffer.add_dwell_threshold(self._store._connection)
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] < 20:
                perception_buffer.initialize(
                    self._store._connection, self._store.timeline._perception_line
                )
            self._store._connection.execute(
                "CREATE TABLE IF NOT EXISTS timeline_input_times("
                "run_id TEXT NOT NULL REFERENCES dispatch_jobs(job_id),"
                "input_index INTEGER NOT NULL,"
                "written_at TEXT NOT NULL,PRIMARY KEY(run_id,input_index))"
            )
            perception_buffer.initialize_summary_history(self._store._connection)
            perception_buffer.initialize_sources(self._store._connection)
            if "filter_reason" not in {
                col[1]
                for col in self._store._connection.execute("PRAGMA table_info(perception_events)")
            }:
                self._store._connection.execute(
                    "ALTER TABLE perception_events ADD COLUMN filter_reason TEXT"
                )
            if "original_summary" not in {
                col[1]
                for col in self._store._connection.execute("PRAGMA table_info(perception_rounds)")
            }:
                self._store._connection.execute(
                    "ALTER TABLE perception_rounds ADD COLUMN original_summary TEXT"
                )
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] < 27:
                perception_buffer.migrate_token_counts(self._store._connection)
            perception_buffer.migrate_window_text(self._store._connection)
            self._store._connection.execute("PRAGMA user_version=32")
        self._upgrade_merged_timeline()

    def _upgrade_merged_timeline(self) -> None:
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] == 32:
                merged_timeline.initialize(self._store._connection, self._store.timeline_policy)
                self._store._connection.execute("PRAGMA user_version=33")
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] == 33:
                self._store._connection.execute(
                    "ALTER TABLE merged_batches ADD COLUMN generated_at INTEGER"
                )
                self._store._connection.execute(
                    "CREATE INDEX merged_history_order ON merged_batches("
                    "room,COALESCE(generated_at,end),last_seq)"
                )
                self._store._connection.execute("PRAGMA user_version=34")
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] == 34:
                context_writes.initialize(self._store._connection)
                self._store._connection.execute("PRAGMA user_version=36")

    def _rename_device_identity(self) -> None:
        db = self._store._connection
        with self._store._transaction():
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if "terminals" in tables:
                db.execute("ALTER TABLE terminals RENAME TO devices")
                tables.add("devices")
                # SQLite rewrites foreign keys and ordinary indexes on RENAME COLUMN.
                for table in (
                    "devices",
                    "receipts",
                    "outputs",
                    "client_devices",
                    "record_batches",
                    "desktop_frame_uploads",
                    "remote_tool_calls",
                    "notification_deliveries",
                    "perception_state",
                    "screenshot_groups",
                    "timeline_batches",
                    "timeline_rollups",
                    "user_questions",
                ):
                    if table not in tables:
                        continue
                    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                    for old in ("terminal_id", "reply_terminal_id", "answered_terminal_id"):
                        if old in columns:
                            new = old.replace("terminal_id", "device_id")
                            db.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
                # Only rewrite known envelope fields, preserving arbitrary files, payloads and text.
                for table in ("devices", *EVENT_TABLES.values(), "slice_stream", "activity_items"):
                    if table not in tables:
                        continue
                    for old, new in (
                        ("terminal_id", "device_id"),
                        ("reply_terminal_id", "reply_device_id"),
                    ):
                        db.execute(
                            f"UPDATE {table} SET data=json_remove(json_set(data, ?, "
                            "json_extract(data, ?)), ?) WHERE json_type(data, ?) IS NOT NULL",
                            (f"$.{new}", f"$.{old}", f"$.{old}", f"$.{old}"),
                        )
                if "notification_events" in tables:
                    db.execute(
                        "UPDATE notification_events SET data=json_remove(json_set(data, "
                        "'$.payload.answered_device_id',"
                        "json_extract(data,'$.payload.answered_terminal_id')), "
                        "'$.payload.answered_terminal_id') WHERE json_extract(data,'$.type') IN "
                        "('question.answered','question.dismissed') "
                        "AND json_extract(data,'$.source_id')='ask_user' "
                        "AND json_type(data,'$.payload.answered_terminal_id') IS NOT NULL"
                    )
                # SQL identifiers are updated by SQLite, JSON paths inside index literals are not.
                if db.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='screenshot_preceding'"
                ).fetchone():
                    db.execute("DROP INDEX screenshot_preceding")
                    screenshot_groups.create_preceding_index(db)
            if db.execute("PRAGMA user_version").fetchone()[0] == 35:
                db.execute("PRAGMA user_version=36")

    def _initialize_through_desktop(self, version: int, has_tables: bool) -> None:
        if version not in (16, 17):
            self._initialize_through_profiles(version, has_tables)
        with self._store._transaction():
            self._store._connection.execute(
                "CREATE TABLE IF NOT EXISTS desktop_frame_uploads (user_id TEXT NOT NULL,"
                "device_id TEXT NOT NULL,producer_event_id TEXT NOT NULL,"
                "fingerprint TEXT NOT NULL,PRIMARY KEY(device_id,producer_event_id))"
            )
            self._store._connection.execute(
                "CREATE TABLE IF NOT EXISTS desktop_vision_queue ("
                "event_id TEXT NOT NULL REFERENCES perception_events(event_id),"
                "frame_index INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,"
                "retry_at REAL NOT NULL DEFAULT 0,PRIMARY KEY(event_id,frame_index))"
            )
            self._store._connection.execute(
                "CREATE INDEX IF NOT EXISTS desktop_vision_due ON desktop_vision_queue(retry_at)"
            )
            self._store._connection.execute("DROP TABLE IF EXISTS desktop_snapshot_receipts")
            self._store._connection.execute(
                "DELETE FROM slice_processing WHERE item_id IS NULL AND frame_done=0 "
                "AND event_id IN (SELECT event_id FROM slice_events "
                "WHERE json_extract(data,'$.type')='slice.image' "
                "AND COALESCE(json_extract(data,'$.payload.capture.platform'),'') "
                "NOT IN ('android','ios'))"
            )
            self._store._connection.execute("PRAGMA user_version=18")

    def _initialize_through_profiles(self, version: int, has_tables: bool) -> None:
        if version == 15:
            return
        if version == 14:
            self._upgrade_profiles()
            return
        if version == 13:
            self._upgrade_notifications()
            self._upgrade_profiles()
            return
        if version in (11, 12):
            if version == 11:
                self._upgrade_schedules()
            self._upgrade_dialogue()
            self._upgrade_notifications()
            self._upgrade_profiles()
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
            self._upgrade_continuous_timeline()
            self._upgrade_screenshot_groups()
            self._upgrade_schedules()
            self._upgrade_dialogue()
            self._upgrade_notifications()
            self._upgrade_profiles()
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
            for table in EVENT_TABLES.values()
        )
        self._store._connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                device_id TEXT NOT NULL,
                producer_event_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                layer TEXT NOT NULL,
                job_id TEXT,
                UNIQUE(device_id,producer_event_id)
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
                device_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                state_key TEXT NOT NULL,
                event_id TEXT NOT NULL REFERENCES receipts(event_id),
                valid_until REAL,
                PRIMARY KEY(user_id,conversation_scope,device_id,source_id,subject,state_key)
            );
            CREATE TABLE IF NOT EXISTS outputs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                output_id TEXT UNIQUE NOT NULL,
                event_id TEXT NOT NULL REFERENCES receipts(event_id),
                job_id TEXT UNIQUE NOT NULL REFERENCES dispatch_jobs(job_id),
                user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                device_id TEXT,
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
        self._upgrade_continuous_timeline()
        self._upgrade_screenshot_groups()
        self._upgrade_schedules()
        self._upgrade_dialogue()
        self._upgrade_notifications()
        self._upgrade_profiles()

    def _upgrade_profiles(self) -> None:
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] < 15:
                profiles.initialize(self._store._connection)
                self._store._connection.execute("PRAGMA user_version=15")

    def _upgrade_notifications(self) -> None:
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] < 14:
                notifications.initialize(self._store._connection)
                self._store._connection.execute("PRAGMA user_version=14")

    def _upgrade_dialogue(self) -> None:
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] >= 13:
                return
            upgrade_dialogue_storage(self._store._connection)
            self._store._connection.execute("PRAGMA user_version=13")

    def _upgrade_schedules(self) -> None:
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] >= 12:
                return
            schedules.initialize(self._store._connection)
            self._store._connection.execute("PRAGMA user_version=12")

    def _upgrade_screenshot_groups(self) -> None:
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] >= 11:
                return
            timeline_batches.enable_screenshot_threshold(self._store._connection)
            screenshot_groups.initialize(self._store._connection)
            self._store._connection.execute("PRAGMA user_version=11")

    def _upgrade_continuous_timeline(self) -> None:
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] >= 10:
                return
            timeline_batches.initialize(self._store._connection, self._store.timeline_policy)
            self._store._connection.execute("PRAGMA user_version=10")

    def _upgrade_history_browser(self) -> None:
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] >= 8:
                return
            upgrade_history_storage(self._store._connection)
            self._store._connection.execute("PRAGMA user_version=8")

    def _upgrade_model_checkpoints(self) -> None:
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] >= 7:
                return
            self._store._connection.execute(
                "CREATE TABLE IF NOT EXISTS session_model_state ("
                "conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),"
                "read_count INTEGER NOT NULL CHECK(read_count>=0),"
                "summary TEXT NOT NULL DEFAULT '',"
                "summarized_messages INTEGER NOT NULL DEFAULT 0 "
                "CHECK(summarized_messages BETWEEN 0 AND read_count),"
                "prefix_digest TEXT NOT NULL DEFAULT '',compactions INTEGER NOT NULL DEFAULT 0)"
            )
            # 旧版只能从完成的主运行推断已处理边界；其后失败或中断的输入仍须原样发送。
            self._store._connection.execute(
                "INSERT OR IGNORE INTO session_model_state(conversation_id,read_count) "
                "SELECT h.conversation_id,SUM(COALESCE(json_array_length(j.messages),1)) "
                "FROM (SELECT s.conversation_id,MAX(s.seq) AS last_completed "
                "FROM session_history s JOIN dispatch_jobs r ON r.job_id=s.run_id "
                "WHERE r.status='completed' GROUP BY s.conversation_id) b "
                "JOIN session_history h ON h.conversation_id=b.conversation_id "
                "AND h.seq<=b.last_completed "
                "LEFT JOIN dispatch_jobs j ON j.job_id=h.run_id GROUP BY h.conversation_id"
            )
            self._store._connection.execute("PRAGMA user_version=7")

    def _upgrade_timeline_batches(self) -> None:
        with self._store._transaction():
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] >= 6:
                return
            # 多个来源项可以在同一模型输入槽位被确认。
            self._store._connection.execute("DROP INDEX activity_input_slot")
            self._store._connection.execute(
                "CREATE INDEX activity_input_slot ON activity_deliveries(input_run_id,input_index)"
            )
            self._store._connection.execute("PRAGMA user_version=6")

    def _initialize_activity(self) -> None:
        # v3 的会话历史只补上排序位置；已有观测和摘要不回填到会话。
        self._store._connection.executescript(
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
        with self._store._transaction():
            # 另一连接可能已经在等锁期间完成这次升级。
            if self._store._connection.execute("PRAGMA user_version").fetchone()[0] >= 5:
                return
            self._store._connection.execute(
                "ALTER TABLE activity_deliveries ADD COLUMN input_run_id TEXT "
                "REFERENCES dispatch_jobs(job_id)"
            )
            self._store._connection.execute(
                "ALTER TABLE activity_deliveries ADD COLUMN input_index INTEGER "
                "CHECK(input_index>=0)"
            )
            self._store._connection.execute(
                "CREATE UNIQUE INDEX activity_input_slot "
                "ON activity_deliveries(input_run_id,input_index)"
            )
            conversations = self._store._connection.execute(
                "SELECT DISTINCT conversation_id FROM session_history WHERE run_id IS NOT NULL"
            ).fetchall()
            for row in conversations:
                self._upgrade_conversation_inputs(row["conversation_id"])
            self._store._connection.execute("PRAGMA user_version=5")

    def _upgrade_conversation_inputs(self, conversation_id: str) -> None:
        # v4 仅保存了活动 applied 标志。非空闲投递是本会话可匹配的唯一来源。
        deliveries = {
            row["activity_id"]: row
            for row in self._store._connection.execute(
                "SELECT i.activity_id,i.seq,i.data FROM activity_deliveries d "
                "JOIN activity_items i ON i.seq=d.activity_seq WHERE d.conversation_id=? "
                "AND d.applied=1 AND NOT EXISTS(SELECT 1 FROM session_history h "
                "WHERE h.conversation_id=d.conversation_id AND h.activity_seq=d.activity_seq)",
                (conversation_id,),
            )
        }
        runs = self._store._connection.execute(
            "SELECT j.job_id,j.messages FROM session_history h "
            "JOIN dispatch_jobs j ON j.job_id=h.run_id WHERE h.conversation_id=? ORDER BY h.seq",
            (conversation_id,),
        ).fetchall()
        for run in runs:
            run_id = run["job_id"]
            events = {
                row["input_index"]: row["event_id"]
                for row in self._store._connection.execute(
                    "SELECT input_index,event_id FROM dispatch_jobs WHERE input_run_id=?", (run_id,)
                )
            }
            transcript = list(decode_messages(run["messages"]))
            for index, message in enumerate(transcript):
                if not isinstance(message, UserMessage):
                    continue
                if index in events:
                    transcript[index] = event_message(self._store.get_event(events[index]))
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
                    self._store._connection.execute(
                        "UPDATE activity_deliveries SET input_run_id=?,input_index=? "
                        "WHERE conversation_id=? AND activity_seq=?",
                        (run_id, index, conversation_id, delivery["seq"]),
                    )
            self._store._connection.execute(
                "UPDATE dispatch_jobs SET messages=? WHERE job_id=?",
                (encode_messages(tuple(transcript)), run_id),
            )
