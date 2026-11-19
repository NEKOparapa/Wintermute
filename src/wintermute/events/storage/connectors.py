"""Persistent storage for MCP, device, and CLI connectors and their credentials."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore

type ToolType = Literal["mcp", "device", "cli"]
type SourceKind = Literal["official", "third_party"]
type AuthMode = Literal["none", "credential", "oauth", "device"]

_CONNECTOR_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
_CONNECTION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


class ConnectorSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    found_at: str = Field(min_length=1, max_length=2048)
    publisher: str = Field(min_length=1, max_length=256)


class ConnectorConnectionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    label: str
    verified: bool


class ConnectorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    tool_type: ToolType
    endpoint: str | None = None
    source: ConnectorSource | None = None
    header_names: list[str] = Field(default_factory=list)
    credential_label: str | None = None
    credential_query_param: str | None = None
    auth_mode: AuthMode = "none"
    scopes: list[str] = Field(default_factory=list)
    grants: dict[str, list[str]] = Field(default_factory=dict)
    device_id: str | None = None
    config: dict[str, JsonValue] = Field(default_factory=dict)
    verified: bool = False
    enabled: bool = True
    has_credentials: bool = False
    connections: list[ConnectorConnectionSummary] = Field(default_factory=list)
    created_at: AwareDatetime
    updated_at: AwareDatetime


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS connectors ("
        "user_id TEXT NOT NULL, "
        "id TEXT NOT NULL, "
        "name TEXT NOT NULL, "
        "tool_type TEXT NOT NULL CHECK(tool_type IN ('mcp','device','cli')), "
        "endpoint TEXT, "
        "source_kind TEXT, "
        "source_found_at TEXT, "
        "source_publisher TEXT, "
        "header_names TEXT NOT NULL DEFAULT '[]', "
        "credential_label TEXT, "
        "credential_query_param TEXT, "
        "auth_mode TEXT NOT NULL DEFAULT 'none' "
        "CHECK(auth_mode IN ('none','credential','oauth','device')), "
        "scopes TEXT NOT NULL DEFAULT '[]', "
        "grants TEXT NOT NULL DEFAULT '{}', "
        "device_id TEXT, "
        "config_json TEXT NOT NULL DEFAULT '{}', "
        "verified INTEGER NOT NULL DEFAULT 0, "
        "enabled INTEGER NOT NULL DEFAULT 1, "
        "created_at REAL NOT NULL, "
        "updated_at REAL NOT NULL, "
        "PRIMARY KEY(user_id, id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS connector_credentials ("
        "user_id TEXT NOT NULL, "
        "connector_id TEXT NOT NULL, "
        "connection_id TEXT NOT NULL DEFAULT 'default', "
        "label TEXT NOT NULL DEFAULT '默认账号', "
        "values_json TEXT NOT NULL, "
        "verified INTEGER NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL, "
        "updated_at REAL NOT NULL, "
        "PRIMARY KEY(user_id, connector_id, connection_id), "
        "FOREIGN KEY(user_id, connector_id) REFERENCES connectors(user_id, id) ON DELETE CASCADE)"
    )


def _slugify(name: str, endpoint: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if slug and len(slug) <= 48 and _CONNECTOR_ID_RE.match(slug):
        return slug
    digest = hashlib.sha256(f"{name}\n{endpoint}".encode()).hexdigest()[:10]
    return f"mcp_{digest}"


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


class ConnectorStore:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._db = store._connection

    def _hydrate(self, row: sqlite3.Row) -> ConnectorRecord:
        cred_rows = self._db.execute(
            "SELECT connection_id, label, verified FROM connector_credentials "
            "WHERE user_id=? AND connector_id=? ORDER BY created_at, connection_id",
            (row["user_id"], row["id"]),
        ).fetchall()
        connections = [
            ConnectorConnectionSummary(
                connection_id=c["connection_id"],
                label=c["label"],
                verified=bool(c["verified"]),
            )
            for c in cred_rows
        ]
        source: ConnectorSource | None = None
        if row["source_kind"] and row["source_found_at"] and row["source_publisher"]:
            source = ConnectorSource(
                kind=row["source_kind"],
                found_at=row["source_found_at"],
                publisher=row["source_publisher"],
            )
        return ConnectorRecord(
            id=row["id"],
            name=row["name"],
            tool_type=row["tool_type"],
            endpoint=row["endpoint"],
            source=source,
            header_names=json.loads(row["header_names"]),
            credential_label=row["credential_label"],
            credential_query_param=row["credential_query_param"],
            auth_mode=row["auth_mode"],
            scopes=json.loads(row["scopes"]),
            grants=json.loads(row["grants"]),
            device_id=row["device_id"],
            config=json.loads(row["config_json"]),
            verified=bool(row["verified"]),
            enabled=bool(row["enabled"]),
            has_credentials=bool(connections),
            connections=connections,
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def list(self, user_id: str) -> list[ConnectorRecord]:
        rows = self._db.execute(
            "SELECT * FROM connectors WHERE user_id=? ORDER BY created_at, id",
            (user_id,),
        ).fetchall()
        return [self._hydrate(row) for row in rows]

    def get(self, user_id: str, connector_id: str) -> ConnectorRecord:
        row = self._db.execute(
            "SELECT * FROM connectors WHERE user_id=? AND id=?",
            (user_id, connector_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"Unknown connector: {connector_id}")
        return self._hydrate(row)

    def upsert_mcp(
        self,
        user_id: str,
        *,
        name: str,
        endpoint: str,
        source: ConnectorSource,
        connector_id: str | None = None,
        header_names: Sequence[str] = (),
        credential_label: str | None = None,
        credential_query_param: str | None = None,
        verified: bool = False,
    ) -> ConnectorRecord:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 128:
            raise ValueError("连接器名称不能为空且不超过 128 个字符")
        clean_headers = [h.strip() for h in header_names if h.strip()]
        clean_query = credential_query_param.strip() if credential_query_param else None
        clean_label = credential_label.strip() if credential_label else None
        needs_cred = bool(clean_headers or clean_query or clean_label)
        auth_mode: AuthMode = "credential" if needs_cred else "none"
        now = self._store.now_utc().timestamp()

        with self._store._transaction():
            if connector_id is not None:
                if not _CONNECTOR_ID_RE.match(connector_id):
                    raise ValueError("连接器 ID 格式无效")
                cid = connector_id
            else:
                existing = self._db.execute(
                    "SELECT id FROM connectors WHERE user_id=? AND tool_type='mcp' AND endpoint=?",
                    (user_id, endpoint),
                ).fetchone()
                if existing is not None:
                    cid = existing["id"]
                else:
                    base = _slugify(clean_name, endpoint)
                    cid = base
                    suffix = 2
                    while self._db.execute(
                        "SELECT 1 FROM connectors WHERE user_id=? AND id=?",
                        (user_id, cid),
                    ).fetchone():
                        cid = f"{base}_{suffix}"
                        suffix += 1

            self._db.execute(
                "INSERT INTO connectors ("
                "user_id, id, name, tool_type, endpoint, source_kind, source_found_at, "
                "source_publisher, header_names, credential_label, credential_query_param, "
                "auth_mode, verified, enabled, created_at, updated_at"
                ") VALUES (?, ?, ?, 'mcp', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(user_id, id) DO UPDATE SET "
                "name=excluded.name, endpoint=excluded.endpoint, "
                "source_kind=excluded.source_kind, source_found_at=excluded.source_found_at, "
                "source_publisher=excluded.source_publisher, header_names=excluded.header_names, "
                "credential_label=excluded.credential_label, "
                "credential_query_param=excluded.credential_query_param, "
                "auth_mode=excluded.auth_mode, verified=excluded.verified, "
                "enabled=1, updated_at=excluded.updated_at",
                (
                    user_id,
                    cid,
                    clean_name,
                    endpoint,
                    source.kind,
                    source.found_at,
                    source.publisher,
                    json.dumps(clean_headers, ensure_ascii=False),
                    clean_label,
                    clean_query,
                    auth_mode,
                    int(verified),
                    now,
                    now,
                ),
            )
            return self.get(user_id, cid)

    def upsert_device(
        self,
        user_id: str,
        *,
        connector_id: str,
        name: str,
        device_id: str,
        enabled: bool,
        config: Mapping[str, JsonValue] | None = None,
    ) -> ConnectorRecord:
        if not _CONNECTOR_ID_RE.match(connector_id):
            raise ValueError("连接器 ID 格式无效")
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 128:
            raise ValueError("连接器名称不能为空且不超过 128 个字符")
        now = self._store.now_utc().timestamp()
        config_text = json.dumps(dict(config or {}), ensure_ascii=False)
        with self._store._transaction():
            self._db.execute(
                "INSERT INTO connectors ("
                "user_id, id, name, tool_type, auth_mode, device_id, config_json, "
                "verified, enabled, created_at, updated_at"
                ") VALUES (?, ?, ?, 'device', 'device', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, id) DO UPDATE SET "
                "name=excluded.name, device_id=excluded.device_id, "
                "config_json=excluded.config_json, verified=excluded.verified, "
                "enabled=excluded.enabled, updated_at=excluded.updated_at",
                (
                    user_id,
                    connector_id,
                    clean_name,
                    device_id,
                    config_text,
                    int(enabled),
                    int(enabled),
                    now,
                    now,
                ),
            )
            return self.get(user_id, connector_id)

    def set_verified(
        self,
        user_id: str,
        connector_id: str,
        verified: bool,
        *,
        connection_id: str | None = None,
    ) -> ConnectorRecord:
        now = self._store.now_utc().timestamp()
        with self._store._transaction():
            cur = self._db.execute(
                "UPDATE connectors SET verified=?, updated_at=? WHERE user_id=? AND id=?",
                (int(verified), now, user_id, connector_id),
            )
            if cur.rowcount == 0:
                raise LookupError(f"Unknown connector: {connector_id}")
            if connection_id is not None:
                self._db.execute(
                    "UPDATE connector_credentials SET verified=?, updated_at=? "
                    "WHERE user_id=? AND connector_id=? AND connection_id=?",
                    (int(verified), now, user_id, connector_id, connection_id),
                )
            return self.get(user_id, connector_id)

    def save_credentials(
        self,
        user_id: str,
        connector_id: str,
        values: Mapping[str, str],
        *,
        connection_id: str = "default",
        label: str = "默认账号",
        verified: bool = False,
    ) -> ConnectorRecord:
        if not _CONNECTION_ID_RE.match(connection_id):
            raise ValueError("connectionId 格式无效")
        cleaned: dict[str, str] = {}
        for key, value in values.items():
            k = key.strip()
            v = value.strip()
            if not k or not v:
                raise ValueError("凭据字段名和值不能为空")
            cleaned[k] = v
        if not cleaned:
            raise ValueError("至少需要提供一个凭据字段")
        now = self._store.now_utc().timestamp()
        with self._store._transaction():
            exists = self._db.execute(
                "SELECT 1 FROM connectors WHERE user_id=? AND id=?",
                (user_id, connector_id),
            ).fetchone()
            if exists is None:
                raise LookupError(f"Unknown connector: {connector_id}")
            self._db.execute(
                "INSERT INTO connector_credentials ("
                "user_id, connector_id, connection_id, label, values_json, verified, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, connector_id, connection_id) DO UPDATE SET "
                "label=excluded.label, values_json=excluded.values_json, "
                "verified=excluded.verified, updated_at=excluded.updated_at",
                (
                    user_id,
                    connector_id,
                    connection_id,
                    label.strip() or "默认账号",
                    json.dumps(cleaned, ensure_ascii=False),
                    int(verified),
                    now,
                    now,
                ),
            )
            self._db.execute(
                "UPDATE connectors SET verified=?, updated_at=? WHERE user_id=? AND id=?",
                (int(verified), now, user_id, connector_id),
            )
            return self.get(user_id, connector_id)

    def get_credentials(
        self,
        user_id: str,
        connector_id: str,
        connection_id: str | None = None,
    ) -> tuple[str, dict[str, str]] | None:
        if connection_id is not None:
            row = self._db.execute(
                "SELECT connection_id, values_json FROM connector_credentials "
                "WHERE user_id=? AND connector_id=? AND connection_id=?",
                (user_id, connector_id, connection_id),
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT connection_id, values_json FROM connector_credentials "
                "WHERE user_id=? AND connector_id=? ORDER BY created_at, connection_id LIMIT 1",
                (user_id, connector_id),
            ).fetchone()
        if row is None:
            return None
        return str(row["connection_id"]), dict(json.loads(row["values_json"]))

    def delete(self, user_id: str, connector_id: str) -> None:
        with self._store._transaction():
            cur = self._db.execute(
                "DELETE FROM connectors WHERE user_id=? AND id=?",
                (user_id, connector_id),
            )
            if cur.rowcount == 0:
                raise LookupError(f"Unknown connector: {connector_id}")
