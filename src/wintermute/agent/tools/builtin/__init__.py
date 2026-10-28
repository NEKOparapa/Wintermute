"""文件和命令基础工具，以及统一注册工厂。"""

from pathlib import Path
from typing import Any

from wintermute.agent.core.tools import Tool
from wintermute.agent.tools.builtin.command import ExecInput, create_exec_tool
from wintermute.agent.tools.builtin.files import (
    EditInput,
    ReadInput,
    TextEdit,
    WriteInput,
    create_edit_tool,
    create_read_tool,
    create_write_tool,
)

__all__ = [
    "EditInput",
    "ExecInput",
    "ReadInput",
    "TextEdit",
    "WriteInput",
    "create_builtin_tools",
    "create_edit_tool",
    "create_exec_tool",
    "create_read_tool",
    "create_write_tool",
]


def create_builtin_tools(
    cwd: str | Path,
    *,
    shell: str | None = None,
    command_timeout: float = 60,
    max_output_bytes: int = 50 * 1024,
    max_output_lines: int = 2000,
) -> tuple[Tool[Any], ...]:
    """以固定工作目录为基准创建 read/write/edit/exec 工具。

    将返回的元组传给 ``AgentRunner(model, tools=...)``；也可单独使用各工厂，
    只开放需要的能力。``cwd`` 是路径解析基准，并非沙箱；绝对路径和命令
    仍使用当前进程的权限。文件修改在当前事件循环内串行执行。
    命令作为前台任务运行，取消或超时会终止对应的 Windows 作业或 POSIX
    进程组；主动脱离进程组的 POSIX 后代进程可能继续运行。
    输出上限适用于读取内容、编辑差异和捕获的命令输出；简短的状态及截断
    提示不计入这些额度。
    """
    return (
        create_read_tool(cwd, max_lines=max_output_lines, max_bytes=max_output_bytes),
        create_write_tool(cwd),
        create_edit_tool(cwd, max_lines=max_output_lines, max_bytes=max_output_bytes),
        create_exec_tool(
            cwd,
            shell=shell,
            timeout=command_timeout,
            max_output_bytes=max_output_bytes,
            max_output_lines=max_output_lines,
        ),
    )
