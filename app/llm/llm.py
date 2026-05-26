from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI, OpenAIError


class LLMError(RuntimeError):
    """模型调用失败时抛出，HTTP 层会把它转换成 502。"""

    pass


@dataclass(frozen=True)
class ToolCall:
    """LLM 返回的一次工具调用，arguments 保留原始 JSON 字符串。"""

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
    """调用 OpenAI 兼容的 /chat/completions 接口。"""

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
        """发送一次 chat completions 请求，可选携带 tools 让模型自主调用工具。"""
        if not self.api_key or not self.model:
            raise LLMError(
                "LLM 未配置。请在 config/settings.json 中设置 api_key 和 model。"
            )

        final_messages = [{"role": "system", "content": system}, *messages]
        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )
        request: dict[str, Any] = {
            "model": self.model,
            "messages": final_messages,
        }
        if tools:
            request["tools"] = tools

        try:
            completion = client.chat.completions.create(**request)
        except OpenAIError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from exc

        try:
            message = completion.choices[0].message
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMError("LLM 响应不符合 chat completions 格式。") from exc

        content = (getattr(message, "content", None) or "").strip()
        tool_calls = _parse_tool_calls(getattr(message, "tool_calls", None))

        if not content and not tool_calls:
            raise LLMError("LLM 响应内容为空。")
        return LLMResponse(content=content, tool_calls=tool_calls)


def _parse_tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    """把 OpenAI SDK 返回的 tool_calls 列表转成内部 ToolCall 元组。"""
    if not raw:
        return ()
    parsed: list[ToolCall] = []
    for call in raw:
        if getattr(call, "type", "function") != "function":
            continue
        function = getattr(call, "function", None)
        if function is None:
            continue
        parsed.append(
            ToolCall(
                id=str(getattr(call, "id", "") or ""),
                name=str(getattr(function, "name", "") or ""),
                arguments=str(getattr(function, "arguments", "") or ""),
            )
        )
    return tuple(parsed)
