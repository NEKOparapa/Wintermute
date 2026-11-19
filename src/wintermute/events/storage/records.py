"""User-scoped historical records, independent of the live event timeline."""

import builtins
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Self, get_args
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from wintermute.events.core.types import EventError

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore

type RecordType = Literal["sms", "call_log", "contacts", "notion", "recording_list", "document"]
type BatchStatus = Literal["queued", "processing", "completed", "partial_failed", "failed"]
type ItemStatus = Literal["pending", "filtered", "processing", "processed", "failed"]
type RecordContent = str | dict[str, JsonValue]
type RecordIdentifier = Annotated[str, StringConstraints(min_length=1, max_length=256)]

MAX_BATCH_BYTES = 16 * 1024 * 1024
INLINE_BYTES = 8 * 1024


class RecordConflict(EventError):
    """A batch id was reused with different content or ownership."""


class RecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)

    external_id: RecordIdentifier | None = None
    occurred_at: AwareDatetime
    title: Annotated[str, Field(max_length=1024)] | None = None
    content: RecordContent

    @field_validator("occurred_at", mode="before")
    @classmethod
    def parse_time(cls, value: object) -> object:
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def finite_content(self) -> Self:
        _json(self.content)
        return self


class RecordBatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)

    batch_id: RecordIdentifier | None = None
    record_type: RecordType
    source_name: RecordIdentifier = "default"
    items: list[RecordInput] = Field(min_length=1, max_length=10000)

    @field_validator("source_name", "batch_id")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("Record identifiers must be nonblank and contain no controls")
        return value


class RecordBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch_id: str
    user_id: str
    terminal_id: str
    record_type: RecordType
    source_name: str
    status: BatchStatus
    total_items: int
    accepted_items: int
    deduplicated_items: int
    filtered_items: int
    processed_items: int
    failed_items: int
    error: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class RecordItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    batch_id: str
    user_id: str
    record_type: RecordType
    source_name: str
    external_id: str | None
    content_sha256: str
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    title: str | None
    status: ItemStatus
    attempts: int
    error: str | None


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS record_batches ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT,batch_id TEXT UNIQUE NOT NULL,"
        "user_id TEXT NOT NULL,terminal_id TEXT NOT NULL,record_type TEXT NOT NULL,"
        "source_name TEXT NOT NULL,request_sha256 TEXT NOT NULL,"
        "status TEXT NOT NULL CHECK(status IN "
        "('queued','processing','completed','partial_failed','failed')),"
        "total_items INTEGER NOT NULL,accepted_items INTEGER NOT NULL DEFAULT 0,"
        "deduplicated_items INTEGER NOT NULL DEFAULT 0,filtered_items INTEGER NOT NULL DEFAULT 0,"
        "processed_items INTEGER NOT NULL DEFAULT 0,failed_items INTEGER NOT NULL DEFAULT 0,"
        "error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS record_items ("
        "item_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES record_batches(batch_id),"
        "user_id TEXT NOT NULL,record_type TEXT NOT NULL,source_name TEXT NOT NULL,"
        "external_id TEXT,"
        "content_sha256 TEXT NOT NULL,dedup_key TEXT NOT NULL,occurred_at TEXT NOT NULL,"
        "received_at TEXT NOT NULL,title TEXT,payload_json TEXT,payload_path TEXT,"
        "status TEXT NOT NULL CHECK(status IN "
        "('pending','filtered','processing','processed','failed')),"
        "attempts INTEGER NOT NULL DEFAULT 0,error TEXT,processed_at TEXT,"
        "CHECK((payload_json IS NULL)!=(payload_path IS NULL)),"
        "UNIQUE(user_id,record_type,source_name,dedup_key,content_sha256))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS record_pending "
        "ON record_items(user_id,status,occurred_at,item_id)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS record_history ON record_batches(user_id,seq DESC)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS record_timeline "
        "ON record_items(user_id,occurred_at DESC,item_id DESC)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS record_type_timeline "
        "ON record_items(user_id,record_type,occurred_at DESC,item_id DESC)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS record_external "
        "ON record_items(user_id,record_type,source_name,external_id) WHERE external_id IS NOT NULL"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS memory_maintenance "
        "(user_id TEXT PRIMARY KEY,completed_date TEXT NOT NULL)"
    )


def _batch(row: sqlite3.Row) -> RecordBatch:
    return RecordBatch.model_validate({key: row[key] for key in RecordBatch.model_fields})


def _item(row: sqlite3.Row) -> RecordItem:
    return RecordItem.model_validate({key: row[key] for key in RecordItem.model_fields})


