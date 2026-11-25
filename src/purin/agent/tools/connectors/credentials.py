"""Save credentials for a connector account and verify the connection."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from purin.agent import Tool, ToolContext, ToolResult
from purin.events.integrations.connectors import ConnectorManager
from purin.events.integrations.mcp_client import McpError
from purin.events.storage.sqlite import SQLiteStore

from .common import NonEmptyStr, json_result


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
            return json_result({"error": f"找不到连接器 {request.connectorId}"}, is_error=True)
        except ValueError as exc:
            return json_result({"error": str(exc)}, is_error=True)

        manager.invalidate(user_id, record.id)
        if record.tool_type != "mcp":
            store.connectors.set_verified(
                user_id, record.id, True, connection_id=request.connectionId
            )
            return json_result(
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
            return json_result(
                {
                    "connectorId": record.id,
                    "connectionId": request.connectionId,
                    "verified": False,
                    "error": f"凭据已保存但验证失败：{exc}",
                },
                is_error=True,
            )

        store.connectors.set_verified(user_id, record.id, True, connection_id=request.connectionId)
        return json_result(
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
