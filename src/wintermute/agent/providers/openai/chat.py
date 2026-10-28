"""面向 OpenAI 兼容 Chat Completions 接口的流式适配器。"""

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from openai.types.chat.completion_create_params import CompletionCreateParamsStreaming

from wintermute.agent.core.errors import ModelProtocolError
from wintermute.agent.core.types import (
    AgentContext,
    AssistantMessage,
    ModelEvent,
    TextDelta,
    ToolCall,
    UserMessage,
)


@dataclass(slots=True)
class _ToolCallBuffer:
    id: str = ""
    name: str = ""
    arguments: str = ""


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _messages(context: AgentContext) -> list[ChatCompletionMessageParam]:
    messages: list[ChatCompletionMessageParam] = []
    if context.system_prompt:
        messages.append({"role": "system", "content": context.system_prompt})
    for message in context.messages:
        if isinstance(message, UserMessage):
            messages.append({"role": "user", "content": message.text})
        elif isinstance(message, AssistantMessage):
            if message.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.text or None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments, allow_nan=False),
                                },
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
            else:
                messages.append({"role": "assistant", "content": message.text})
        else:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )
    return messages


class OpenAIChatModel:
    """将调用方持有的 AsyncOpenAI 客户端适配为 Agent 的 Model 协议。

    调用方负责配置客户端的认证、服务地址、重试和网络超时，并在使用后关闭客户端。
    本适配器负责关闭每次请求的响应流，包括取消请求时的清理。
    ``extra_body`` 用于传入服务特有的请求选项。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        extra_body: Mapping[str, object] | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("Model name must not be empty")
        reserved = {"model", "messages", "tools", "stream", "n"}
        if extra_body and reserved.intersection(extra_body):
            raise ValueError("extra_body must not override model, messages, tools, stream or n")
        self._client = client
        self._model = model
        self._extra_body = dict(extra_body) if extra_body is not None else None

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        tools: list[ChatCompletionToolParam] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in context.tools
        ]
        request: CompletionCreateParamsStreaming = {
            "model": self._model,
            "messages": _messages(context),
            "stream": True,
        }
        if tools:
            request["tools"] = tools
        stream = await self._client.chat.completions.create(**request, extra_body=self._extra_body)
        text: list[str] = []
        calls: dict[int, _ToolCallBuffer] = {}
        finish: Literal["stop", "tool_calls", "length"] | None = None
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue  # 可选的数据块，可能仅包含用量统计。
                if len(chunk.choices) != 1 or chunk.choices[0].index != 0:
                    raise ModelProtocolError("Expected exactly one completion choice")
                if finish is not None:
                    raise ModelProtocolError("Received a completion choice after finish_reason")
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.function_call is not None:
                    raise ModelProtocolError("Legacy function_call responses are unsupported")
                for part in (delta.content, delta.refusal):
                    if part:
                        text.append(part)
                        yield TextDelta(part)
                for call in delta.tool_calls or ():
                    if call.index < 0 or call.type not in (None, "function"):
                        raise ModelProtocolError("Invalid tool call index or type")
                    buffer = calls.setdefault(call.index, _ToolCallBuffer())
                    if call.id:
                        if buffer.id and buffer.id != call.id:
                            raise ModelProtocolError("A tool call changed its ID during streaming")
                        buffer.id = call.id
                    if call.function:
                        buffer.name += call.function.name or ""
                        buffer.arguments += call.function.arguments or ""
                reason = choice.finish_reason
                if reason is not None:
                    if reason not in ("stop", "tool_calls", "length"):
                        raise ModelProtocolError(f"Unsupported finish_reason: {reason}")
                    finish = reason
        finally:
            await stream.close()

        if finish is None:
            raise ModelProtocolError("Completion stream ended without finish_reason")
        if calls and sorted(calls) != list(range(len(calls))):
            raise ModelProtocolError("Tool call indices must be contiguous from zero")
        parsed: list[ToolCall] = []
        ids: set[str] = set()
        for index in sorted(calls):
            buffer = calls[index]
            if not buffer.id or not buffer.name or buffer.id in ids:
                raise ModelProtocolError("Missing tool call ID/name or duplicate tool call ID")
            try:
                arguments: Any = json.loads(buffer.arguments, parse_constant=_reject_constant)
            except ValueError as error:
                raise ModelProtocolError("Tool arguments are not valid JSON") from error
            if not isinstance(arguments, dict):
                raise ModelProtocolError("Tool arguments must be a JSON object")
            ids.add(buffer.id)
            parsed.append(ToolCall(buffer.id, buffer.name, arguments))
        if (finish == "tool_calls" and not parsed) or (finish == "stop" and parsed):
            raise ModelProtocolError("finish_reason does not match the tool calls")
        yield AssistantMessage("".join(text), tuple(parsed), finish)
