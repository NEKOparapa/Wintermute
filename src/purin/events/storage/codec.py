"""事件指纹与完整会话消息的持久化编码。"""

import hashlib
import json
from dataclasses import asdict

from purin.agent.core.types import (
    AssistantMessage,
    ImageMessage,
    Message,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from purin.events.core.types import (
    EventError,
    EventInput,
    Layer,
)

EVENT_TABLES: dict[Layer, str] = {
    "conversation": "conversation_events",
    "notification": "notification_events",
    "perception": "perception_events",
    "slice": "slice_events",
}


def encode_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def event_fingerprint(event: EventInput) -> str:
    fields = event.model_dump(mode="json", exclude={"occurred_at"})
    return hashlib.sha256(encode_json(fields).encode("utf-8")).hexdigest()


def encode_messages(messages: tuple[Message, ...]) -> str:
    """保留工具调用与结果的完整结构，避免恢复时只剩文本。"""
    pending: dict[str, str] = {}
    for message in messages:
        if isinstance(message, ToolResultMessage):
            if pending.get(message.tool_call_id) != message.name:
                raise EventError("Transcript contains an unmatched tool result")
            del pending[message.tool_call_id]
        else:
            if pending:
                raise EventError("Transcript contains tool calls without results")
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls:
                    if not call.id or not call.name or call.id in pending:
                        raise EventError("Transcript contains invalid tool calls")
                    pending[call.id] = call.name
            elif not isinstance(message, UserMessage):
                raise EventError("Transcript contains an unsupported message")
    if pending:
        raise EventError("Transcript ends with unfinished tool calls")
    return encode_json([asdict(message) for message in messages])


def decode_messages(value: str) -> tuple[Message, ...]:
    result: list[Message] = []
    for item in json.loads(value):
        role = item.pop("role")
        if role == "user":
            if "images" in item:
                item["images"] = tuple(item["images"])
                result.append(ImageMessage(**item))
            else:
                result.append(UserMessage(**item))
        elif role == "assistant":
            item["tool_calls"] = tuple(ToolCall(**call) for call in item["tool_calls"])
            result.append(AssistantMessage(**item))
        elif role == "tool":
            result.append(ToolResultMessage(**item))
        else:
            raise EventError("Stored transcript contains an unsupported role")
    return tuple(result)
