from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

from ..config.config import Settings
from ..infrastructure.llm.llm import OpenAICompatibleLLM
from ..infrastructure.storage.subagent_task_store import (
    TERMINAL_TASK_STATUSES,
    SubagentTaskStore,
)
from ..infrastructure.tools.base import ToolRegistry
from ..infrastructure.tools.runner import run_registered_tool

logger = logging.getLogger(__name__)


_PLANNING_PROMPT = """你是 Wintermute 的子代理规划器。

你的唯一职责是把当前任务拆成一个可顺序执行的固定计划，并调用 submit_subagent_plan。
- 计划必须包含 1 到 20 个清晰、可验证的步骤。
- 步骤应覆盖完成目标所需的实际工作，不要加入寒暄、等待用户确认或创建其它代理。
- 当前阶段不能执行文件、终端或日程工具。
"""

_EXECUTION_PROMPT = """你是 Wintermute 的子代理执行器。

只执行系统提供的当前计划步骤。你可以使用提供的工具完成实际工作，但不能创建其它代理，
也不能修改既定计划。需要工具时先调用工具；确认当前步骤已经完成后，输出一段简洁的步骤结果。
不要声称执行了尚未通过工具完成的操作，也不要提前执行后续步骤。
"""

_PLAN_TOOL = {
    "type": "function",
    "name": "submit_subagent_plan",
    "description": "提交运行前的固定任务计划。",
    "parameters": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {"type": "string"},
                "description": "按执行顺序排列的 1 到 20 个非空步骤。",
            }
        },
        "required": ["steps"],
        "additionalProperties": False,
    },
    "strict": False,
}


class SubagentRunCancelled(RuntimeError):
    """Raised internally when a cooperative interruption is observed."""


class SubagentProtocolError(RuntimeError):
    """Raised when the planning or execution response violates the protocol."""


