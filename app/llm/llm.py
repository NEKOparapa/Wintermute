from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI, OpenAIError


class LLMError(RuntimeError):
    """模型调用失败时抛出，HTTP 层会把它转换成 502。"""

    pass


@dataclass(frozen=True)
class ToolCall:
    """LLM 返回的一次工具调用，arguments 保留原始 JSON 字符串。

    id 保存 Responses API 的 call_id，用于在后续轮次里把工具结果
    （function_call_output）与本次调用配对。
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMResponse:
    """LLM 一次回复的归一化结果。content 与 tool_calls 至少有其一不为空。"""

    content: str
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)


@dataclass
class OpenAICompatibleLLM:
    """调用 OpenAI 兼容的 /responses 接口（Responses API）。"""

    base_url: str
    api_key: str | None
    model: str | None
    timeout_seconds: int = 60

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """发送一次 Responses 请求，可选携带 tools 让模型自主调用工具。

        system 作为 instructions 传入，messages 是 Responses API 的 input 项列表
        （文本消息、多模态消息、function_call / function_call_output 等）。
        """
        if not self.api_key or not self.model:
            raise LLMError(
                "LLM 未配置。请在 config/settings.json 中设置 api_key 和 model。"
            )

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )
        request: dict[str, Any] = {
            "model": self.model,
            "input": messages,
        }
        if system:
            request["instructions"] = system
        if tools:
            request["tools"] = tools

        try:
            response = client.responses.create(**request)
        except OpenAIError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from exc

        content, tool_calls = _parse_response(response)
        if not content and not tool_calls:
            raise LLMError("LLM 响应内容为空。")
        return LLMResponse(content=content, tool_calls=tool_calls)


def _parse_response(response: Any) -> tuple[str, tuple[ToolCall, ...]]:
    """把 Responses 输出归一化成文本和工具调用。"""
    output = getattr(response, "output", None)
    if output is None:
        raise LLMError("LLM 响应不符合 Responses API 格式。")

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for item in output:
        item_type = getattr(item, "type", None)
        if item_type == "message":
            text_parts.extend(_message_text_parts(item))
        elif item_type == "function_call":
            tool_calls.append(
                ToolCall(
                    id=str(getattr(item, "call_id", "") or ""),
                    name=str(getattr(item, "name", "") or ""),
                    arguments=str(getattr(item, "arguments", "") or ""),
                )
            )

    content = "".join(text_parts).strip()
    if not content:
        # output_text 是 SDK 聚合所有文本的便捷属性，作为兜底来源。
        content = str(getattr(response, "output_text", "") or "").strip()
    return content, tuple(tool_calls)


def _message_text_parts(item: Any) -> list[str]:
    """从一条 message 输出项里抽取文本（含 refusal 文案）。"""
    parts: list[str] = []
    for part in getattr(item, "content", None) or []:
        part_type = getattr(part, "type", None)
        if part_type == "output_text":
            parts.append(str(getattr(part, "text", "") or ""))
        elif part_type == "refusal":
            parts.append(str(getattr(part, "refusal", "") or ""))
    return parts
