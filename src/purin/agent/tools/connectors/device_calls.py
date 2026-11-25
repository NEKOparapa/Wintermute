"""Dispatch calls to connectors advertised by a device in this conversation."""

from collections.abc import Callable, Sequence

from pydantic import JsonValue

from purin.agent import ToolContext
from purin.config.models import HTTPDeviceBinding
from purin.events.integrations.devices import DeviceCapabilities
from purin.events.storage.sqlite import SQLiteStore


class DeviceConnectorCalls:
    """Remote queue scoped to the devices bound to one user's conversation."""

    def __init__(
        self,
        store: SQLiteStore,
        conversation: str,
        bindings: Callable[[], Sequence[HTTPDeviceBinding]],
        capabilities: DeviceCapabilities,
        reply_target: Callable[[], str | None],
    ) -> None:
        user = store.get_conversation_user(conversation)
        self.queue = store.remote_tools
        self.scope = {"user_id": user, "conversation_id": conversation}
        self.bindings = bindings
        self.capabilities = capabilities
        self.reply_target = reply_target

    @property
    def targets(self) -> frozenset[str]:
        return frozenset(
            binding.device_id
            for binding in self.bindings()
            if binding.user_id == self.scope["user_id"]
            and binding.conversation_id == self.scope["conversation_id"]
        )

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
            timeout_seconds=120 if tool == "mac_speak" else 30,
            run_id=context.run_id,
            call_id=context.call_id,
            reply_device_id=self.reply_target(),
            available=tool in self.capabilities.tools(device_id),
        )
        return {"id": call.id, "device_id": device_id, "tool": tool, "status": call.status}
