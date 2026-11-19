"""账号设备查询和会话设备消息工具。"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from wintermute.agent import Tool
from wintermute.agent.tools.devices.messaging import device_messaging_tool
from wintermute.agent.tools.devices.statuses import device_statuses_tool

if TYPE_CHECKING:
    from pathlib import Path

    from wintermute.config.models import HTTPDeviceSettings
    from wintermute.events.core.attachments import HistoryAttachment
    from wintermute.events.integrations.devices import DeviceCapabilities
    from wintermute.events.storage.sqlite import SQLiteStore


@dataclass(frozen=True, slots=True)
class DeviceToolBindings:
    bindings: Sequence["HTTPDeviceSettings"]
    capabilities: "DeviceCapabilities"
    resolve_history: Callable[["SQLiteStore", str, str, str], "HistoryAttachment"]


def create_user_device_tools(
    store: "SQLiteStore",
    conversation: str,
    devices: DeviceToolBindings,
    workspace: "Path",
    reply_device_id: Callable[[], str | None],
) -> tuple[Tool[Any], ...]:
    user = store.get_conversation_user(conversation)
    notification_targets = frozenset(
        b.device_id
        for b in devices.bindings
        if b.user_id == user and b.conversation_id == conversation
    )
    return (
        device_statuses_tool(
            store, user, devices.bindings, devices.capabilities, notification_targets
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
