from __future__ import annotations

from ..config.config import Settings
from .base import Tool, ToolRegistry
from .files import ReadFileTool, WriteFileTool
from .terminal import TerminalTool

__all__ = [
    "Tool",
    "ToolRegistry",
    "TerminalTool",
    "ReadFileTool",
    "WriteFileTool",
    "build_tool_registry",
]


def build_tool_registry(settings: Settings) -> ToolRegistry | None:
    """根据配置组装默认工具注册表；总开关关闭时返回 None。"""
    if not settings.tools_enabled:
        return None

    registry = ToolRegistry()
    workdir = settings.terminal_workdir
    if settings.terminal_enabled:
        registry.register(
            TerminalTool(
                workdir=workdir,
                timeout_seconds=settings.terminal_timeout_seconds,
                denylist=settings.terminal_command_denylist,
            )
        )
    # 文件读写工具与 terminal 共享同一工作目录，避免越界。
    registry.register(ReadFileTool(workdir=workdir))
    registry.register(WriteFileTool(workdir=workdir))
    return registry if registry else None
