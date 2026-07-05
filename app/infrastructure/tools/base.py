from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """供主 AI 调用的工具基类。子类提供 name/description/parameters 与 run。"""

    name: str
    description: str
    parameters: dict[str, Any]

    @abstractmethod
    def run(self, arguments: dict[str, Any]) -> str:
        """执行工具。返回值会作为 tool 消息的 content 回喂给 LLM。"""

    def to_responses_tool(self) -> dict[str, Any]:
        """转换为 OpenAI Responses API 的 function tool 入参元素（扁平结构）。

        strict=False 以兼容含可选参数的 schema，避免严格模式下要求所有字段必填。
        """
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": False,
        }


class ToolRegistry:
    """简单的名字到工具实例的映射表。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具，重名直接覆盖。"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """按名字查工具，找不到返回 None 由调用方处理。"""
        return self._tools.get(name)

    def to_responses_tools(self) -> list[dict[str, Any]]:
        """返回所有已注册工具的 Responses API tool schema 列表。"""
        return [tool.to_responses_tool() for tool in self._tools.values()]

    def __bool__(self) -> bool:
        return bool(self._tools)

    def __len__(self) -> int:
        return len(self._tools)
