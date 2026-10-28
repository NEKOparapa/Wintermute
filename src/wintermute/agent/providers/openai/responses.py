"""异步流式 Responses 适配器，每轮显式回传历史，不依赖服务端会话状态。"""

import json
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.responses import ResponseInputParam
from openai.types.responses.response_create_params import ResponseCreateParamsStreaming

from wintermute.agent import (
    AgentContext,
    AssistantMessage,
    ModelEvent,
    ModelProtocolError,
    TextDelta,
    ToolCall,
    UserMessage,
)

_OUTPUT_KEY = "openai.responses.output"


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _parse_output(output: object) -> tuple[str, tuple[ToolCall, ...]]:
    """执行任何函数调用前，先校验完整输出。"""
    if not isinstance(output, list):
        raise ModelProtocolError("Responses output must be a list of items")
    text: list[str] = []
    calls: list[ToolCall] = []
    call_ids: set[str] = set()
    item_ids: set[str] = set()
    for item in output:
        if not isinstance(item, dict):
            raise ModelProtocolError("Invalid Responses output item")
        item_id = item.get("id")
        if item_id is not None:
            if not isinstance(item_id, str) or not item_id or item_id in item_ids:
                raise ModelProtocolError("Responses output item IDs must be unique and nonempty")
            item_ids.add(item_id)
        status = item.get("status")
        if status is not None and status != "completed":
            raise ModelProtocolError(f"Responses output item is not complete: {status}")
        kind = item.get("type")
        if kind == "reasoning":
            # 原样保留推理数据用于回传，不将其作为回答正文输出。
            continue
        if kind == "message":
            if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                raise ModelProtocolError("Expected an assistant Responses output message")
            for part in item["content"]:
                if not isinstance(part, dict):
                    raise ModelProtocolError("Invalid Responses message content")
                if part.get("type") == "output_text":
                    value = part.get("text")
                elif part.get("type") == "refusal":
                    value = part.get("refusal")
                else:
                    raise ModelProtocolError(f"Unsupported Responses content: {part.get('type')}")
                if not isinstance(value, str):
                    raise ModelProtocolError("Responses text/refusal must be a string")
                text.append(value)
        elif kind == "function_call":
            call_id, name = item.get("call_id"), item.get("name")
            if (
                not isinstance(call_id, str)
                or not call_id.strip()
                or call_id in call_ids
                or not isinstance(name, str)
                or not name.strip()
            ):
                raise ModelProtocolError("Function calls require unique call_id values and names")
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                raise ModelProtocolError("Function arguments must be a JSON string")
            try:
                parsed: Any = json.loads(arguments, parse_constant=_reject_constant)
            except ValueError as exc:
                raise ModelProtocolError("Function arguments are not valid JSON") from exc
            if not isinstance(parsed, dict):
                raise ModelProtocolError("Function arguments must be a JSON object")
            call_ids.add(call_id)
            calls.append(ToolCall(call_id, name, parsed))
        else:
            raise ModelProtocolError(f"Unsupported Responses output item: {kind}")
    return "".join(text), tuple(calls)


def _input(context: AgentContext) -> ResponseInputParam:
    items: list[dict[str, Any]] = []
    for message in context.messages:
        if isinstance(message, UserMessage):
            items.append({"type": "message", "role": "user", "content": message.text})
        elif isinstance(message, AssistantMessage):
            if _OUTPUT_KEY in message.provider_data:
                output = message.provider_data[_OUTPUT_KEY]
                text, calls = _parse_output(output)
                if text != message.text or calls != message.tool_calls:
                    raise ModelProtocolError(
                        "Responses provider_data does not match the assistant message. "
                        "Clear provider_data when rewriting its text or tool calls."
                    )
                items.extend(deepcopy(output))
                continue
            if message.text or not message.tool_calls:
                items.append({"type": "message", "role": "assistant", "content": message.text})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, ensure_ascii=False, allow_nan=False
                        ),
                    }
                )
        else:
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
    # SDK 使用包含多种 TypedDict 的联合类型；经过校验的原始输出项
    # 仍需保留供应商特有的字段，这些字段不由核心消息协议解析。
    return cast(ResponseInputParam, items)