@dataclass
class SubagentService:
    """Run one persisted subagent task through planning and ordered execution."""

    store: SubagentTaskStore
    settings: Settings
    tool_registry: ToolRegistry | None = None
    llm: OpenAICompatibleLLM | None = None

    def __post_init__(self) -> None:
        if self.llm is None:
            self.llm = OpenAICompatibleLLM(
                base_url=self.settings.base_url,
                api_key=self.settings.api_key,
                model=self.settings.model,
            )

    def run(self, task_id: str, cancel_event: threading.Event) -> dict[str, Any]:
        """Plan and execute a task, always reconciling it to a terminal state."""
        try:
            self._check_cancelled(task_id, cancel_event)
            task = self.store.transition_task(task_id, "planning")
            steps = self._create_plan(task, cancel_event)
            self._check_cancelled(task_id, cancel_event)
            task = self.store.set_plan(task_id, steps)
            task = self.store.transition_task(task_id, "running")

            for step in task["plan"]:
                self._check_cancelled(task_id, cancel_event)
                task = self.store.transition_step(
                    task_id,
                    str(step["id"]),
                    "in_progress",
                )
                result = self._execute_step(task, str(step["id"]), cancel_event)
                self._check_cancelled(task_id, cancel_event)
                task = self.store.transition_step(
                    task_id,
                    str(step["id"]),
                    "completed",
                    result=result,
                )

            return self.store.transition_task(
                task_id,
                "completed",
                result=_aggregate_result(task),
            )
        except SubagentRunCancelled:
            return self._finalize_interrupted(task_id)
        except Exception as exc:  # noqa: BLE001 - background task must reach a terminal state
            if cancel_event.is_set() or self._is_interrupting(task_id):
                return self._finalize_interrupted(task_id)
            logger.exception("子代理任务执行失败 task_id=%s", task_id)
            return self._finalize_failed(task_id, str(exc))

    def _create_plan(
        self,
        task: dict[str, Any],
        cancel_event: threading.Event,
    ) -> list[str]:
        self._check_cancelled(str(task["id"]), cancel_event)
        assert self.llm is not None
        response = self.llm.complete(
            system=_PLANNING_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"任务目标：\n{task['objective']}\n\n请提交固定执行计划。",
                }
            ],
            tools=[_PLAN_TOOL],
            tool_choice={"type": "function", "name": "submit_subagent_plan"},
            parallel_tool_calls=False,
        )
        self._check_cancelled(str(task["id"]), cancel_event)
        if len(response.tool_calls) != 1:
            raise SubagentProtocolError("规划阶段必须且只能提交一次计划。")
        call = response.tool_calls[0]
        if call.name != "submit_subagent_plan":
            raise SubagentProtocolError(f"规划阶段调用了未知工具: {call.name}")
        try:
            arguments = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise SubagentProtocolError("计划参数不是有效 JSON。") from exc
        if not isinstance(arguments, dict):
            raise SubagentProtocolError("计划参数必须是对象。")
        raw_steps = arguments.get("steps")
        if not isinstance(raw_steps, list):
            raise SubagentProtocolError("计划 steps 必须是数组。")
        if any(not isinstance(item, str) for item in raw_steps):
            raise SubagentProtocolError("计划中的每个步骤都必须是字符串。")
        steps = [item.strip() for item in raw_steps]
        if not 1 <= len(steps) <= 20 or any(not item for item in steps):
            raise SubagentProtocolError("计划必须包含 1 到 20 个非空步骤。")
        return steps

    def _execute_step(
        self,
        task: dict[str, Any],
        step_id: str,
        cancel_event: threading.Event,
    ) -> str:
        step = _step_by_id(task, step_id)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": _step_input(task, step),
            }
        ]
        tools_schema = (
            self.tool_registry.to_responses_tools()
            if self.tool_registry is not None and len(self.tool_registry) > 0
            else None
        )
        max_iterations = max(1, self.settings.max_tool_iterations)
        assert self.llm is not None

        for iteration in range(max_iterations + 1):
            self._check_cancelled(str(task["id"]), cancel_event)
            response = self.llm.complete(
                system=_EXECUTION_PROMPT,
                messages=messages,
                tools=tools_schema,
            )
            self._check_cancelled(str(task["id"]), cancel_event)
            if not response.tool_calls:
                result = response.content.strip()
                if not result:
                    raise SubagentProtocolError(f"步骤 {step_id} 没有返回执行结果。")
                return result
            if iteration >= max_iterations:
                raise SubagentProtocolError(
                    f"步骤 {step_id} 的工具调用次数已达上限 {max_iterations}。"
                )

            for call in response.tool_calls:
                self._check_cancelled(str(task["id"]), cancel_event)
                messages.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
                result_text = run_registered_tool(self.tool_registry, call)
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.id,
                        "output": result_text,
                    }
                )
                self._check_cancelled(str(task["id"]), cancel_event)

        raise SubagentProtocolError(f"步骤 {step_id} 未能结束。")

    def _check_cancelled(
        self,
        task_id: str,
        cancel_event: threading.Event,
    ) -> None:
        if cancel_event.is_set() or self._is_interrupting(task_id):
            raise SubagentRunCancelled()

    def _is_interrupting(self, task_id: str) -> bool:
        task = self.store.get_task(task_id)
        return bool(task and task.get("status") == "interrupting")

    def _finalize_interrupted(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"子代理任务不存在: {task_id}")
        if task["status"] in TERMINAL_TASK_STATUSES:
            return task
        if task["status"] != "interrupting":
            task = self.store.request_interrupt(
                task_id,
                reason="subagent_cancelled",
            )
        current_step_id = task.get("current_step_id")
        if current_step_id:
            task = self.store.transition_step(
                task_id,
                str(current_step_id),
                "interrupted",
            )
        return self.store.transition_task(task_id, "interrupted")

    def _finalize_failed(self, task_id: str, error: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"子代理任务不存在: {task_id}")
        if task["status"] in TERMINAL_TASK_STATUSES:
            return task
        if task["status"] == "interrupting":
            return self._finalize_interrupted(task_id)
        current_step_id = task.get("current_step_id")
        try:
            if current_step_id and task["status"] == "running":
                task = self.store.transition_step(
                    task_id,
                    str(current_step_id),
                    "failed",
                    error=error,
                )
            return self.store.transition_task(task_id, "failed", error=error)
        except ValueError:
            # An interrupt may win the store lock after the read above.
            latest = self.store.get_task(task_id)
            if latest is not None and latest.get("status") == "interrupting":
                return self._finalize_interrupted(task_id)
            raise


def _step_by_id(task: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in task.get("plan", []):
        if str(step.get("id")) == step_id:
            return step
    raise ValueError(f"计划步骤不存在: {step_id}")


def _step_input(task: dict[str, Any], current_step: dict[str, Any]) -> str:
    plan_lines = []
    for step in task.get("plan", []):
        status = str(step.get("status") or "pending")
        result = str(step.get("result") or "").strip()
        result_part = f"；已有结果：{result}" if result else ""
        plan_lines.append(
            f"- [{status}] {step.get('id')} {step.get('title', '')}{result_part}"
        )
    description = str(current_step.get("description") or "").strip()
    description_part = f"\n步骤说明：{description}" if description else ""
    return (
        f"任务目标：\n{task['objective']}\n\n"
        f"固定计划：\n{'\n'.join(plan_lines)}\n\n"
        f"当前只执行：{current_step.get('id')} {current_step.get('title', '')}"
        f"{description_part}"
    )


def _aggregate_result(task: dict[str, Any]) -> str:
    lines = []
    for step in task.get("plan", []):
        result = str(step.get("result") or "").strip()
        if result:
            lines.append(f"{step.get('index')}. {step.get('title')}: {result}")
    return "\n".join(lines) or "任务计划已全部完成。"
