from __future__ import annotations

from ..config.config import Settings
from ..storage.schedule_store import ScheduleStore
from .base import Tool, ToolRegistry
from .files import ReadFileTool, WriteFileTool
from .runner import run_registered_tool
from .schedule import ScheduleTool
from .terminal import TerminalTool

__all__ = [
    "Tool",
    "ToolRegistry",
    "TerminalTool",
    "ReadFileTool",
    "WriteFileTool",
    "ScheduleTool",
    "build_l0_tool_registry",
    "build_l1_tool_registry",
    "run_registered_tool",
]


def build_l0_tool_registry(settings: Settings) -> ToolRegistry | None:
    """根据配置组装 L0 对话工具注册表；总开关关闭时返回 None。"""
    if not settings.tools_enabled:
        return None

    registry = ToolRegistry()
    workdir = settings.terminal_workdir
    schedule_store = ScheduleStore(settings.data_dir)
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
    registry.register(
        ScheduleTool(
            schedule_store,
            allowed_actions={"create", "list", "get", "update", "delete"},
        )
    )
    return registry if registry else None


def build_l1_tool_registry(settings: Settings) -> ToolRegistry | None:
    """根据配置组装 L1 主动流程工具注册表；总开关关闭时返回 None。"""
    if not settings.tools_enabled:
        return None

    registry = ToolRegistry()
    # L1 主动流程默认只读，避免主动任务产生写入或命令执行副作用。
    registry.register(ReadFileTool(workdir=settings.terminal_workdir))
    schedule_store = ScheduleStore(settings.data_dir)
    registry.register(ScheduleTool(schedule_store, allowed_actions={"list", "get"}))
    return registry if registry else None