class OpenAIResponsesModel:
    """通过 /responses 将调用方持有的 AsyncOpenAI 客户端适配为 Model。

    每次请求均设置 store=False，并发送传入的历史上下文。
    适配器不缓存响应 ID 或会话状态，使上下文变换和多个 Runner 的使用相互独立。
    完整的原始输出项保存在 AssistantMessage.provider_data 中，包括加密的推理项。
    调用方负责认证、超时、重试及客户端生命周期管理。
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
        reserved = {
            "model",
            "input",
            "instructions",
            "tools",
            "stream",
            "store",
            "include",
            "previous_response_id",
            "conversation",
            "background",
        }
        if extra_body and reserved.intersection(extra_body):
            names = ", ".join(sorted(reserved.intersection(extra_body)))
            raise ValueError(f"extra_body must not override Responses protocol fields: {names}")
        self._client = client
        self._model = model
        self._extra_body = deepcopy(dict(extra_body)) if extra_body is not None else None

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        request: ResponseCreateParamsStreaming = {
            "model": self._model,
            "input": _input(context),
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if context.system_prompt:
            request["instructions"] = context.system_prompt
        if context.tools:
            request["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    # 避免 Responses 将工具的可选参数规范化为必填字段；
                    # 参数仍由 Runner 严格校验。
                    "strict": False,
                }
                for tool in context.tools
            ]
        stream = await self._client.responses.create(**request, extra_body=self._extra_body)
        final: AssistantMessage | None = None
        reasoning: dict[str, str] = {}
        try:
            async for event in stream:
                if final is not None:
                    raise ModelProtocolError("Responses emitted events after response.completed")
                if (
                    event.type == "response.output_text.delta"
                    or event.type == "response.refusal.delta"
                ):
                    delta = event.delta
                    if not isinstance(delta, str):
                        raise ModelProtocolError("Responses text delta must be a string")
                    yield TextDelta(delta)
                elif (
                    event.type == "response.output_item.added"
                    or event.type == "response.output_item.done"
                ):
                    if event.item.type == "reasoning":
                        item = event.item.model_dump(mode="json", exclude_none=True)
                        item_id, encrypted = item.get("id"), item.get("encrypted_content")
                        if isinstance(item_id, str) and isinstance(encrypted, str) and encrypted:
                            reasoning[item_id] = encrypted
                elif event.type == "response.completed":
                    if event.response.status != "completed":
                        raise ModelProtocolError("response.completed has a non-completed status")
                    output = [
                        item.model_dump(mode="json", exclude_none=True)
                        for item in event.response.output
                    ]
                    # 部分兼容服务仅在 output_item.added/done 中提供 encrypted_content。
                    # 即使后续事件或最终输出项省略该字段，也要保留此前收到的非空值。
                    for item in output:
                        if item.get("type") == "reasoning" and not item.get("encrypted_content"):
                            encrypted = reasoning.get(item.get("id", ""))
                            if encrypted:
                                item["encrypted_content"] = encrypted
                    text, calls = _parse_output(output)
                    final = AssistantMessage(
                        text=text,
                        tool_calls=calls,
                        finish_reason="tool_calls" if calls else "stop",
                        provider_data={_OUTPUT_KEY: output},
                    )
                elif event.type == "response.failed":
                    error = event.response.error
                    detail = error.message if error is not None else "unknown error"
                    raise ModelProtocolError(f"Responses request failed: {detail}")
                elif event.type == "response.incomplete":
                    details = event.response.incomplete_details
                    reason = details.reason if details is not None else "unknown reason"
                    raise ModelProtocolError(f"Responses request incomplete: {reason}")
                elif event.type == "error":
                    raise ModelProtocolError(f"Responses stream error: {event.message}")
                # 生命周期、推理摘要和参数增量/完成事件不代表可执行的最终结果。
                # 以 completed.output 中的完整输出为准。
        finally:
            await stream.close()
        if final is None:
            raise ModelProtocolError("Responses stream ended without response.completed")
        yield final
