"""Register and verify a remote MCP connector."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.events.integrations.connectors import ConnectorManager
from wintermute.events.integrations.mcp_client import McpAuthError, McpError, validate_mcp_endpoint
from wintermute.events.storage.repositories.connectors import ConnectorSource
from wintermute.events.storage.sqlite import SQLiteStore

from .common import json_result


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
            return json_result({"error": str(exc)}, is_error=True)

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
            return json_result(
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
            return json_result(
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
            return json_result(
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
        return json_result(
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
