"""SQLite 连接、事务、设备身份与原子事件入口。"""

import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from pydantic import ValidationError

from wintermute.events.core.activity import (
    MOBILE_USAGE_STATE_KEY,
    perception_activity,
)
from wintermute.events.core.mobile_location import LOCATION_STATE_KEY, LocationFix
from wintermute.events.core.perception_rules import (
    SHORT_DWELL_SECONDS,
    filter_window,
    is_mobile_home_activity,
    is_wintermute_activity,
)
from wintermute.events.core.timeline_compression import TimelinePolicy
from wintermute.events.core.types import (
    Device,
    DuplicateEventError,
    EventError,
    EventInput,
    EventRecord,
    Layer,
    Receipt,
    ScreenshotContext,
    utc_now,
)
from wintermute.events.storage.codec import (
    EVENT_TABLES,
    encode_json,
    event_fingerprint,
)
from wintermute.events.storage.repositories import connectors, questions, records, remote_tools
from wintermute.events.storage.repositories.chat_files import ChatFileStore
from wintermute.events.storage.repositories.desktop import DesktopStore
from wintermute.events.storage.repositories.devices import DeviceStore
from wintermute.events.storage.repositories.history import HistoryStore
from wintermute.events.storage.repositories.holidays import HolidayYearStore
from wintermute.events.storage.repositories.memory import MemoryStore
from wintermute.events.storage.repositories.notifications import NotificationStore
from wintermute.events.storage.repositories.outputs import OutputStore
from wintermute.events.storage.repositories.schedules import ScheduleStore
from wintermute.events.storage.repositories.sessions import SessionStore
from wintermute.events.storage.repositories.slices import SliceStore
from wintermute.events.storage.repositories.timeline import TimelineStore
from wintermute.events.storage.schema import SchemaManager


