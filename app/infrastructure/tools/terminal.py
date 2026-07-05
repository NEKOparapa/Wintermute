from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from .base import Tool

logger = logging.getLogger(__name__)

DEFAULT_DENYLIST: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "shutdown",
    "reboot",
    "init 0",
    "init 6",
    ":(){:|:&};:",
)


class TerminalTool(Tool):
    """在受限工作目录里执行 shell 命令，返回 exit_code/stdout/stderr。"""

    name = "terminal"
    description = (
        "在受限的工作目录中执行单条 shell 命令（bash -c）。"
        "返回 JSON 字符串，含 exit_code、stdout、stderr。"
        "不要执行交互式或长驻命令。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令。",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        workdir: Path | str,
        timeout_seconds: int,
        denylist: tuple[str, ...] = DEFAULT_DENYLIST,
        max_output_chars: int = 4000,
    ) -> None:
        """绑定工作目录、超时、黑名单与单段输出上限，并确保 workdir 存在。"""
        self.workdir = Path(workdir)
        self.timeout_seconds = timeout_seconds
        self.denylist = tuple(denylist)
        self.max_output_chars = max_output_chars
        self.workdir.mkdir(parents=True, exist_ok=True)

    def run(self, arguments: dict[str, Any]) -> str:
        """执行命令；任何错误都包装成 JSON 结果而不是抛异常。"""
        command = str(arguments.get("command", "")).strip()
        if not command:
            return _format_result(-1, "", "command 不能为空。")

        if self._is_denied(command):
            logger.warning("terminal 命令被拒绝: %s", command)
            return _format_result(-1, "", f"命令被拒绝: {command}")

        logger.info("terminal 执行: %s", command)
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(self.workdir),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return _format_result(-1, "", f"命令超时（{self.timeout_seconds}s）。")
        except OSError as exc:
            return _format_result(-1, "", f"命令执行失败: {exc}")

        return _format_result(
            completed.returncode,
            _truncate(completed.stdout or "", self.max_output_chars),
            _truncate(completed.stderr or "", self.max_output_chars),
        )

    def _is_denied(self, command: str) -> bool:
        normalized = " ".join(command.split())
        return any(deny and deny in normalized for deny in self.denylist)


def _format_result(exit_code: int, stdout: str, stderr: str) -> str:
    return json.dumps(
        {"exit_code": exit_code, "stdout": stdout, "stderr": stderr},
        ensure_ascii=False,
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[已截断 {len(text) - limit} 字符]"
