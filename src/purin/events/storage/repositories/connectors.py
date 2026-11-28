"""Persistent storage for MCP, device, and CLI connectors and their credentials."""

from __future__ import annotations

import builtins
import hashlib
import json
import re
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

if TYPE_CHECKING:
    from purin.events.storage.sqlite import SQLiteStore

type ToolType = Literal["mcp", "device", "cli"]
type SourceKind = Literal["official", "third_party"]
type AuthMode = Literal["none", "credential", "oauth", "device"]
type ConnectorCallStatus = Literal[
    "queued", "running", "succeeded", "failed", "timed_out", "expired"
]

_CONNECTOR_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
_CONNECTION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
_SHELL_TOOLS = frozenset({"powershell", "cmd", "sh", "bash", "pwsh"})


class ConnectorCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    connector_id: str
    connector_name: str
    tool_type: ToolType
    target: str
    tool: str
    arguments: dict[str, JsonValue]
    status: ConnectorCallStatus
    output: str = ""
    error: str = ""
    exit_code: int | None = None
    truncated: bool = False
    timeout_seconds: int | None = None
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None


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

    def list_account(self, user_id: str) -> list[ConnectorRecord]:
        rows = self._db.execute(
            "SELECT * FROM connectors WHERE user_id=? AND tool_type!='device' "
            "ORDER BY created_at, id",
            (user_id,),
        ).fetchall()
        return [self._hydrate(row) for row in rows]

    def get(
        self, user_id: str, connector_id: str, *, device_id: str | None = None
    ) -> ConnectorRecord:
        if device_id is None:
            try:
                return self.get_account(user_id, connector_id)
            except LookupError:
                pass
        devices = self.list_devices(
            user_id, (device_id,) if device_id is not None else None, connector_id
        )
        if len(devices) == 1:
            return devices[0]
        if devices:
            raise ValueError("多台设备上存在该连接器，请指定设备")
        raise LookupError(f"Unknown connector: {connector_id}")

    def get_account(self, user_id: str, connector_id: str) -> ConnectorRecord:
        row = self._db.execute(
            "SELECT * FROM connectors WHERE user_id=? AND id=? AND tool_type!='device'",
            (user_id, connector_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"Unknown connector: {connector_id}")
        return self._hydrate(row)

    def list_devices(
        self,
        user_id: str,
        device_ids: Collection[str] | None,
        connector_id: str | None = None,
    ) -> builtins.list[ConnectorRecord]:
        where = "user_id=?"
        params = [user_id]
        if device_ids is not None:
            if not device_ids:
                return []
            placeholders = ",".join("?" for _ in device_ids)
            where += f" AND device_id IN ({placeholders})"
            params.extend(device_ids)
        if connector_id is not None:
            where += " AND id=?"
            params.append(connector_id)
        rows = self._db.execute(
            f"SELECT * FROM device_connectors WHERE {where} ORDER BY device_id, created_at, id",
            params,
        )
        return [
            ConnectorRecord(
                id=r["id"],
                name=r["name"],
                tool_type="device",
                auth_mode="device",
                device_id=r["device_id"],
                config=json.loads(r["config_json"]),
                enabled=bool(r["enabled"]),
                verified=bool(r["verified"]),
                created_at=_dt(r["created_at"]),
                updated_at=_dt(r["updated_at"]),
            )
            for r in rows
        ]

    def list(
        self, user_id: str, *, device_ids: Collection[str] | None = None
    ) -> builtins.list[ConnectorRecord]:
        return self.list_account(user_id) + self.list_devices(user_id, device_ids)

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
                if self._db.execute(
                    "SELECT 1 FROM connectors WHERE user_id=? AND id=? AND tool_type!='mcp'",
                    (user_id, cid),
                ).fetchone():
                    raise ValueError("连接器 ID 已用于其他类型的连接器")
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
                "INSERT INTO connectors(user_id,id,name,tool_type,auth_mode,created_at,updated_at) "
                "VALUES(?,?,?,'device','device',?,?) ON CONFLICT(user_id,id) DO NOTHING",
                (user_id, connector_id, clean_name, now, now),
            )
            if (
                self._db.execute(
                    "SELECT tool_type FROM connectors WHERE user_id=? AND id=?",
                    (user_id, connector_id),
                ).fetchone()[0]
                != "device"
            ):
                raise ValueError("连接器 ID 已用于其他类型的连接器")
            self._db.execute(
                "INSERT INTO device_connectors(user_id,id,name,device_id,config_json,verified,"
                "enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id,device_id,id) DO UPDATE SET name=excluded.name, "
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
            return self.get(user_id, connector_id, device_id=device_id)

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

    def delete(self, user_id: str, connector_id: str, *, device_id: str | None = None) -> None:
        with self._store._transaction():
            if device_id is None:
                cur = self._db.execute(
                    "DELETE FROM connectors WHERE user_id=? AND id=? AND tool_type!='device'",
                    (user_id, connector_id),
                )
                if not cur.rowcount:
                    raise LookupError(f"Unknown connector: {connector_id}")
                return
            cur = self._db.execute(
                "DELETE FROM device_connectors WHERE user_id=? AND id=? AND device_id=?",
                (user_id, connector_id, device_id),
            )
            if cur.rowcount == 0:
                raise LookupError(f"Unknown connector: {connector_id}")
            self._db.execute(
                "DELETE FROM connectors WHERE user_id=? AND id=? AND tool_type='device' "
                "AND NOT EXISTS (SELECT 1 FROM device_connectors WHERE user_id=? AND id=?)",
                (user_id, connector_id, user_id, connector_id),
            )

    def record_call(
        self,
        *,
        user_id: str,
        conversation_id: str,
        connector_id: str,
        connector_name: str,
        tool_type: ToolType,
        target: str,
        tool: str,
        arguments: Mapping[str, JsonValue],
        status: ConnectorCallStatus,
        output: str = "",
        error: str = "",
        created_at: float | None = None,
        completed_at: float | None = None,
    ) -> ConnectorCallRecord:
        now = self._store.now_utc().timestamp()
        started = created_at if created_at is not None else now
        finished = completed_at if completed_at is not None else now
        call_id = "cc_" + uuid4().hex
        truncated = False
        out_text = output
        err_text = error
        if len(out_text.encode("utf-8")) > 8192:
            out_text = out_text.encode("utf-8")[:8192].decode("utf-8", errors="ignore")
            truncated = True
        if len(err_text.encode("utf-8")) > 8192:
            err_text = err_text.encode("utf-8")[:8192].decode("utf-8", errors="ignore")
            truncated = True
        args_text = json.dumps(dict(arguments), ensure_ascii=False)
        with self._store._transaction():
            self._db.execute(
                "INSERT INTO connector_calls ("
                "id, user_id, conversation_id, connector_id, connector_name, tool_type, "
                "target, tool, arguments, status, output, error, truncated, created_at, "
                "completed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    call_id,
                    user_id,
                    conversation_id,
                    connector_id,
                    connector_name,
                    tool_type,
                    target,
                    tool,
                    args_text,
                    status,
                    out_text,
                    err_text,
                    int(truncated),
                    started,
                    finished,
                ),
            )
        return ConnectorCallRecord(
            id=call_id,
            connector_id=connector_id,
            connector_name=connector_name,
            tool_type=tool_type,
            target=target,
            tool=tool,
            arguments=dict(arguments),
            status=status,
            output=out_text,
            error=err_text,
            truncated=truncated,
            created_at=_dt(started),
            started_at=_dt(started),
            completed_at=_dt(finished),
        )

    def list_history(
        self,
        *,
        user_id: str,
        conversation_id: str,
        limit: int = 30,
        cursor: str | None = None,
    ) -> tuple[builtins.list[ConnectorCallRecord], str | None]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        before_ts: float | None = None
        before_id: str | None = None
        if cursor is not None:
            ts_part, sep, id_part = cursor.partition(":")
            if not sep or not id_part:
                raise ValueError("Invalid connector history cursor")
            try:
                before_ts = float(ts_part)
            except ValueError:
                raise ValueError("Invalid connector history cursor") from None
            before_id = id_part

        connector_names = {
            (row["device_id"], row["id"]): str(row["name"])
            for row in self._db.execute(
                "SELECT device_id, id, name FROM device_connectors WHERE user_id=?", (user_id,)
            ).fetchall()
        }

        mcp_where = "user_id=? AND conversation_id=?"
        mcp_params: list[object] = [user_id, conversation_id]
        if before_ts is not None and before_id is not None:
            mcp_where += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            mcp_params.extend([before_ts, before_ts, before_id])
        mcp_rows = self._db.execute(
            f"SELECT * FROM connector_calls WHERE {mcp_where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (*mcp_params, limit + 1),
        ).fetchall()

        dev_where = "user_id=? AND conversation_id=?"
        dev_params: list[object] = [user_id, conversation_id]
        if before_ts is not None and before_id is not None:
            dev_where += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            dev_params.extend([before_ts, before_ts, before_id])
        dev_rows = self._db.execute(
            f"SELECT * FROM remote_tool_calls WHERE {dev_where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (*dev_params, limit + 1),
        ).fetchall()

        merged: list[tuple[float, str, ConnectorCallRecord]] = []
        for row in mcp_rows:
            rec = ConnectorCallRecord(
                id=row["id"],
                connector_id=row["connector_id"],
                connector_name=row["connector_name"],
                tool_type=row["tool_type"],
                target=row["target"],
                tool=row["tool"],
                arguments=json.loads(row["arguments"]),
                status=row["status"],
                output=row["output"],
                error=row["error"],
                truncated=bool(row["truncated"]),
                created_at=_dt(row["created_at"]),
                started_at=_dt(row["created_at"]),
                completed_at=_dt(row["completed_at"]) if row["completed_at"] is not None else None,
            )
            merged.append((float(row["created_at"]), str(row["id"]), rec))

        for row in dev_rows:
            tool_name = str(row["tool"])
            if tool_name in _SHELL_TOOLS:
                cid = "terminal"
                default_name = "本机命令行"
            elif tool_name == "obsidian":
                cid = "obsidian"
                default_name = "Obsidian"
            else:
                cid = tool_name
                default_name = tool_name
            cname = connector_names.get((row["device_id"], cid), default_name)
            rec = ConnectorCallRecord(
                id=row["id"],
                connector_id=cid,
                connector_name=cname,
                tool_type="device",
                target=row["device_id"],
                tool=tool_name,
                arguments=json.loads(row["arguments"]),
                status=row["status"],
                output=row["stdout"],
                error=row["stderr"],
                exit_code=row["exit_code"],
                truncated=bool(row["truncated"]),
                timeout_seconds=row["timeout_seconds"],
                created_at=_dt(row["created_at"]),
                started_at=_dt(row["started_at"]) if row["started_at"] is not None else None,
                completed_at=_dt(row["completed_at"]) if row["completed_at"] is not None else None,
            )
            merged.append((float(row["created_at"]), str(row["id"]), rec))

        merged.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        has_more = len(merged) > limit
        page = merged[:limit]
        next_cursor = f"{page[-1][0]:.6f}:{page[-1][1]}" if has_more and page else None
        return [entry[2] for entry in page], next_cursor
