"""Connector management, discovery and MCP invocation tools."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.agent.tools.device_arguments import device_tool_info, validate_device_arguments
from wintermute.agent.tools.device_connectors import (
    RECEIPTS,
    BuiltinConnector,
    DeviceConnectorCalls,
    native_connector,
)
from wintermute.config.models import HTTPDeviceSettings
from wintermute.events.integrations.connectors import (
    ConnectorManager,
    device_connector_tools,
    is_connector_connected,
)
from wintermute.events.integrations.devices import DeviceCapabilities
from wintermute.events.integrations.mcp_client import (
    McpAuthError,
    McpError,
    McpRpcError,
    McpToolInfo,
    validate_mcp_endpoint,
    validate_tool_args,
)
from wintermute.events.storage.repositories.connectors import ConnectorRecord, ConnectorSource
from wintermute.events.storage.sqlite import SQLiteStore

type NonEmptyStr = Annotated[str, Field(min_length=1, max_length=256)]


def _json_result(data: Mapping[str, object], *, is_error: bool = False) -> ToolResult:
    return ToolResult(json.dumps(data, ensure_ascii=False), is_error=is_error)


type Provider = ConnectorRecord | BuiltinConnector


def _provider(record: Provider) -> str:
    return f"{record.id}@{record.device_id}" if record.device_id is not None else record.id


def _native_summary(record: BuiltinConnector) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "provider": _provider(record),
        "device_id": record.device_id,
        "toolType": "device",
        "authMode": "device",
        "verified": True,
        "enabled": True,
        "connected": True,
        "status": "connected",
        "tools": [record.tool.name],
    }


def _device_providers(
    store: SQLiteStore,
    user_id: str,
    devices: Collection[str],
    capabilities: DeviceCapabilities | None,
) -> list[Provider]:
    installed = store.connectors.list_devices(user_id, devices)
    managed = {rec.device_id for rec in installed if "device_status" in device_connector_tools(rec)}
    providers: list[Provider] = list(installed)
    for device_id in sorted(devices):
        if device_id not in managed and (native := native_connector(device_id, capabilities)):
            providers.append(native)
    return providers


# ---------------------------------------------------------------------------
# ConnectorList
# ---------------------------------------------------------------------------


class RequestedGrants(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot: list[str] = Field(default_factory=list)
    user: list[str] = Field(default_factory=list)


class ConnectorListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonEmptyStr | None = Field(default=None, description="按连接器 ID 精确筛选，例如 obsidian")
    device_id: NonEmptyStr | None = Field(default=None, description="只列出本会话中指定设备的结果")
    scopes: list[str] | None = Field(default=None, description="需要核对的 OAuth 权限范围")
    requestedGrants: RequestedGrants | None = Field(
        default=None, description="需要核对的 Slack bot 和 user 权限"
    )


def connector_list_tool(
    store: SQLiteStore,
    conversation_id: str,
    bindings: Sequence[HTTPDeviceSettings],
    capabilities: DeviceCapabilities | None,
) -> Tool[ConnectorListRequest]:
    user_id = store.get_conversation_user(conversation_id)
    bound_devices = {
        t.device_id: t.display_name or t.device_id
        for t in bindings
        if t.user_id == user_id and t.conversation_id == conversation_id
    }

    def summary(record: Provider, request: ConnectorListRequest) -> dict[str, object]:
        if isinstance(record, BuiltinConnector):
            return _native_summary(record)
        connected, status = is_connector_connected(record, capabilities, bound_devices)
        item: dict[str, object] = {
            "id": record.id,
            "name": record.name,
            "provider": _provider(record),
            "toolType": record.tool_type,
            "authMode": record.auth_mode,
            "verified": record.verified,
            "enabled": record.enabled,
            "connected": connected,
            "status": status,
        }
        if record.tool_type == "device":
            item["device_id"] = record.device_id
            item["tools"] = device_connector_tools(record)
            return item
        if record.connections:
            item["connections"] = [
                {"connectionId": c.connection_id, "label": c.label, "verified": c.verified}
                for c in record.connections
            ]
        if request.scopes is not None and (
            request.id is not None or record.auth_mode == "oauth" or record.scopes
        ):
            missing = [scope for scope in request.scopes if scope not in record.scopes]
            item["missingScopes"] = missing
            if missing:
                item.update(connected=False, status="missing_scopes")
        if request.requestedGrants is not None and (
            request.id is not None or record.id == "slack" or record.grants
        ):
            missing_grants = {
                role: [grant for grant in requested if grant not in record.grants.get(role, [])]
                for role, requested in (
                    ("bot", request.requestedGrants.bot),
                    ("user", request.requestedGrants.user),
                )
            }
            item["missingGrants"] = missing_grants
            if any(missing_grants.values()):
                item.update(connected=False, status="missing_grants")
        return item

    async def execute(request: ConnectorListRequest, context: ToolContext) -> ToolResult:
        installed: dict[str, list[dict[str, object]]] = {
            device_id: []
            for device_id in bound_devices
            if request.device_id is None or request.device_id == device_id
        }
        for record in _device_providers(store, user_id, installed, capabilities):
            assert record.device_id is not None
            if request.id is None or request.id == record.id:
                installed[record.device_id].append(summary(record, request))
        devices = []
        names = store.devices.names(user_id)
        for device_id, connectors in installed.items():
            if request.id is not None and not connectors:
                continue
            status, age = capabilities.presence(device_id) if capabilities else ("unknown", None)
            devices.append(
                {
                    "device_id": device_id,
                    "name": names.get(device_id) or bound_devices[device_id],
                    "status": status,
                    "lastSeenSecondsAgo": age,
                    "tools": capabilities.tools(device_id) if capabilities else (),
                    "connectors": connectors,
                }
            )
        account_connectors = []
        if request.device_id is None:
            account_connectors = [
                summary(rec, request)
                for rec in store.connectors.list_account(user_id)
                if request.id is None or request.id == rec.id
            ]
        return _json_result({"devices": devices, "accountConnectors": account_connectors})

    return Tool(
        "ConnectorList",
        "按设备列出本会话的连接器，返回设备在线状态、连接器的启用、授权和可用状态。"
        "id 和 device_id 可单独或组合筛选。同一连接器在多台设备上分别列出。"
        "未指定设备时也列出 accountConnectors。传 scopes 或 requestedGrants 可核对账号连接器"
        "缺少的权限，分别返回 missingScopes 或 missingGrants。"
        "将连接器的 provider 原样传给 Mcp 即可查看或调用工具。",
        ConnectorListRequest,
        execute,
    )


# ---------------------------------------------------------------------------
# ConnectorSetup
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
# ConnectorCredentialSave
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

    async def execute(request: ConnectorCredentialSaveRequest, context: ToolContext) -> ToolResult:
        try:
            record = store.connectors.save_credentials(
                user_id,
                request.connectorId,
                request.values,
                connection_id=request.connectionId,
                verified=False,
            )
        except LookupError:
            return _json_result({"error": f"找不到连接器 {request.connectorId}"}, is_error=True)
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

        store.connectors.set_verified(user_id, record.id, True, connection_id=request.connectionId)
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
# Mcp
# ---------------------------------------------------------------------------


class McpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["search", "list", "describe", "call"]
    provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=321,
        description="连接器 ID。选择特定设备时使用搜索或 ConnectorList 返回的完整 provider。",
    )
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
    device_calls: DeviceConnectorCalls | None,
) -> Tool[McpRequest]:
    user_id = store.get_conversation_user(conversation_id)
    bound_devices = device_calls.targets if device_calls else set()

    async def available_tools(rec: Provider, connection_id: str | None) -> list[McpToolInfo]:
        if isinstance(rec, BuiltinConnector):
            if connection_id is not None:
                raise McpError("connectionId 仅用于选择服务账号")
            return [rec.tool]
        if not rec.enabled:
            raise McpError("连接器已停用")
        if rec.tool_type == "mcp":
            accounts = [c.connection_id for c in rec.connections]
            if not accounts and rec.auth_mode == "none":
                accounts = ["default"]
            if connection_id is not None and connection_id not in accounts:
                raise McpError("找不到指定的连接器账号")
            return await manager.get_tools(store, user_id, rec, connection_id)
        if rec.tool_type != "device":
            raise McpError(f"该类型的连接器尚无调用实现：{rec.tool_type}")
        if connection_id is not None:
            raise McpError("connectionId 用于选择服务账号，设备由 provider 指定")
        advertised = (
            device_calls.capabilities.tools(rec.device_id) if device_calls and rec.device_id else ()
        )
        tools = [
            device_tool_info(name) for name in device_connector_tools(rec) if name in advertised
        ]
        if not tools:
            raise McpError("该设备当前未上报此连接器的工具")
        return tools

    async def execute(request: McpRequest, context: ToolContext) -> ToolResult:
        records: list[Provider] = list(store.connectors.list_account(user_id))
        if device_calls is not None:
            records.extend(
                _device_providers(store, user_id, bound_devices, device_calls.capabilities)
            )
        records.append(RECEIPTS)
        targets = [rec for rec in records if request.provider in (None, rec.id, _provider(rec))]
        if request.action == "search":
            assert request.query is not None
            needle = request.query.casefold()
            matches: list[dict[str, object]] = []
            errors: list[dict[str, str]] = []
            for rec in targets:
                if isinstance(rec, ConnectorRecord) and (
                    not rec.enabled or not rec.verified or rec.tool_type not in ("mcp", "device")
                ):
                    continue
                if (
                    request.connectionId
                    and request.provider is None
                    and (isinstance(rec, BuiltinConnector) or rec.tool_type == "device")
                ):
                    continue
                provider = _provider(rec)
                try:
                    tools = await available_tools(rec, request.connectionId)
                except McpError as exc:
                    errors.append({"provider": provider, "error": str(exc)})
                    continue
                for info in tools:
                    haystack = f"{provider} {rec.name} {info.name} {info.description}".casefold()
                    if needle in haystack:
                        matches.append(
                            {
                                "provider": provider,
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
        if not targets:
            return _json_result({"error": f"找不到连接器 {request.provider}"}, is_error=True)
        if len(targets) > 1:
            return _json_result(
                {
                    "error": "多台设备安装了该连接器，请选择一个完整的 provider。",
                    "providers": [_provider(record) for record in targets],
                },
                is_error=True,
            )
        rec = targets[0]
        provider = _provider(rec)
        try:
            tools = await available_tools(rec, request.connectionId)
        except McpError as exc:
            return _json_result({"error": str(exc)}, is_error=True)

        if request.action == "list":
            return _json_result(
                {
                    "provider": provider,
                    "tools": [{"tool": t.name, "description": t.description} for t in tools],
                }
            )

        assert request.tool is not None
        matched_tool = next((t for t in tools if t.name == request.tool), None)
        if matched_tool is None:
            return _json_result(
                {
                    "error": (
                        f"连接器 {rec.id} 中找不到工具 {request.tool}，请先用 list 确认确切名称"
                    ),
                    "availableTools": [t.name for t in tools[:30]],
                },
                is_error=True,
            )

        if request.action == "describe":
            return _json_result(
                {
                    "provider": provider,
                    "tool": matched_tool.name,
                    "description": matched_tool.description,
                    "inputSchema": matched_tool.input_schema,
                }
            )

        # action == "call"
        assert request.args is not None
        if rec.device_id is not None:
            try:
                validate_device_arguments(matched_tool.name, request.args)
                schema_err = None
            except ValueError as exc:
                schema_err = str(exc)
        else:
            schema_err = validate_tool_args(matched_tool.input_schema, request.args)
        if schema_err is not None:
            return _json_result(
                {
                    "error": f"参数校验失败：{schema_err}。请对照 inputSchema 修改后重试。",
                    "provider": provider,
                    "tool": matched_tool.name,
                    "inputSchema": matched_tool.input_schema,
                },
                is_error=True,
            )

        if rec.device_id is not None:
            assert device_calls is not None
            try:
                receipt = device_calls.submit(
                    rec.device_id, matched_tool.name, request.args, context
                )
            except (ValueError, LookupError) as exc:
                return _json_result({"error": str(exc), "provider": provider}, is_error=True)
            return _json_result(
                {"provider": provider, "tool": matched_tool.name, "result": receipt}
            )

        if isinstance(rec, BuiltinConnector):
            call_id = request.args["id"]
            assert isinstance(call_id, str)  # validated against the receipt tool's schema
            try:
                call = store.remote_tools.get(
                    user_id=user_id, conversation_id=conversation_id, id=call_id
                )
            except LookupError:
                return _json_result({"error": "本会话找不到该调用"}, is_error=True)
            return _json_result(
                {
                    "provider": provider,
                    "tool": matched_tool.name,
                    "result": call.model_dump(mode="json"),
                }
            )

        cred_pair = store.connectors.get_credentials(user_id, rec.id, request.connectionId)
        resolved_target = cred_pair[0] if cred_pair else (request.connectionId or "default")
        creds = cred_pair[1] if cred_pair else None
        started_ts = store.now_utc().timestamp()
        try:
            result = await manager.mcp.call_tool(rec, matched_tool.name, request.args, creds)
        except McpRpcError as exc:
            err_msg = f"MCP 工具调用失败 (code={exc.code}): {exc.rpc_message}"
            store.connectors.record_call(
                user_id=user_id,
                conversation_id=conversation_id,
                connector_id=rec.id,
                connector_name=rec.name,
                tool_type="mcp",
                target=resolved_target,
                tool=matched_tool.name,
                arguments=request.args,
                status="failed",
                error=err_msg,
                created_at=started_ts,
            )
            payload: dict[str, object] = {
                "error": err_msg,
                "provider": rec.id,
                "tool": matched_tool.name,
            }
            if exc.code == -32602:
                payload["inputSchema"] = matched_tool.input_schema
            return _json_result(payload, is_error=True)
        except McpError as exc:
            store.connectors.record_call(
                user_id=user_id,
                conversation_id=conversation_id,
                connector_id=rec.id,
                connector_name=rec.name,
                tool_type="mcp",
                target=resolved_target,
                tool=matched_tool.name,
                arguments=request.args,
                status="failed",
                error=str(exc),
                created_at=started_ts,
            )
            return _json_result({"error": str(exc)}, is_error=True)

        is_err = bool(result.get("isError"))
        result_text = json.dumps(result, ensure_ascii=False)
        store.connectors.record_call(
            user_id=user_id,
            conversation_id=conversation_id,
            connector_id=rec.id,
            connector_name=rec.name,
            tool_type="mcp",
            target=resolved_target,
            tool=matched_tool.name,
            arguments=request.args,
            status="failed" if is_err else "succeeded",
            output="" if is_err else result_text,
            error=result_text if is_err else "",
            created_at=started_ts,
        )
        return _json_result(
            {
                "provider": rec.id,
                "tool": matched_tool.name,
                "result": result,
                **({"inputSchema": matched_tool.input_schema} if is_err else {}),
            },
            is_error=is_err,
        )

    return Tool(
        "Mcp",
        "发现并调用账号和设备连接器。支持四种动作：search（全局按 query 搜索工具）、"
        "list（列出指定 provider 的工具）、describe（查看单个工具的参数 schema）、"
        "call（执行指定工具，无参数时 args 也必须传 {}）。"
        "多账号用 connectionId 选择。设备使用 search 或 ConnectorList 返回的完整 provider。"
        "设备调用会先返回排队回执，执行结果随后送达本会话。"
        "需要查询已有回执时，用 call 调用 wintermute:receipts 的 get_call_result，args 传入 id。"
        "参数传错时会返回 inputSchema，照着修改后重试一次。",
        McpRequest,
        execute,
    )


def create_connector_tools(
    store: SQLiteStore,
    conversation_id: str,
    bindings: Sequence[HTTPDeviceSettings],
    capabilities: DeviceCapabilities | None,
    manager: ConnectorManager,
    device_calls: DeviceConnectorCalls | None,
) -> tuple[Tool[Any], ...]:
    return (
        connector_list_tool(store, conversation_id, bindings, capabilities),
        connector_setup_tool(store, conversation_id, manager),
        connector_credential_save_tool(store, conversation_id, manager),
        mcp_tool(store, conversation_id, manager, device_calls),
    )
