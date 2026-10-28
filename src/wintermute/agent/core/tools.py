"""支持 Pydantic 输入校验和异步执行协议的工具定义。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel

from wintermute.agent.core.types import ToolDefinition, ToolResult


@dataclass(frozen=True, slots=True)
class ToolContext:
    call_id: str
    turn: int


@dataclass(frozen=True, slots=True)
class Tool[Parameters: BaseModel]:
    name: str
    description: str
    parameters: type[Parameters]
    execute: Callable[[Parameters, ToolContext], Awaitable[ToolResult]]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Tool name must not be empty")

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(self.name, self.description, self.parameters.model_json_schema())
