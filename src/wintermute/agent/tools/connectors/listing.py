"""List account connectors and the connectors enabled on each device."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.config.models import HTTPDeviceBinding
from wintermute.events.integrations.connectors import device_connector_tools, is_connector_connected
from wintermute.events.integrations.devices import DeviceCapabilities
from wintermute.events.storage.sqlite import SQLiteStore

from .common import NonEmptyStr, json_result
from .providers import BuiltinConnector, Provider, device_providers, provider_key


def _native_summary(record: BuiltinConnector) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "provider": provider_key(record),
        "device_id": record.device_id,
        "toolType": "device",
        "authMode": "device",
        "verified": True,
        "enabled": True,
        "connected": True,
        "status": "connected",
        "tools": [record.tool.name],
    }


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
    bindings: Sequence[HTTPDeviceBinding] | Callable[[], Sequence[HTTPDeviceBinding]],
    capabilities: DeviceCapabilities | None,
) -> Tool[ConnectorListRequest]:
    user_id = store.get_conversation_user(conversation_id)

    def summary(
        record: Provider, request: ConnectorListRequest, bound_devices: dict[str, str]
    ) -> dict[str, object]:
        if isinstance(record, BuiltinConnector):
            return _native_summary(record)
        connected, status = is_connector_connected(record, capabilities, bound_devices)
        item: dict[str, object] = {
            "id": record.id,
            "name": record.name,
            "provider": provider_key(record),
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
        current = bindings() if callable(bindings) else bindings
        bound_devices = {
            t.device_id: t.display_name or t.device_id
            for t in current
            if t.user_id == user_id and t.conversation_id == conversation_id
        }
        installed: dict[str, list[dict[str, object]]] = {
            device_id: []
            for device_id in bound_devices
            if request.device_id is None or request.device_id == device_id
        }
        for record in device_providers(store, user_id, installed, capabilities):
            assert record.device_id is not None
            if request.id is None or request.id == record.id:
                installed[record.device_id].append(summary(record, request, bound_devices))
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
                summary(rec, request, bound_devices)
                for rec in store.connectors.list_account(user_id)
                if request.id is None or request.id == rec.id
            ]
        return json_result({"devices": devices, "accountConnectors": account_connectors})

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
