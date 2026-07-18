from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .base import Tool

if TYPE_CHECKING:
    from ...subagents.manager import SubagentManager


class SubagentTaskTool(Tool):
    """Let the L0 main AI create and control asynchronous subagent tasks."""

    name = "subagent_task"
    description = (
        "异步子代理任务工具。适合把可独立描述、无需当前对话等待的工作委派到后台。"
        "通过 action=create/list/get/interrupt 创建、查看或中断任务。"
        "create 的 objective 必须自包含；工具会立即返回任务 ID。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "get", "interrupt"],
            },
            "objective": {
                "type": "string",
                "description": "create 必填；完整、自包含的任务目标。",
            },
            "id": {
                "type": "string",
                "description": "get/interrupt 必填；子代理任务 UUID。",
            },
            "status": {
                "type": "string",
                "enum": [
                    "queued",
                    "planning",
                    "running",
                    "interrupting",
                    "completed",
                    "interrupted",
                    "failed",
                ],
                "description": "list 的可选状态过滤。",
            },
            "limit": {
                "type": "integer",
                "description": "list 的可选最大返回条数。",
            },
            "reason": {
                "type": "string",
                "description": "interrupt 的可选原因。",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    def run(self, arguments: dict[str, Any]) -> str:
        return self.run_with_context(arguments, None)

    def run_with_context(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> str:
        action = str(arguments.get("action") or "").strip().lower()
        try:
            if action == "create":
                task = self.manager.create_task(
                    arguments.get("objective"),
                    origin=context,
                )
                return _json({"task": task})
            if action == "list":
                limit = _optional_int(arguments.get("limit"))
                tasks = self.manager.list_tasks(
                    status=arguments.get("status"),
                    limit=limit,
                )
                return _json({"count": len(tasks), "tasks": tasks})
            if action == "get":
                task = self.manager.get_task(arguments.get("id"))
                if task is None:
                    return _json({"error": "subagent_task_not_found", "id": arguments.get("id")})
                return _json({"task": task})
            if action == "interrupt":
                task = self.manager.interrupt_task(
                    arguments.get("id"),
                    reason=arguments.get("reason") or "requested_by_main_ai",
                )
                return _json({"task": task})
            return _json({"error": "unknown_action", "action": action})
        except Exception as exc:  # noqa: BLE001 - tool errors are stable JSON
            return _json({"error": str(exc), "action": action})


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    limit = int(value)
    if limit < 0:
        raise ValueError("limit 必须是非负整数。")
    return limit


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
