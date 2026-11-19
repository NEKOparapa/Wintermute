"""Five-tool connector suite: ConnectorList, ConnectorStatus, ConnectorSetup,
ConnectorCredentialSave, and Mcp."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.config.models import HTTPTerminalSettings
from wintermute.events.integrations.mcp_client import (
    McpAuthError,
    McpError,
    McpRpcError,
    McpToolInfo,
    StreamableHttpMcpClient,
    validate_mcp_endpoint,
    validate_tool_args,
)
from wintermute.events.integrations.terminal_tool import TerminalCapabilities
from wintermute.events.storage.connectors import ConnectorRecord, ConnectorSource
from wintermute.events.storage.sqlite import SQLiteStore

type NonEmptyStr = Annotated[str, Field(min_length=1, max_length=256)]


class ConnectorManager:
    """Shared runtime state (MCP client & tool schema cache) for connectors."""

    def __init__(self, mcp_client: StreamableHttpMcpClient | None = None) -> None:
        self.mcp = mcp_client or StreamableHttpMcpClient()
        self._tool_cache: dict[tuple[str, str, str], list[McpToolInfo]] = {}

    def invalidate(self, user_id: str, connector_id: str) -> None:
        keys = [k for k in self._tool_cache if k[0] == user_id and k[1] == connector_id]
        for key in keys:
            self._tool_cache.pop(key, None)

    async def get_tools(
        self,
        store: SQLiteStore,
        user_id: str,
        record: ConnectorRecord,
        connection_id: str | None = None,
        *,
        refresh: bool = False,
    ) -> list[McpToolInfo]:
        cred_pair = store.connectors.get_credentials(user_id, record.id, connection_id)
        resolved_conn = cred_pair[0] if cred_pair else (connection_id or "default")
        creds = cred_pair[1] if cred_pair else None
        cache_key = (user_id, record.id, resolved_conn)
        if not refresh and cache_key in self._tool_cache:
            return self._tool_cache[cache_key]
        tools = await self.mcp.list_tools(record, creds)
        self._tool_cache[cache_key] = tools
        return tools


def _is_connector_connected(
    record: ConnectorRecord,
    capabilities: TerminalCapabilities | None,
    bound_terminals: Mapping[str, str],
) -> tuple[bool, str]:
    if not record.enabled:
        return False, "disconnected"
    if record.tool_type == "device":
        device_id = record.device_id
        if not device_id or (bound_terminals and device_id not in bound_terminals):
            return False, "offline"
        if capabilities is None:
            return record.verified, "connected" if record.verified else "offline"
        advertised = capabilities.tools(device_id)
        if record.id in advertised:
            return True, "connected"
        return False, "offline"
    if record.tool_type == "mcp":
        if record.auth_mode == "credential" and not record.has_credentials:
            return False, "pending_auth"
        if record.verified:
            return True, "connected"
        return False, "pending_auth" if record.auth_mode != "none" else "unverified"
    return record.verified, "connected" if record.verified else "disconnected"


def _json_result(data: Mapping[str, object], *, is_error: bool = False) -> ToolResult:
    return ToolResult(json.dumps(data, ensure_ascii=False), is_error=is_error)


# ---------------------------------------------------------------------------
# 1. ConnectorList
# ---------------------------------------------------------------------------


class ConnectorListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def connector_list_tool(
    store: SQLiteStore,
    conversation_id: str,
    bindings: Sequence[HTTPTerminalSettings],
    capabilities: TerminalCapabilities | None,
) -> Tool[ConnectorListRequest]:
    user_id = store.get_conversation_user(conversation_id)
    bound_terminals = {
        t.terminal_id: t.display_name or t.terminal_id
        for t in bindings
        if t.user_id == user_id and t.conversation_id == conversation_id
    }

    async def execute(request: ConnectorListRequest, context: ToolContext) -> ToolResult:
        records = store.connectors.list(user_id)
        items: list[dict[str, object]] = []
        for rec in records:
            connected, status = _is_connector_connected(rec, capabilities, bound_terminals)
            item: dict[str, object] = {
                "id": rec.id,
                "name": rec.name,
                "toolType": rec.tool_type,
                "connected": connected,
                "status": status,
            }
            if rec.tool_type == "device" and rec.device_id:
                item["deviceId"] = rec.device_id
            if rec.tool_type == "mcp" and rec.connections:
                item["connections"] = [
                    {
                        "connectionId": c.connection_id,
                        "label": c.label,
                        "verified": c.verified,
                    }
                    for c in rec.connections
                ]
            items.append(item)
        return _json_result({"connectors": items})

    return Tool(
        "ConnectorList",
        "列出当前账号下可用的连接器，不带参数（传 {}）。每项返回 id、toolType"
        "（mcp / device / cli）、connected 和显示名 name。toolType 决定后续调用路径："
        "只有 mcp 可以交给 Mcp 工具调用；device 类型（如桌面端的 obsidian）需要走设备工具"
        " terminal_tool 调用。",
        ConnectorListRequest,
        execute,
    )


# ---------------------------------------------------------------------------
# 2. ConnectorStatus
# ---------------------------------------------------------------------------


class RequestedGrants(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot: list[str] = Field(default_factory=list)
    user: list[str] = Field(default_factory=list)


class ConnectorStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonEmptyStr = Field(description="连接器 id，例如 obsidian、gmail、google_calendar")
    deviceId: str | None = Field(default=None, min_length=1, max_length=256)
    scopes: list[str] | None = Field(default=None, description="可选，需要检查的 OAuth 权限范围")
    requestedGrants: RequestedGrants | None = Field(
        default=None, description="仅 Slack 使用，填写 bot 和 user 两个权限数组"
    )


def connector_status_tool(
    store: SQLiteStore,
    conversation_id: str,
    bindings: Sequence[HTTPTerminalSettings],
    capabilities: TerminalCapabilities | None,
) -> Tool[ConnectorStatusRequest]:
    user_id = store.get_conversation_user(conversation_id)
    bound_terminals = {
        t.terminal_id: t.display_name or t.terminal_id
        for t in bindings
        if t.user_id == user_id and t.conversation_id == conversation_id
    }

    async def execute(request: ConnectorStatusRequest, context: ToolContext) -> ToolResult:
        try:
            rec = store.connectors.get(user_id, request.id)
        except LookupError:
            return _json_result(
                {
                    "id": request.id,
                    "connected": False,
                    "status": "not_configured",
                    "message": "账号下尚未配置该连接器",
                }
            )
        if request.deviceId and rec.device_id and rec.device_id != request.deviceId:
            return _json_result(
                {
                    "id": rec.id,
                    "toolType": rec.tool_type,
                    "deviceId": request.deviceId,
                    "connected": False,
                    "status": "device_mismatch",
                }
            )
        connected, status = _is_connector_connected(rec, capabilities, bound_terminals)
        payload: dict[str, object] = {
            "id": rec.id,
            "name": rec.name,
            "toolType": rec.tool_type,
            "connected": connected,
            "status": status,
            "authMode": rec.auth_mode,
        }
        if rec.device_id:
            payload["deviceId"] = rec.device_id
        if rec.connections:
            payload["connections"] = [
                {
                    "connectionId": c.connection_id,
                    "label": c.label,
                    "verified": c.verified,
                }
                for c in rec.connections
            ]
        if request.scopes is not None:
            granted = set(rec.scopes)
            missing = [s for s in request.scopes if s not in granted]
            payload["missingScopes"] = missing
            if missing:
                payload["connected"] = False
                payload["status"] = "missing_scopes"
        if request.requestedGrants is not None:
            bot_granted = set(rec.grants.get("bot", []))
            user_granted = set(rec.grants.get("user", []))
            missing_bot = [s for s in request.requestedGrants.bot if s not in bot_granted]
            missing_user = [s for s in request.requestedGrants.user if s not in user_granted]
            payload["missingGrants"] = {"bot": missing_bot, "user": missing_user}
            if missing_bot or missing_user:
                payload["connected"] = False
                payload["status"] = "missing_grants"
        return _json_result(payload)

    return Tool(
        "ConnectorStatus",
        "查询某个连接器当前的授权和在线状态，只报状态，不报它能执行哪些工具。"
        "支持按 deviceId 指定设备、按 scopes 检查 OAuth 权限范围，或对 Slack 传 requestedGrants。",
        ConnectorStatusRequest,
        execute,
    )


# ---------------------------------------------------------------------------
# 3. ConnectorSetup
# ---------------------------------------------------------------------------


class ConnectorSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["official", "third_party"]
    foundAt: str = Field(min_length=1, max_length=2048, description="官方文档地址")
    publisher: str = Field(min_length=1, max_length=256, description="服务运营方")


class ConnectorSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(
        min_length=1,
        max_length=2048,
        description="从官方文档 MCP 配置原样复制的 https Streamable HTTP 地址，不能自己拼",
    )
    name: str = Field(min_length=1, max_length=128, description="连接器显示名")
    source: ConnectorSourceInput
    headerNames: list[str] = Field(
        default_factory=list,
        max_length=8,
        description='密钥所在请求头，例如 ["X-Api-Key"] 或 ["Authorization"]',
    )
    credentialLabel: str | None = Field(
        default=None, max_length=64, description="凭据展示名称，如 API Key 或 Token"
    )
    credentialQueryParam: str | None = Field(
        default=None, max_length=128, description="密钥放在 URL 参数里时填写参数名"
    )


def connector_setup_tool(
    store: SQLiteStore,
    conversation_id: str,
    manager: ConnectorManager,
) -> Tool[ConnectorSetupRequest]:
    user_id = store.get_conversation_user(conversation_id)

    async def execute(request: ConnectorSetupRequest, context: ToolContext) -> ToolResult:
        try:
            endpoint = validate_mcp_endpoint(request.endpoint)
        except ValueError as exc:
            return _json_result({"error": str(exc)}, is_error=True)

        needs_credentials = bool(
            request.headerNames or request.credentialQueryParam or request.credentialLabel
        )
        source = ConnectorSource(
            kind=request.source.kind,
            found_at=request.source.foundAt,
            publisher=request.source.publisher,
        )
        record = store.connectors.upsert_mcp(
            user_id,
            name=request.name,
            endpoint=endpoint,
            source=source,
            header_names=request.headerNames,
            credential_label=request.credentialLabel,
            credential_query_param=request.credentialQueryParam,
            verified=False,
        )
        manager.invalidate(user_id, record.id)

        if needs_credentials:
            return _json_result(
                {
                    "id": record.id,
                    "name": record.name,
                    "toolType": "mcp",
                    "status": "pending_auth",
                    "thirdParty": True,
                    "credentialFields": (
                        request.headerNames
                        or ([request.credentialQueryParam] if request.credentialQueryParam else [])
                        or ([request.credentialLabel] if request.credentialLabel else ["Token"])
                    ),
                    "message": (
                        "连接器已登记为待授权状态（第三方服务，未经平台审核）。"
                        "请引导用户在客户端「设置 → 连接器」中填写凭据并验证。"
                    ),
                }
            )

        # No credentials declared: probe Streamable HTTP endpoint immediately.
        try:
            tools = await manager.get_tools(store, user_id, record, refresh=True)
        except McpAuthError:
            return _json_result(
                {
                    "id": record.id,
                    "name": record.name,
                    "toolType": "mcp",
                    "status": "pending_auth",
                    "thirdParty": True,
                    "message": (
                        "远程 MCP 服务器要求鉴权。请引导用户在客户端「设置 → 连接器」中配置凭据。"
                    ),
                }
            )
        except McpError as exc:
            return _json_result(
                {
                    "id": record.id,
                    "name": record.name,
                    "toolType": "mcp",
                    "status": "unverified",
                    "thirdParty": True,
                    "error": str(exc),
                },
                is_error=True,
            )

        store.connectors.set_verified(user_id, record.id, True)
        return _json_result(
            {
                "id": record.id,
                "name": record.name,
                "toolType": "mcp",
                "status": "connected",
                "thirdParty": True,
                "toolCount": len(tools),
                "message": "远程 Streamable HTTP MCP 服务器已连接并验证通过。",
            }
        )

    return Tool(
        "ConnectorSetup",
        "给不在目录里的服务添加远程 MCP 服务器。只支持 https 且必须为 Streamable HTTP，"
        "老的 SSE 地址可能连不上。endpoint 必须从官方文档的 MCP 配置里原样复制，不能自己拼。"
        "本工具不接收任何密钥值；需要凭据时会返回 pending_auth，由设置页或卡片收集。",
        ConnectorSetupRequest,
        execute,
    )


# ---------------------------------------------------------------------------
# 4. ConnectorCredentialSave
# ---------------------------------------------------------------------------


class ConnectorCredentialSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connectorId: NonEmptyStr = Field(description="目标连接器 id")
    values: dict[str, str] = Field(
        min_length=1, description="按凭据字段名填写的密钥键值对，保存后立即验证且不回显"
    )
    connectionId: str = Field(
        default="default", min_length=1, max_length=64, description="多账号场景下的连接标识"
    )


def connector_credential_save_tool(
    store: SQLiteStore,
    conversation_id: str,
    manager: ConnectorManager,
) -> Tool[ConnectorCredentialSaveRequest]:
    user_id = store.get_conversation_user(conversation_id)

    async def execute(
        request: ConnectorCredentialSaveRequest, context: ToolContext
    ) -> ToolResult:
        try:
            record = store.connectors.save_credentials(
                user_id,
                request.connectorId,
                request.values,
                connection_id=request.connectionId,
                verified=False,
            )
        except LookupError:
            return _json_result(
                {"error": f"找不到连接器 {request.connectorId}"}, is_error=True
            )
        except ValueError as exc:
            return _json_result({"error": str(exc)}, is_error=True)

        manager.invalidate(user_id, record.id)
        if record.tool_type != "mcp":
            store.connectors.set_verified(
                user_id, record.id, True, connection_id=request.connectionId
            )
            return _json_result(
                {
                    "connectorId": record.id,
                    "connectionId": request.connectionId,
                    "verified": True,
                    "message": "凭据已安全保存（内容不回显）。",
                }
            )

        try:
            tools = await manager.get_tools(
                store, user_id, record, request.connectionId, refresh=True
            )
        except McpError as exc:
            return _json_result(
                {
                    "connectorId": record.id,
                    "connectionId": request.connectionId,
                    "verified": False,
                    "error": f"凭据已保存但验证失败：{exc}",
                },
                is_error=True,
            )

        store.connectors.set_verified(
            user_id, record.id, True, connection_id=request.connectionId
        )
        return _json_result(
            {
                "connectorId": record.id,
                "connectionId": request.connectionId,
                "verified": True,
                "toolCount": len(tools),
                "message": "凭据已保存并验证通过（内容不回显）。",
            }
        )

    return Tool(
        "ConnectorCredentialSave",
        "仅在用户已经把凭据直接发到聊天里时使用。按凭据字段名保存 values 后立即验证，"
        "不会回显任何密钥内容。正常情况下优先引导用户在「设置 → 连接器」中填写凭据。",
        ConnectorCredentialSaveRequest,
        execute,
    )


# ---------------------------------------------------------------------------
# 5. Mcp
# ---------------------------------------------------------------------------


class McpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["search", "list", "describe", "call"]
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    query: str | None = Field(default=None, min_length=1, max_length=256)
    tool: str | None = Field(default=None, min_length=1, max_length=256)
    args: dict[str, JsonValue] | None = None
    limit: int = Field(default=10, ge=1, le=50)
    connectionId: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def check_action(self) -> Self:
        if self.action == "search":
            if not self.query:
                raise ValueError("search 必须提供 query")
        elif self.action == "list":
            if not self.provider:
                raise ValueError("list 必须提供 provider")
        elif self.action == "describe":
            if not self.provider or not self.tool:
                raise ValueError("describe 必须提供 provider 和确切的 tool 名称")
        elif self.action == "call":
            if not self.provider or not self.tool or self.args is None:
                raise ValueError("call 必须提供 provider、tool 和 args（无参数也要传 {}）")
        return self


def mcp_tool(
    store: SQLiteStore,
    conversation_id: str,
    manager: ConnectorManager,
) -> Tool[McpRequest]:
    user_id = store.get_conversation_user(conversation_id)

    async def execute(request: McpRequest, context: ToolContext) -> ToolResult:
        if request.action == "search":
            assert request.query is not None
            needle = request.query.lower()
            records = store.connectors.list(user_id)
            targets = [
                r
                for r in records
                if r.tool_type == "mcp"
                and r.enabled
                and r.verified
                and (request.provider is None or r.id == request.provider)
            ]
            matches: list[dict[str, object]] = []
            errors: list[dict[str, str]] = []
            for rec in targets:
                try:
                    tools = await manager.get_tools(store, user_id, rec, request.connectionId)
                except McpError as exc:
                    errors.append({"provider": rec.id, "error": str(exc)})
                    continue
                for info in tools:
                    haystack = f"{rec.id} {rec.name} {info.name} {info.description}".lower()
                    if needle in haystack:
                        matches.append(
                            {
                                "provider": rec.id,
                                "tool": info.name,
                                "description": info.description,
                            }
                        )
                        if len(matches) >= request.limit:
                            break
                if len(matches) >= request.limit:
                    break
            return _json_result({"matches": matches, "errors": errors})

        assert request.provider is not None
        try:
            rec = store.connectors.get(user_id, request.provider)
        except LookupError:
            return _json_result({"error": f"找不到连接器 {request.provider}"}, is_error=True)

        if rec.tool_type != "mcp":
            return _json_result(
                {
                    "error": (
                        f"连接器 {rec.id} 的 toolType 为 {rec.tool_type}，不能交给 Mcp 调用；"
                        "device 类型请使用 terminal_tool。"
                    )
                },
                is_error=True,
            )

        try:
            tools = await manager.get_tools(store, user_id, rec, request.connectionId)
        except McpError as exc:
            return _json_result({"error": str(exc)}, is_error=True)

        if request.action == "list":
            return _json_result(
                {
                    "provider": rec.id,
                    "tools": [
                        {"tool": t.name, "description": t.description}
                        for t in tools
                    ],
                }
            )

        assert request.tool is not None
        matched_tool = next((t for t in tools if t.name == request.tool), None)
        if matched_tool is None:
            return _json_result(
                {
                    "error": (
                        f"连接器 {rec.id} 中找不到工具 {request.tool}，"
                        "请先用 list 确认确切名称"
                    ),
                    "availableTools": [t.name for t in tools[:30]],
                },
                is_error=True,
            )

        if request.action == "describe":
            return _json_result(
                {
                    "provider": rec.id,
                    "tool": matched_tool.name,
                    "description": matched_tool.description,
                    "inputSchema": matched_tool.input_schema,
                }
            )

        # action == "call"
        assert request.args is not None
        schema_err = validate_tool_args(matched_tool.input_schema, request.args)
        if schema_err is not None:
            return _json_result(
                {
                    "error": f"参数校验失败：{schema_err}。请对照 inputSchema 修改后重试。",
                    "provider": rec.id,
                    "tool": matched_tool.name,
                    "inputSchema": matched_tool.input_schema,
                },
                is_error=True,
            )

        cred_pair = store.connectors.get_credentials(user_id, rec.id, request.connectionId)
        creds = cred_pair[1] if cred_pair else None
        try:
            result = await manager.mcp.call_tool(rec, matched_tool.name, request.args, creds)
        except McpRpcError as exc:
            payload: dict[str, object] = {
                "error": f"MCP 工具调用失败 (code={exc.code}): {exc.rpc_message}",
                "provider": rec.id,
                "tool": matched_tool.name,
            }
            if exc.code == -32602:
                payload["inputSchema"] = matched_tool.input_schema
            return _json_result(payload, is_error=True)
        except McpError as exc:
            return _json_result({"error": str(exc)}, is_error=True)

        return _json_result(
            {
                "provider": rec.id,
                "tool": matched_tool.name,
                "result": result,
            },
            is_error=bool(result.get("isError")),
        )

    return Tool(
        "Mcp",
        "调用 toolType 为 mcp 的连接器工具。支持四种动作：search（全局按 query 搜索工具）、"
        "list（列出指定 provider 的工具）、describe（查看单个工具的参数 schema）、"
        "call（执行指定工具，无参数时 args 也必须传 {}）。"
        "多账号时通过 connectionId 选择。参数传错时会返回 inputSchema，照着修改后重试一次。",
        McpRequest,
        execute,
    )


def create_connector_tools(
    store: SQLiteStore,
    conversation_id: str,
    bindings: Sequence[HTTPTerminalSettings],
    capabilities: TerminalCapabilities | None,
    manager: ConnectorManager,
) -> tuple[Tool[Any], ...]:
    return (
        connector_list_tool(store, conversation_id, bindings, capabilities),
        connector_status_tool(store, conversation_id, bindings, capabilities),
        connector_setup_tool(store, conversation_id, manager),
        connector_credential_save_tool(store, conversation_id, manager),
        mcp_tool(store, conversation_id, manager),
    )
