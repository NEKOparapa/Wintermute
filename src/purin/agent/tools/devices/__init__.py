"""账号设备查询和会话设备消息工具。"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from purin.agent import Tool
from purin.agent.tools.devices.messaging import device_messaging_tool
from purin.agent.tools.devices.statuses import device_statuses_tool

if TYPE_CHECKING:
    from pathlib import Path

    from purin.config.models import HTTPDeviceBinding
    from purin.events.core.attachments import HistoryAttachment
    from purin.events.integrations.devices import DeviceCapabilities
    from purin.events.storage.sqlite import SQLiteStore


@dataclass(frozen=True, slots=True)
class DeviceToolBindings:
    bindings: Sequence["HTTPDeviceBinding"] | Callable[[], Sequence["HTTPDeviceBinding"]]
    capabilities: "DeviceCapabilities"
    resolve_history: Callable[["SQLiteStore", str, str, str], "HistoryAttachment"]

    def current(self) -> Sequence["HTTPDeviceBinding"]:
        return self.bindings() if callable(self.bindings) else self.bindings


def create_user_device_tools(
    store: "SQLiteStore",
    conversation: str,
    devices: DeviceToolBindings,
    workspace: "Path",
    reply_device_id: Callable[[], str | None],
) -> tuple[Tool[Any], ...]:
    user = store.get_conversation_user(conversation)

    def notification_targets() -> frozenset[str]:
        return frozenset(
            binding.device_id
            for binding in devices.current()
            if binding.user_id == user and binding.conversation_id == conversation
        )

    return (
        device_statuses_tool(
            store, user, devices.current, devices.capabilities, notification_targets
        ),
        device_messaging_tool(
            store,
            user,
            conversation,
            notification_targets,
            workspace,
            devices.resolve_history,
            reply_device_id,
        ),
    )
