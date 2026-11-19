"""Account device presence and notification routing for the current conversation."""

import json
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.config.models import HTTPDeviceSettings
from wintermute.events.integrations.devices import DeviceCapabilities
from wintermute.events.storage.sqlite import SQLiteStore


class DeviceStatusesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def device_statuses_tool(
    store: SQLiteStore,
    user: str,
    bindings: Sequence[HTTPDeviceSettings],
    capabilities: DeviceCapabilities,
    notification_targets: frozenset[str],
) -> Tool[DeviceStatusesRequest]:
    names = {b.device_id: b.display_name or b.device_id for b in bindings if b.user_id == user}

    async def execute(_: DeviceStatusesRequest, context: ToolContext) -> ToolResult:
        observed = store.devices.names(user)
        devices = [
            {
                "device_id": device_id,
                "name": observed.get(device_id) or names[device_id],
                "status": capabilities.presence(device_id)[0],
                "can_notify": device_id in notification_targets,
            }
            for device_id in sorted(names)
            if device_id in observed or device_id in notification_targets
        ]
        return ToolResult(json.dumps({"devices": devices}, ensure_ascii=False))

    return Tool(
        "device_statuses",
        "列出当前账号已连接过的设备及本会话可通知的设备，返回名称、device_id 和在线状态。"
        "can_notify 为 true 的设备可作为 device_messaging 的 notify 目标，即使当前离线。",
        DeviceStatusesRequest,
        execute,
    )
