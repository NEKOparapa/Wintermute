"""Dispatch calls to connectors advertised by a device in this conversation."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from wintermute.agent import ToolContext
from wintermute.agent.tools.device_arguments import device_tool_info
from wintermute.config.models import HTTPDeviceSettings
from wintermute.events.integrations.devices import DeviceCapabilities
from wintermute.events.integrations.mcp_client import McpToolInfo
from wintermute.events.storage.sqlite import SQLiteStore


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


class DeviceConnectorCalls:
    """Remote queue scoped to the devices bound to one user's conversation."""

    def __init__(
        self,
        store: SQLiteStore,
        conversation: str,
        bindings: Sequence[HTTPDeviceSettings],
        capabilities: DeviceCapabilities,
        reply_target: Callable[[], str | None],
    ) -> None:
        user = store.get_conversation_user(conversation)
        self.queue = store.remote_tools
        self.scope = {"user_id": user, "conversation_id": conversation}
        self.targets = {
            b.device_id
            for b in bindings
            if b.user_id == user and b.conversation_id == conversation
        }
        self.capabilities = capabilities
        self.reply_target = reply_target

    def submit(
        self,
        device_id: str,
        tool: str,
        args: dict[str, JsonValue],
        context: ToolContext,
    ) -> dict[str, str]:
        if device_id not in self.targets:
            raise ValueError("目标设备不属于当前会话")
        if not context.run_id:
            raise ValueError("远程调用需要主 Agent 的运行 ID")
        call = self.queue.create(
            **self.scope,
            device_id=device_id,
            tool=tool,
            arguments=args,
            timeout_seconds=30,
            run_id=context.run_id,
            call_id=context.call_id,
            reply_device_id=self.reply_target(),
            available=tool in self.capabilities.tools(device_id),
        )
        return {"id": call.id, "device_id": device_id, "tool": tool, "status": call.status}
