from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
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


@dataclass(frozen=True)
class UploadedFile:
    """Files API 上传完成后的文件引用。"""

    id: str
    status: str


@dataclass
class OpenAICompatibleLLM:
    """调用 OpenAI 兼容的 /responses 接口（Responses API）。"""

    base_url: str
    api_key: str | None
    model: str | None
    timeout_seconds: int = 60

    def _client(self) -> OpenAI:
        if not self.api_key:
            raise LLMError("LLM 未配置。请在 config/settings.json 中设置 api_key。")
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )

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

        client = self._client()
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

    def upload_file(
        self,
        file_path: str | Path,
        *,
        purpose: str = "user_data",
        preprocess_configs: dict[str, Any] | None = None,
        poll_interval_seconds: float = 2.0,
        wait_timeout_seconds: float = 600.0,
    ) -> UploadedFile:
        """上传本地文件到兼容服务 Files API，并等待 processing 结束。"""
        path = Path(file_path).expanduser()
        client = self._client()
        try:
            with path.open("rb") as file:
                kwargs: dict[str, Any] = {"file": file, "purpose": purpose}
                if preprocess_configs:
                    kwargs["extra_body"] = _flatten_multipart_fields(
                        "preprocess_configs",
                        preprocess_configs,
                    )
                uploaded = client.files.create(**kwargs)
        except OSError as exc:
            raise LLMError(f"文件读取失败: {path}") from exc
        except OpenAIError as exc:
            raise LLMError(f"文件上传失败: {exc}") from exc

        file_id = _file_attr(uploaded, "id")
        if not file_id:
            raise LLMError("文件上传响应缺少 file id。")

        deadline = time.monotonic() + wait_timeout_seconds
        current = uploaded
        while _file_attr(current, "status") == "processing":
            if time.monotonic() >= deadline:
                raise LLMError(f"文件处理超时: {file_id}")
            time.sleep(max(0.1, poll_interval_seconds))
            try:
                current = client.files.retrieve(file_id)
            except OpenAIError as exc:
                raise LLMError(f"文件状态查询失败: {exc}") from exc

        status = _file_attr(current, "status")
        if status in {"error", "failed", "cancelled"}:
            raise LLMError(f"文件处理失败: {file_id} status={status}")
        return UploadedFile(id=file_id, status=status)


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


def _file_attr(file_obj: Any, name: str) -> str:
    return str(getattr(file_obj, name, "") or "").strip()


def _flatten_multipart_fields(prefix: str, value: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, child in value.items():
        field_name = f"{prefix}[{key}]"
        if isinstance(child, dict):
            fields.update(_flatten_multipart_fields(field_name, child))
        else:
            fields[field_name] = child
    return fields


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
