"""Resolve account and device providers for ConnectorList and Mcp."""

from collections.abc import Collection
from dataclasses import dataclass

from wintermute.events.integrations.connectors import device_connector_tools
from wintermute.events.integrations.devices import DeviceCapabilities
from wintermute.events.integrations.mcp_client import McpToolInfo
from wintermute.events.storage.repositories.connectors import ConnectorRecord
from wintermute.events.storage.sqlite import SQLiteStore

from .device_arguments import device_tool_info


@dataclass(frozen=True, slots=True)
class BuiltinConnector:
    id: str
    name: str
    tool: McpToolInfo
    device_id: str | None = None


# Colons distinguish internal providers from user-configured connector IDs.
RECEIPTS = BuiltinConnector(
    "wintermute:receipts",
    "设备调用回执",
    McpToolInfo(
        "get_call_result",
        "按调用 ID 查询本会话中的设备调用状态和执行结果",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    ),
)


def native_connector(
    device_id: str,
    capabilities: DeviceCapabilities | None,
) -> BuiltinConnector | None:
    # Mobile and ESP32 advertise this opt-in tool in their claim, without a catalog sync.
    if capabilities and "device_status" in capabilities.tools(device_id):
        return BuiltinConnector(
            "wintermute:device", "设备运行状态", device_tool_info("device_status"), device_id
        )
    return None


type Provider = ConnectorRecord | BuiltinConnector


def provider_key(record: Provider) -> str:
    return f"{record.id}@{record.device_id}" if record.device_id is not None else record.id


def device_providers(
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
