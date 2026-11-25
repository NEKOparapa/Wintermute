"""Account device presence and notification routing for the current conversation."""

import json
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from purin.agent import Tool, ToolContext, ToolResult
from purin.config.models import HTTPDeviceBinding
from purin.events.integrations.devices import DeviceCapabilities
from purin.events.storage.sqlite import SQLiteStore


class DeviceStatusesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def device_statuses_tool(
    store: SQLiteStore,
    user: str,
    bindings: Callable[[], Sequence[HTTPDeviceBinding]],
    capabilities: DeviceCapabilities,
    notification_targets: Callable[[], frozenset[str]],
) -> Tool[DeviceStatusesRequest]:
    async def execute(_: DeviceStatusesRequest, context: ToolContext) -> ToolResult:
        names = {
            b.device_id: b.display_name or b.device_id for b in bindings() if b.user_id == user
        }
        targets = notification_targets()
        observed = store.devices.profiles(user)
        devices = []
        for device_id, default_name in sorted(names.items()):
            profile = observed.get(device_id)
            can_notify = device_id in targets
            if profile is None and not can_notify:
                continue
            devices.append(
                {
                    "device_id": device_id,
                    "name": (profile.name if profile else None) or default_name,
                    "platform": profile.platform if profile else None,
                    "status": capabilities.presence(device_id)[0],
                    "can_notify": can_notify,
                }
            )
        return ToolResult(json.dumps({"devices": devices}, ensure_ascii=False))

    return Tool(
        "device_statuses",
        "列出当前账号已连接过的设备及本会话可通知的设备，返回名称、device_id、平台和在线状态。"
        "can_notify 为 true 的设备可作为 device_messaging 的 notify 目标，即使当前离线。",
        DeviceStatusesRequest,
        execute,
    )