def _safe_component(value: str) -> str:
    # Identifiers are opaque, never paths. Encoding every unsafe identifier also avoids
    # Windows device names, ADS, traversal and collisions introduced by sanitization.
    if (
        re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value)
        and value.upper()
        not in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(10)),
            *(f"LPT{i}" for i in range(10)),
        }
        and not value.startswith("hash_")
    ):
        return value
    return "hash_" + hashlib.sha256(value.encode()).hexdigest()


class RecordStore:
    def __init__(self, store: "SQLiteStore", directory: Path) -> None:
        self._store = store
        self._db = store._connection
        self.directory = directory.absolute()

    def _path(self, relative: str, *, create_parent: bool = False) -> Path:
        parts = relative.split("/")
        if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts):
            raise EventError("Invalid record payload path")
        if any(p.startswith(".") for p in parts):
            raise EventError("Invalid record payload path")
        # Reject links in both the configured root and all descendants before any mkdir/read.
        for ancestor in (*reversed(self.directory.parents), self.directory):
            if ancestor.is_symlink() or ancestor.is_junction():
                raise EventError("Record storage cannot contain symbolic links")
        target = self.directory.joinpath(*parts)
        for parent in (target.parent.parent, target.parent, target):
            if parent.is_symlink() or parent.is_junction():
                raise EventError("Record storage cannot contain symbolic links")
        if not target.resolve().is_relative_to(self.directory.resolve()):
            raise EventError("Record payload escaped storage")
        if create_parent:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return target

    def _write_payload(self, relative: str, raw: bytes) -> None:
        target = self._path(relative, create_parent=True)
        fd, name = tempfile.mkstemp(prefix=".record-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self._path(relative))
        finally:
            Path(name).unlink(missing_ok=True)

    def submit(self, terminal_id: str, batch: RecordBatchInput) -> RecordBatch:
        batch = RecordBatchInput.model_validate(batch.model_dump())
        request = _json(batch.model_dump(mode="json", exclude={"batch_id"}))
        if len(request.encode("utf-8")) > MAX_BATCH_BYTES:
            raise ValueError("Record batch exceeds 16 MiB")
        digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
        batch_id = batch.batch_id or "rec_batch_" + uuid4().hex
        with self._store._transaction():
            terminal = self._store.get_terminal(terminal_id)
            if "record" not in terminal.allowed_layers:
                raise EventError("Terminal is not allowed to import records")
            existing = self._db.execute(
                "SELECT * FROM record_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if existing is not None:
                if (existing["user_id"], existing["terminal_id"], existing["request_sha256"]) != (
                    terminal.user_id,
                    terminal_id,
                    digest,
                ):
                    raise RecordConflict("Record batch id conflicts with a previous import")
                return _batch(existing)
            now = self._store.now_utc().isoformat()
            self._db.execute(
                "INSERT INTO record_batches(batch_id,user_id,terminal_id,record_type,source_name,"
                "request_sha256,status,total_items,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'queued',?,?,?)",
                (
                    batch_id,
                    terminal.user_id,
                    terminal_id,
                    batch.record_type,
                    batch.source_name,
                    digest,
                    len(batch.items),
                    now,
                    now,
                ),
            )
            accepted = 0
            for item in batch.items:
                raw = _json(item.content).encode("utf-8")
                # Include provenance time/title in the version hash, otherwise identical SMS
                # bodies at different times or retitled documents would disappear silently.
                content_hash = hashlib.sha256(
                    _json(item.model_dump(mode="json", exclude={"external_id"})).encode("utf-8")
                ).hexdigest()
                key = "external:" + item.external_id if item.external_id else "content"
                old = self._db.execute(
                    "SELECT 1 FROM record_items WHERE user_id=? AND record_type=? "
                    "AND source_name=? AND dedup_key=? AND content_sha256=?",
                    (terminal.user_id, batch.record_type, batch.source_name, key, content_hash),
                ).fetchone()
                if old is not None:
                    continue
                relative = None
                if len(raw) > INLINE_BYTES:
                    relative = "/".join(
                        (
                            _safe_component(terminal.user_id),
                            _safe_component(batch_id),
                            hashlib.sha256(raw).hexdigest() + ".json",
                        )
                    )
                    self._write_payload(relative, raw)
                self._db.execute(
                    "INSERT INTO record_items(item_id,batch_id,user_id,record_type,source_name,"
                    "external_id,content_sha256,dedup_key,occurred_at,received_at,title,payload_json,"
                    "payload_path,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
                    (
                        "rec_" + uuid4().hex,
                        batch_id,
                        terminal.user_id,
                        batch.record_type,
                        batch.source_name,
                        item.external_id,
                        content_hash,
                        key,
                        item.occurred_at.isoformat(),
                        now,
                        item.title,
                        raw.decode("utf-8") if relative is None else None,
                        relative,
                    ),
                )
                accepted += 1
            self._db.execute(
                "UPDATE record_batches SET accepted_items=?,deduplicated_items=?,status=? "
                "WHERE batch_id=?",
                (
                    accepted,
                    len(batch.items) - accepted,
                    "queued" if accepted else "completed",
                    batch_id,
                ),
            )
            return self.get(terminal.user_id, batch_id)

    def get(self, user_id: str, batch_id: str) -> RecordBatch:
        self._store._ensure_open()
        row = self._db.execute(
            "SELECT * FROM record_batches WHERE user_id=? AND batch_id=?", (user_id, batch_id)
        ).fetchone()
        if row is None:
            raise LookupError("Unknown record batch")
        return _batch(row)

    def list(
        self, user_id: str, *, limit: int = 20, cursor: str | None = None
    ) -> tuple[list[RecordBatch], str | None]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        upper = 2**63 - 1
        if cursor is not None:
            if not re.fullmatch(r"[1-9][0-9]{0,18}", cursor) or int(cursor) >= 2**63:
                raise ValueError("Invalid record batch cursor")
            upper = int(cursor)
        self._store._ensure_open()
        rows = self._db.execute(
            "SELECT * FROM record_batches WHERE user_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
            (user_id, upper, limit + 1),
        ).fetchall()
        return (
            [_batch(row) for row in rows[:limit]],
            str(rows[limit - 1]["seq"]) if len(rows) > limit else None,
        )

    def get_item(self, user_id: str, item_id: str) -> RecordItem:
        self._store._ensure_open()
        row = self._db.execute(
            "SELECT * FROM record_items WHERE user_id=? AND item_id=?", (user_id, item_id)
        ).fetchone()
        if row is None:
            raise LookupError("Unknown record item")
        return _item(row)

    def list_items(
        self,
        user_id: str,
        *,
        limit: int = 40,
        cursor: str | None = None,
        record_type: str | None = None,
    ) -> tuple[builtins.list[RecordItem], str | None]:
        """Keyset pagination by original occurrence time, including completed records."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        if record_type is not None and record_type not in get_args(RecordType.__value__):
            raise ValueError("Invalid record type")
        self._store._ensure_open()
        clauses = ["user_id=?"]
        params: list[str | int] = [user_id]
        if record_type is not None:
            clauses.append("record_type=?")
            params.append(record_type)
        if cursor is not None:
            if not re.fullmatch(r"rec_[0-9a-f]{32}", cursor):
                raise ValueError("Invalid record cursor")
            try:
                anchor = self.get_item(user_id, cursor)
            except LookupError:
                raise ValueError("Invalid record cursor") from None
            if record_type is not None and anchor.record_type != record_type:
                raise ValueError("Invalid record cursor")
            clauses.append("(occurred_at,item_id)<(?,?)")
            params.extend((anchor.occurred_at.isoformat(), anchor.item_id))
        params.append(limit + 1)
        rows = self._db.execute(
            "SELECT * FROM record_items WHERE " + " AND ".join(clauses)
            + " ORDER BY occurred_at DESC,item_id DESC LIMIT ?",
            params,
        ).fetchall()
        return (
            [_item(row) for row in rows[:limit]],
            rows[limit - 1]["item_id"] if len(rows) > limit else None,
        )

    def pending(self, user_id: str, *, limit: int = 100) -> tuple[RecordItem, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Pending page size must be between 1 and 1000")
        self._store._ensure_open()
        return tuple(
            _item(row)
            for row in self._db.execute(
                "SELECT * FROM record_items WHERE user_id=? AND status='pending' "
                "ORDER BY occurred_at,item_id LIMIT ?",
                (user_id, limit),
            )
        )

    def queue_times(self, user_id: str) -> tuple[datetime, datetime] | None:
        self._store._ensure_open()
        row = self._db.execute(
            "SELECT MIN(received_at),MAX(received_at) FROM record_items "
            "WHERE user_id=? AND status='pending'",
            (user_id,),
        ).fetchone()
        if row[0] is None:
            return None
        return datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1])

    def payload(self, item: RecordItem) -> RecordContent:
        self._store._ensure_open()
        row = self._db.execute(
            "SELECT payload_json,payload_path FROM record_items WHERE user_id=? AND item_id=?",
            (item.user_id, item.item_id),
        ).fetchone()
        if row is None:
            raise LookupError("Unknown record item")
        if row["payload_path"] is None:
            raw = row["payload_json"]
        else:
            path = self._path(row["payload_path"])
            with path.open("rb") as source:
                data = source.read(MAX_BATCH_BYTES + 1)
            if (
                len(data) > MAX_BATCH_BYTES
                or hashlib.sha256(data).hexdigest() + ".json" != path.name
            ):
                raise EventError("Record payload failed integrity check")
            raw = data.decode("utf-8")
        value: JsonValue = json.loads(raw)
        if not isinstance(value, (str, dict)):
            raise EventError("Invalid stored record payload")
        return value

    def previous_payload(self, item: RecordItem) -> RecordContent | None:
        """Latest already processed snapshot, never a newer fact than the incoming record."""
        if item.external_id is None:
            return None
        self._store._ensure_open()
        row = self._db.execute(
            "SELECT * FROM record_items WHERE user_id=? AND record_type=? AND source_name=? "
            "AND external_id=? AND status='processed' AND occurred_at<=? AND item_id!=? "
            "ORDER BY occurred_at DESC,received_at DESC,item_id DESC LIMIT 1",
            (
                item.user_id,
                item.record_type,
                item.source_name,
                item.external_id,
                item.occurred_at.isoformat(),
                item.item_id,
            ),
        ).fetchone()
        return self.payload(_item(row)) if row is not None else None

    def mark(
        self,
        user_id: str,
        item_ids: Sequence[str],
        *,
        status: Literal["processed", "filtered"],
        error: str | None = None,
        owner_id: str | None = None,
    ) -> None:
        if status not in {"processed", "filtered"}:
            raise ValueError("Invalid record completion status")
        self._finish(user_id, item_ids, status=status, error=error, owner_id=owner_id)

    def start(self, user_id: str, item_ids: Sequence[str], *, owner_id: str | None = None) -> None:
        """Expose in-flight batches without making a crash strand items in processing state."""
        with self._store._transaction():
            if owner_id is not None:
                self._store._require_scheduler(owner_id)
            now = self._store.now_utc().isoformat()
            for item_id in dict.fromkeys(item_ids):
                row = self._db.execute(
                    "SELECT batch_id FROM record_items WHERE user_id=? AND item_id=? "
                    "AND status='pending'",
                    (user_id, item_id),
                ).fetchone()
                if row is None:
                    raise LookupError("Unknown pending record item")
                self._db.execute(
                    "UPDATE record_batches SET status='processing',updated_at=? WHERE batch_id=?",
                    (now, row["batch_id"]),
                )

    def fail(
        self,
        user_id: str,
        item_ids: Sequence[str],
        *,
        error: str,
        owner_id: str | None = None,
    ) -> None:
        self._finish(user_id, item_ids, status="failed", error=error, owner_id=owner_id)

    def _finish(
        self,
        user_id: str,
        item_ids: Sequence[str],
        *,
        status: str,
        error: str | None,
        owner_id: str | None,
    ) -> None:
        # Error bodies may contain model/record secrets; only persist a generic failure label.
        safe_error = "record_processing_failed" if status == "failed" else error
        with self._store._transaction():
            if owner_id is not None:
                self._store._require_scheduler(owner_id)
            batches: set[str] = set()
            now = self._store.now_utc().isoformat()
            for item_id in dict.fromkeys(item_ids):
                row = self._db.execute(
                    "SELECT batch_id,attempts,status FROM record_items "
                    "WHERE user_id=? AND item_id=?",
                    (user_id, item_id),
                ).fetchone()
                if row is None:
                    raise LookupError("Unknown record item")
                if row["status"] not in {"pending", "processing"}:
                    continue
                attempts = row["attempts"] + (1 if status == "failed" else 0)
                actual = "pending" if status == "failed" and attempts < 3 else status
                self._db.execute(
                    "UPDATE record_items SET status=?,attempts=?,error=?,processed_at=? "
                    "WHERE item_id=?",
                    (actual, attempts, safe_error, now if actual != "pending" else None, item_id),
                )
                batches.add(row["batch_id"])
            for batch_id in batches:
                counts = dict(
                    self._db.execute(
                        "SELECT status,COUNT(*) FROM record_items WHERE batch_id=? GROUP BY status",
                        (batch_id,),
                    ).fetchall()
                )
                pending = counts.get("pending", 0) + counts.get("processing", 0)
                failures = counts.get("failed", 0)
                successes = counts.get("processed", 0) + counts.get("filtered", 0)
                batch_status = (
                    "processing"
                    if pending
                    else "partial_failed"
                    if failures and successes
                    else "failed"
                    if failures
                    else "completed"
                )
                self._db.execute(
                    "UPDATE record_batches SET status=?,filtered_items=?,processed_items=?,"
                    "failed_items=?,error=?,updated_at=? WHERE batch_id=?",
                    (
                        batch_status,
                        counts.get("filtered", 0),
                        counts.get("processed", 0),
                        failures,
                        "record_processing_failed" if failures else None,
                        now,
                        batch_id,
                    ),
                )
