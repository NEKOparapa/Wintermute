"""Build the connector management, discovery and invocation tools for a conversation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from purin.agent import Tool
from purin.config.models import HTTPDeviceBinding
from purin.events.integrations.connectors import ConnectorManager
from purin.events.integrations.devices import DeviceCapabilities
from purin.events.storage.sqlite import SQLiteStore

from .credentials import ConnectorCredentialSaveRequest, connector_credential_save_tool
from .device_calls import DeviceConnectorCalls
from .listing import ConnectorListRequest, RequestedGrants, connector_list_tool
from .mcp import McpRequest, mcp_tool
from .setup import ConnectorSetupRequest, ConnectorSourceInput, connector_setup_tool

__all__ = [
    "ConnectorCredentialSaveRequest",
    "ConnectorListRequest",
    "ConnectorSetupRequest",
    "ConnectorSourceInput",
    "DeviceConnectorCalls",
    "McpRequest",
    "RequestedGrants",
    "connector_credential_save_tool",
    "connector_list_tool",
    "connector_setup_tool",
    "create_connector_tools",
    "mcp_tool",
]


def create_connector_tools(
    store: SQLiteStore,
    conversation_id: str,
    bindings: Sequence[HTTPDeviceBinding] | Callable[[], Sequence[HTTPDeviceBinding]],
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
