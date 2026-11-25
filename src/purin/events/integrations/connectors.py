"""Connector state shared by Agent tools and the HTTP settings interface."""

from __future__ import annotations

from collections.abc import Mapping

from purin.events.integrations.devices import DeviceCapabilities
from purin.events.integrations.mcp_client import McpToolInfo, StreamableHttpMcpClient
from purin.events.storage.repositories.connectors import ConnectorRecord
from purin.events.storage.sqlite import SQLiteStore


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


def device_connector_tools(record: ConnectorRecord) -> list[str]:
    expected = record.config.get("tools")
    if isinstance(expected, list) and expected:
        return [tool for tool in expected if isinstance(tool, str) and tool]
    return [record.id]


def is_connector_connected(
    record: ConnectorRecord,
    capabilities: DeviceCapabilities | None,
    bound_devices: Mapping[str, str],
) -> tuple[bool, str]:
    if not record.enabled:
        return False, "disconnected"
    if record.tool_type == "device":
        device_id = record.device_id
        if not device_id or device_id not in bound_devices or capabilities is None:
            return False, "offline"
        advertised = capabilities.tools(device_id)
        if any(tool_name in advertised for tool_name in device_connector_tools(record)):
            return True, "connected"
        return False, "offline"
    if record.tool_type == "mcp":
        if record.auth_mode == "credential" and not record.has_credentials:
            return False, "pending_auth"
        if record.verified:
            return True, "connected"
        return False, "pending_auth" if record.auth_mode != "none" else "unverified"
    return record.verified, "connected" if record.verified else "disconnected"