class SQLiteStore:
    """拥有共享连接；各业务仓储在同一连接上提交短事务。"""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        max_event_bytes: int = 256 * 1024,
        clock: Callable[[], datetime] | None = None,
        timeline_policy: TimelinePolicy | None = None,
        records_storage_path: str | Path | None = None,
    ) -> None:
        if (
            isinstance(max_event_bytes, bool)
            or not isinstance(max_event_bytes, int)
            or max_event_bytes < 1
        ):
            raise ValueError("max_event_bytes must be positive")
        self.max_event_bytes = max_event_bytes
        self.timeline_policy = timeline_policy or TimelinePolicy()
        self._clock = clock or utc_now
        self._closed = False
        self._connection = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self._connection.row_factory = sqlite3.Row
        try:
            db_file = self._connection.execute("PRAGMA database_list").fetchone()["file"]
            self._database_path = Path(db_file) if db_file else None
            self._connection.execute("PRAGMA foreign_keys=ON")
            self.memory = MemoryStore(self)
            self.notifications = NotificationStore(self)
            self.schedules = ScheduleStore(self)
            self.holiday_years = HolidayYearStore(self._connection)
            self.desktop = DesktopStore(self)
            self.devices = DeviceStore(self)
            self.timeline = TimelineStore(self)
            self.slices = SliceStore(self)
            self.sessions = SessionStore(self)
            self.history = HistoryStore(self)
            self.chat_files = ChatFileStore(self)
            self.outputs = OutputStore(self)
            SchemaManager(self).initialize()
            with self._transaction():
                records.initialize(self._connection)
                self.memory.initialize()
                questions.initialize(self._connection)
                connectors.initialize(self._connection)
                self.chat_files.initialize()
                self.devices.initialize()
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS mobile_location_queue("
                    "seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES "
                    "perception_events(event_id) ON DELETE CASCADE, project INTEGER NOT NULL)"
                )
                self._connection.execute(
                    "CREATE INDEX IF NOT EXISTS mapped_mobile_location ON perception_events("
                    "user_id,conversation_id,json_extract(data,'$.device_id'),seq DESC) "
                    "WHERE json_extract(data,'$.payload.state_key')='mobile.location' "
                    "AND json_type(data,'$.payload.map_location.address')='text' "
                    "AND filter_reason IS NULL"
                )
                self.holiday_years.initialize()
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS chat_images (id TEXT PRIMARY KEY, "
                    "event_id TEXT NOT NULL REFERENCES conversation_events(event_id) "
                    "ON DELETE CASCADE, data BLOB NOT NULL)"
                )
            record_path = (
                Path(records_storage_path)
                if records_storage_path is not None
                else self._database_path.parent / "records"
                if self._database_path is not None
                else Path("data/records")
            )
            self.records = records.RecordStore(self, record_path)
            self.questions = questions.QuestionStore(self)
            self.connectors = connectors.ConnectorStore(self)
            self.remote_tools = remote_tools.RemoteCallStore(self)
            self._connection.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            self._connection.close()
            raise

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Store clock must return timezone-aware datetimes")
        return value.astimezone(UTC)

    def now_utc(self) -> datetime:
        return self._now()

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

    def register_device(self, device: Device) -> None:
        device = Device.model_validate(device.model_dump())
        with self._transaction():
            self._register_device(device)

    def _register_device(self, device: Device) -> None:
        existing = self._connection.execute(
            "SELECT user_id,data FROM devices WHERE device_id=?", (device.device_id,)
        ).fetchone()
        if existing is not None and existing["user_id"] != device.user_id:
            raise EventError("A device cannot change its owner")
        if existing and "slice" not in device.allowed_layers:
            previous = Device.model_validate_json(existing["data"])
            if "slice" in previous.allowed_layers:
                self._connection.execute(
                    "DELETE FROM slice_processing WHERE item_id IS NULL AND frame_done=0 "
                    "AND event_id IN (SELECT event_id FROM receipts WHERE device_id=?)",
                    (device.device_id,),
                )
        self._connection.execute(
            "INSERT INTO devices(device_id,user_id,data) VALUES(?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET data=excluded.data",
            (device.device_id, device.user_id, device.model_dump_json()),
        )

    def get_device(self, device_id: str) -> Device:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT data FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if row is None:
            raise EventError(f"Unknown device: {device_id}")
        return Device.model_validate_json(row["data"])

    def register_conversation(self, conversation_id: str, user_id: str) -> None:
        if not conversation_id.strip() or not user_id.strip():
            raise EventError("Conversation and user IDs must be nonempty")
        with self._transaction():
            self._register_conversation(conversation_id, user_id)

    def _register_conversation(self, conversation_id: str, user_id: str) -> None:
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

    def first_conversation_scope(self) -> tuple[str, str] | None:
        """Return the user and conversation for single-conversation device enrollment."""
        self._ensure_open()
        row = self._connection.execute(
            "SELECT user_id,conversation_id FROM conversations ORDER BY rowid LIMIT 1"
        ).fetchone()
        return (str(row["user_id"]), str(row["conversation_id"])) if row is not None else None

    def conversation_scopes(self) -> list[tuple[str, str]]:
        """List persisted conversations, including ones without a currently paired device."""
        self._ensure_open()
        return [
            (str(user), str(conversation))
            for user, conversation in self._connection.execute(
                "SELECT user_id,conversation_id FROM conversations ORDER BY user_id,conversation_id"
            )
        ]

    def _check_scope(self, conversation_id: str | None, user_id: str) -> None:
        if conversation_id is not None and self.get_conversation_user(conversation_id) != user_id:
            raise EventError("Conversation belongs to a different user")

    def received_event(self, device_id: str, producer_event_id: str) -> EventRecord | None:
        """Resolve a device's previous submission before persisting a large upload."""
        self._ensure_open()
        row = self._connection.execute(
            "SELECT event_id FROM receipts WHERE device_id=? AND producer_event_id=?",
            (device_id, producer_event_id),
        ).fetchone()
        return self.get_event(row["event_id"]) if row is not None else None

    def _check_event_size(self, event: EventInput) -> None:
        encoded = encode_json(event.model_dump(mode="json"))
        if len(encoded.encode("utf-8")) > self.max_event_bytes:
            raise EventError("Event exceeds the configured byte limit")

    def chat_image(self, image_id: str, user_id: str, conversation_id: str) -> bytes:
        row = self._connection.execute(
            "SELECT i.data FROM chat_images i JOIN conversation_events e USING(event_id) "
            "WHERE i.id=? AND e.user_id=? AND e.conversation_id=?",
            (image_id, user_id, conversation_id),
        ).fetchone()
        if row is None:
            raise LookupError("Unknown chat image")
        return bytes(row[0])

    def append(self, event: EventInput, *, images: tuple[bytes, ...] = ()) -> Receipt:
        # 冻结模型的嵌套 payload 仍可能被修改，因此持久化前重新验证快照。
        event = EventInput.model_validate(event.model_dump())
        if event.layer == "conversation" and "image_hashes" in event.payload:
            raise EventError("Image references must originate from an upload")
        if images:
            if event.layer != "conversation":
                raise EventError("Chat images require a user message")
            event.payload["image_hashes"] = [sha256(image).hexdigest() for image in images]
        self._check_event_size(event)
        with self._transaction():
            receipt = self._append_event(event)
            if images and not receipt.duplicate:
                event_id = receipt.event.event_id
                self._connection.executemany(
                    "INSERT INTO chat_images(id,event_id,data) VALUES(?,?,?)",
                    [(f"{event_id}:chat:{i}", event_id, data) for i, data in enumerate(images)],
                )
            return receipt

    def _append_event(self, event: EventInput) -> Receipt:
        """Append an already validated event within the caller's write transaction."""
        if event.layer == "record":
            raise EventError("Historical records require the dedicated record batch ingress")
        if event.type == "slice.image":
            ScreenshotContext.model_validate(event.payload.get("capture"))
        fingerprint = event_fingerprint(event)
        device = self.get_device(event.device_id)
        if event.layer not in device.allowed_layers:
            raise EventError("Device is not allowed to use this layer")
        self._check_scope(event.conversation_id, device.user_id)
        if event.reply_device_id is not None:
            reply = self.get_device(event.reply_device_id)
            if reply.user_id != device.user_id:
                raise EventError("Reply device belongs to a different user")
        existing = self._connection.execute(
            "SELECT event_id,fingerprint,job_id FROM receipts "
            "WHERE device_id=? AND producer_event_id=?",
            (event.device_id, event.producer_event_id),
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
            user_id=device.user_id,
            received_at=self._now(),
        )
        if (
            record.type == "state.observed"
            and record.payload.get("state_key") == MOBILE_USAGE_STATE_KEY
            and isinstance(value := record.payload.get("value"), dict)
        ):
            # Keep the name captured with the interval, including offline uploads.
            # Older clients only supplied the name at pairing.
            profile = self._connection.execute(
                "SELECT name,platform FROM client_devices WHERE device_id=?", (record.device_id,)
            ).fetchone()
            raw_name = value.pop("device_name", None)
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            if 0 < len(name) <= 80 and all(unicodedata.category(c) != "Cc" for c in name):
                value["device_name"] = name
            elif profile and profile["name"]:
                value["device_name"] = profile["name"]
            if profile and profile["platform"]:
                value["device_platform"] = profile["platform"]
        job_id = f"job_{uuid4().hex}" if event.layer in ("conversation", "notification") else None
        self._connection.execute(
            "INSERT INTO receipts(event_id,device_id,producer_event_id,"
            "fingerprint,layer,job_id) "
            "VALUES(?,?,?,?,?,?)",
            (
                record.event_id,
                record.device_id,
                record.producer_event_id,
                fingerprint,
                record.layer,
                job_id,
            ),
        )
        table = EVENT_TABLES[record.layer]
        self._connection.execute(
            f"INSERT INTO {table}(event_id,user_id,conversation_id,data) VALUES(?,?,?,?)",
            (
                record.event_id,
                record.user_id,
                record.conversation_id,
                record.model_dump_json(),
            ),
        )
        if record.layer == "conversation":
            self._connection.execute(
                "INSERT INTO dialogue_entries(user_id,conversation_id,event_id) VALUES(?,?,?)",
                (record.user_id, record.conversation_id, record.event_id),
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
            state_key, value = record.payload.get("state_key"), record.payload.get("value")
            reason = None
            if is_mobile_home_activity(state_key, value):
                reason = "mobile_home"
            elif state_key == MOBILE_USAGE_STATE_KEY and isinstance(value, dict):
                seconds = value.get("duration_seconds")
                if (
                    isinstance(seconds, (int, float))
                    and not isinstance(seconds, bool)
                    and 0 <= seconds < 1
                ):
                    reason = "mobile_zero_duration"
            raw_location = state_key == LOCATION_STATE_KEY and not isinstance(value, str)
            projected = reason is None and not raw_location and self._project_state(record)
            if is_wintermute_activity(state_key, value):
                reason = "wintermute_app"
            if reason:
                self._connection.execute(
                    "UPDATE perception_events SET filter_reason=? WHERE event_id=?",
                    (reason, record.event_id),
                )
                return Receipt(event=record, duplicate=False, job_id=None)
            frames = record.payload.get("frames")
            short_visit = (
                record.payload.get("state_key") == "desktop.window_span"
                and isinstance(value, dict)
                and filter_window(value, threshold=SHORT_DWELL_SECONDS) is None
            )
            if short_visit:
                self._connection.execute(
                    "UPDATE perception_events SET filter_reason='short_dwell' WHERE event_id=?",
                    (record.event_id,),
                )
            if (
                not short_visit
                and state_key in ("desktop.window_span", MOBILE_USAGE_STATE_KEY)
                and (record.correlation_id or "").startswith("desktop-window:")
                and record.payload.get("image_recognition") in ("pending", "awaiting_model")
                and isinstance(frames, list)
            ):
                self._connection.executemany(
                    "INSERT INTO desktop_vision_queue(event_id,frame_index) VALUES(?,?)",
                    ((record.event_id, i) for i in range(len(frames))),
                )
            if raw_location:
                try:
                    LocationFix.model_validate(value)
                except ValidationError:
                    raise EventError("Invalid mobile location fix") from None
                self._connection.execute(
                    "INSERT INTO mobile_location_queue(event_id,project) VALUES(?,?)",
                    (record.event_id, 1),
                )
            elif projected:
                self.timeline._project_activity(
                    perception_activity(record), source_seq=record.source_seq
                )
        elif record.layer == "slice":
            self._connection.execute(
                "INSERT INTO slice_processing(event_id) VALUES(?)", (record.event_id,)
            )
        return Receipt(event=record, duplicate=False, job_id=job_id)

    def next_mobile_location(self) -> EventRecord | None:
        while row := self._connection.execute(
            "SELECT p.data FROM mobile_location_queue q JOIN perception_events p USING(event_id) "
            "ORDER BY q.seq LIMIT 1"
        ).fetchone():
            event = EventRecord.model_validate_json(row[0])
            baseline = self._connection.execute(
                "SELECT data FROM perception_events WHERE user_id=? AND conversation_id IS ? "
                "AND json_extract(data,'$.device_id')=? "
                "AND json_extract(data,'$.payload.state_key')='mobile.location' "
                "AND json_type(data,'$.payload.map_location.address')='text' "
                "AND filter_reason IS NULL ORDER BY seq DESC LIMIT 1",
                (event.user_id, event.conversation_id, event.device_id),
            ).fetchone()
            if baseline is None:
                return event
            previous = EventRecord.model_validate_json(baseline[0])
            if event.occurred_at < previous.occurred_at:
                reason = "location_stale"
            elif (
                LocationFix.model_validate(event.payload["value"]).distance_to(
                    LocationFix.model_validate(previous.payload["value"])
                )
                < 300
            ):
                reason = "location_nearby"
            else:
                return event
            with self._transaction():
                self._connection.execute(
                    "UPDATE perception_events SET filter_reason=? WHERE event_id=?",
                    (reason, event.event_id),
                )
                self._connection.execute(
                    "DELETE FROM mobile_location_queue WHERE event_id=?", (event.event_id,)
                )
        return None

    def finish_mobile_location(self, event_id: str, address: str | None) -> None:
        with self._transaction():
            row = self._connection.execute(
                "DELETE FROM mobile_location_queue WHERE event_id=? RETURNING project", (event_id,)
            ).fetchone()
            if row is None:
                return
            event = self.get_event(event_id)
            event.payload["map_location"] = {"provider": "photon", "address": address}
            self._connection.execute(
                "UPDATE perception_events SET data=?, filter_reason=? WHERE event_id=?",
                (event.model_dump_json(), None if address else "location_unresolved", event_id),
            )
            if address and self._project_state(event):
                self.timeline._project_activity(
                    perception_activity(event), source_seq=event.source_seq
                )

    def _project_state(self, event: EventRecord) -> bool:
        subject = event.payload.get("subject")
        key = event.payload.get("state_key")
        if not isinstance(subject, str) or not isinstance(key, str) or not subject or not key:
            return True
        scope = event.conversation_id or ""
        state_key = (event.user_id, scope, event.device_id, event.source_id, subject, key)
        row = self._connection.execute(
            "SELECT event_id FROM perception_state WHERE user_id=? AND conversation_scope=? "
            "AND device_id=? AND source_id=? AND subject=? AND state_key=?",
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
            "INSERT INTO perception_state(user_id,conversation_scope,device_id,source_id,subject,"
            "state_key,event_id,valid_until) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,conversation_scope,device_id,source_id,subject,state_key) "
            "DO UPDATE SET event_id=excluded.event_id,valid_until=excluded.valid_until",
            (*state_key, event.event_id, expiry),
        )
        return True

    def get_event(self, event_id: str, *, user_id: str | None = None) -> EventRecord:
        self._ensure_open()
        receipt = self._connection.execute(
            "SELECT layer FROM receipts WHERE event_id=?", (event_id,)
        ).fetchone()
        if receipt is None:
            raise EventError(f"Unknown event: {event_id}")
        table = EVENT_TABLES[receipt["layer"]]
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
        table = EVENT_TABLES[layer]
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
            "WHERE e.filter_reason IS NULL AND s.user_id=? "
            "AND (s.conversation_scope='' OR s.conversation_scope=?) "
            "AND (s.valid_until IS NULL OR s.valid_until>?) ORDER BY e.seq DESC LIMIT ?",
            (user_id, conversation_id or "", instant.timestamp(), self._limit(limit)),
        ).fetchall()
        return tuple(EventRecord.model_validate_json(row["data"]) for row in rows)

    def close(self) -> None:
        if self._closed:
            return
        if self.sessions._owner is not None:
            self.sessions.release_scheduler(self.sessions._owner)
        self._connection.close()
        self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
