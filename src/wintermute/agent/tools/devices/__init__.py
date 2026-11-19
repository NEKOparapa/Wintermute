"""面向会话绑定设备的调用、通知和提问工具。"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from wintermute.agent import Tool
from wintermute.agent.tools.devices.ask_user import ask_user_tool
from wintermute.agent.tools.devices.list_user_devices import list_user_devices_tool
from wintermute.agent.tools.devices.notify_user import notify_user_tool

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
    return (
        list_user_devices_tool(
            store,
            store.get_conversation_user(conversation),
            devices.bindings,
            devices.capabilities,
        ),
        ask_user_tool(store, conversation, reply_device_id),
        notify_user_tool(
            store, conversation, devices.bindings, workspace, devices.resolve_history
        ),
    )
