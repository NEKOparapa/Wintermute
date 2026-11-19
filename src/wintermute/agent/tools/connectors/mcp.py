"""Discover and call tools on account and device connectors."""

from __future__ import annotations

import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.events.integrations.connectors import ConnectorManager, device_connector_tools
from wintermute.events.integrations.mcp_client import (
    McpError,
    McpRpcError,
    McpToolInfo,
    validate_tool_args,
)
from wintermute.events.storage.repositories.connectors import ConnectorRecord
from wintermute.events.storage.sqlite import SQLiteStore

from .common import json_result
from .device_arguments import device_tool_info, validate_device_arguments
from .device_calls import DeviceConnectorCalls
from .providers import RECEIPTS, BuiltinConnector, Provider, device_providers, provider_key


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
                device_providers(store, user_id, bound_devices, device_calls.capabilities)
            )
        records.append(RECEIPTS)
        targets = [rec for rec in records if request.provider in (None, rec.id, provider_key(rec))]
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
                provider = provider_key(rec)
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
            return json_result({"matches": matches, "errors": errors})

        assert request.provider is not None
        if not targets:
            return json_result({"error": f"找不到连接器 {request.provider}"}, is_error=True)
        if len(targets) > 1:
            return json_result(
                {
                    "error": "多台设备安装了该连接器，请选择一个完整的 provider。",
                    "providers": [provider_key(record) for record in targets],
                },
                is_error=True,
            )
        rec = targets[0]
        provider = provider_key(rec)
        try:
            tools = await available_tools(rec, request.connectionId)
        except McpError as exc:
            return json_result({"error": str(exc)}, is_error=True)

        if request.action == "list":
            return json_result(
                {
                    "provider": provider,
                    "tools": [{"tool": t.name, "description": t.description} for t in tools],
                }
            )

        assert request.tool is not None
        matched_tool = next((t for t in tools if t.name == request.tool), None)
        if matched_tool is None:
            return json_result(
                {
                    "error": (
                        f"连接器 {rec.id} 中找不到工具 {request.tool}，请先用 list 确认确切名称"
                    ),
                    "availableTools": [t.name for t in tools[:30]],
                },
                is_error=True,
            )

        if request.action == "describe":
            return json_result(
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
            return json_result(
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
                return json_result({"error": str(exc), "provider": provider}, is_error=True)
            return json_result(
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
                return json_result({"error": "本会话找不到该调用"}, is_error=True)
            return json_result(
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
            return json_result(payload, is_error=True)
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
            return json_result({"error": str(exc)}, is_error=True)

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
        return json_result(
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
