from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI, OpenAIError


class LLMError(RuntimeError):
    """模型调用失败时抛出，HTTP 层会把它转换成 502。"""

    pass


@dataclass
class OpenAICompatibleLLM:
    """调用 OpenAI 兼容的 /chat/completions 接口。"""

    base_url: str
    api_key: str | None
    model: str | None
    timeout_seconds: int = 60

    def complete(self, *, system: str, messages: list[dict[str, str]]) -> str:
        """组装系统提示词和历史消息，发送 Chat Completions 请求。"""
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
        try:
            completion = client.chat.completions.create(
                model=self.model,
                messages=final_messages,
            )
        except OpenAIError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from exc

        try:
            content = completion.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMError("LLM 响应不符合 chat completions 格式。") from exc
        if content is None:
            raise LLMError("LLM 响应内容为空。")
        return str(content).strip()
