"""List devices that authenticated to the current account."""

import json
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.config.models import HTTPDeviceSettings
from wintermute.events.integrations.devices import DeviceCapabilities
from wintermute.events.storage.sqlite import SQLiteStore


class ListUserDevices(BaseModel):
    model_config = ConfigDict(extra="forbid")


def list_user_devices_tool(
    store: SQLiteStore,
    user: str,
    bindings: Sequence[HTTPDeviceSettings],
    capabilities: DeviceCapabilities,
) -> Tool[ListUserDevices]:
    names = {b.device_id: b.display_name or b.device_id for b in bindings if b.user_id == user}

    async def execute(_: ListUserDevices, context: ToolContext) -> ToolResult:
        devices = [
            {
                "device_id": device_id,
                "name": custom or names[device_id],
                "status": capabilities.presence(device_id)[0],
            }
            for device_id, custom in store.devices.names(user).items()
            if device_id in names
        ]
        return ToolResult(json.dumps({"devices": devices}, ensure_ascii=False))

    return Tool(
        "list_user_devices",
        "列出当前账号已连接过的设备。返回名称、设备 ID 和在线状态。",
        ListUserDevices,
        execute,
    )
