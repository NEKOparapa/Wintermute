from __future__ import annotations

from ..config.config import Settings
from .base import Tool, ToolRegistry
from .terminal import TerminalTool

__all__ = ["Tool", "ToolRegistry", "TerminalTool", "build_tool_registry"]


def build_tool_registry(settings: Settings) -> ToolRegistry | None:
    """根据配置组装默认工具注册表；总开关关闭时返回 None。"""
    if not settings.tools_enabled:
        return None

    registry = ToolRegistry()
    if settings.terminal_enabled:
        registry.register(
            TerminalTool(
                workdir=settings.terminal_workdir,
                timeout_seconds=settings.terminal_timeout_seconds,
                denylist=settings.terminal_command_denylist,
            )
        )
    return registry if registry else None
