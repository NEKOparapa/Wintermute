from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any

from ..storage.subagent_task_store import SubagentTaskStore

logger = logging.getLogger(__name__)

_TASK_CONTEXT_HEADER = (
    "以下是子代理任务的当前状态。活跃任务进度来自权威任务存储；"
    "最近终态任务仅提供结果摘要："
)
_TERMINAL_TASK_LIMIT = 10
_COMPLETED_STEP_STATUSES = frozenset({"completed", "skipped"})


def load_subagent_task_snapshot(
    task_store: SubagentTaskStore,
) -> dict[str, list[dict[str, Any]]]:
    """读取用于 prompt 的任务快照；存储异常不应阻断主流程。"""
    try:
        snapshot = task_store.context_snapshot(
            recent_terminal_limit=_TERMINAL_TASK_LIMIT
        )
    except Exception:
        logger.exception("子代理任务上下文读取失败")
        return _empty_snapshot()
    if not isinstance(snapshot, Mapping):
        logger.warning("子代理任务上下文不是对象，已忽略")
        return _empty_snapshot()
    return {
        "active_tasks": _task_list(snapshot.get("active_tasks")),
        "recent_terminal_tasks": _task_list(
            snapshot.get("recent_terminal_tasks")
        )[:_TERMINAL_TASK_LIMIT],
    }


def subagent_task_context_block(snapshot: object) -> str:
    """把任务快照转成紧凑上下文，不包含 metadata 或工具日志。"""
    if not isinstance(snapshot, Mapping):
        return ""
    active_tasks = _task_list(snapshot.get("active_tasks"))
    terminal_tasks = _task_list(snapshot.get("recent_terminal_tasks"))[
        :_TERMINAL_TASK_LIMIT
    ]
    omitted_active_count = _nonnegative_int(snapshot.get("omitted_active_count"))
    if not active_tasks and not terminal_tasks and omitted_active_count == 0:
        return ""

    lines = [_TASK_CONTEXT_HEADER]
    if active_tasks:
        lines.append("活跃任务：")
        for task in active_tasks:
            lines.extend(_active_task_lines(task))
    if terminal_tasks:
        lines.append("最近终态任务（最多 10 条）：")
        for task in terminal_tasks:
            lines.extend(_terminal_task_lines(task))
    if omitted_active_count:
        lines.append(
            f"- 另有 {omitted_active_count} 个活跃任务因 token 预算仅保留计数；"
            "完整详情可由 L0 主 AI 通过 subagent_task list/get 查询。"
        )
    return "\n".join(lines)


def compact_subagent_task_snapshot(snapshot: object) -> dict[str, list[dict[str, Any]]]:
    """Drop terminal details and long text while retaining every active task's state."""
    if not isinstance(snapshot, Mapping):
        return _empty_snapshot()
    active_tasks: list[dict[str, Any]] = []
    for task in _task_list(snapshot.get("active_tasks")):
        current_step_id = _text(task.get("current_step_id"))
        compact_plan = []
        for step in _task_list(task.get("plan")):
            step_id = _text(step.get("id"))
            compact_plan.append(
                {
                    "id": step_id,
                    "status": _text(step.get("status")),
                    "title": "",
                }
            )
        active_tasks.append(
            {
                "_compact": True,
                "id": task.get("id"),
                "title": "",
                "status": task.get("status"),
                "current_step_id": current_step_id or None,
                "progress_completed": sum(
                    1
                    for step in compact_plan
                    if step.get("status") in _COMPLETED_STEP_STATUSES
                ),
                "progress_total": len(compact_plan),
                "plan": compact_plan,
            }
        )
    status_order = {"interrupting": 0, "running": 1, "planning": 2, "queued": 3}
    active_tasks.sort(key=lambda item: status_order.get(_text(item.get("status")), 99))
    return {"active_tasks": active_tasks, "recent_terminal_tasks": []}


def iter_compact_subagent_task_snapshots(snapshot: object) -> Iterator[dict[str, Any]]:
    """Yield smaller active summaries, retaining highest-priority task rows first."""
    compact = compact_subagent_task_snapshot(snapshot)
    active_tasks = compact["active_tasks"]
    total = len(active_tasks)
    if total == 0:
        yield compact
        return
    # Always preserve at least one active task ID/status/progress row. If the budget
    # cannot fit even that, the caller returns the smallest available prompt.
    for kept_count in range(total, 0, -1):
        yield {
            "active_tasks": active_tasks[:kept_count],
            "recent_terminal_tasks": [],
            "omitted_active_count": total - kept_count,
        }


def _active_task_lines(task: dict[str, Any]) -> list[str]:
    plan = _task_list(task.get("plan"))
    completed = sum(
        1 for step in plan if _text(step.get("status")) in _COMPLETED_STEP_STATUSES
    )
    task_id = _text(task.get("id")) or "unknown"
    status = _text(task.get("status")) or "unknown"
    current_step_id = _text(task.get("current_step_id"))
    current_step = _find_step(plan, current_step_id)
    current_label = "无"
    if current_step_id:
        current_title = (
            _inline(current_step.get("title"), limit=160) if current_step else ""
        )
        current_label = (
            f"{current_step_id}: {current_title}" if current_title else current_step_id
        )

    title = _inline(task.get("title"), limit=200)
    title_part = f" 任务={title}" if title else ""
    if task.get("_compact"):
        completed = _nonnegative_int(task.get("progress_completed"))
        total = _nonnegative_int(task.get("progress_total"))
        return [
            f"- [task {task_id}] 状态={status} 进度={completed}/{total} "
            f"当前步骤={current_label}{title_part}"
        ]
    lines = [
        f"- [task {task_id}] 状态={status} 进度={completed}/{len(plan)} "
        f"当前步骤={current_label}{title_part}"
    ]
    objective = _inline(task.get("objective"), limit=500)
    if objective:
        lines.append(f"  目标：{objective}")
    for step in plan:
        step_id = _text(step.get("id")) or "unknown"
        step_status = _text(step.get("status")) or "unknown"
        step_title = _inline(step.get("title"), limit=240) or "（无标题）"
        step_description = _inline(step.get("description"), limit=320)
        description_part = f" — {step_description}" if step_description else ""
        marker = " 当前" if current_step_id and step_id == current_step_id else ""
        lines.append(
            f"  - [{step_id} {step_status}{marker}] {step_title}{description_part}"
        )
    return lines


def _terminal_task_lines(task: dict[str, Any]) -> list[str]:
    task_id = _text(task.get("id")) or "unknown"
    status = _text(task.get("status")) or "unknown"
    title = _inline(task.get("title"), limit=200)
    title_part = f" 任务={title}" if title else ""
    lines = [f"- [task {task_id}] 状态={status}{title_part}"]

    objective = _inline(task.get("objective"), limit=500)
    if objective:
        lines.append(f"  目标：{objective}")
    for summary_label, summary in _terminal_summaries(task):
        lines.append(f"  {summary_label}：{summary}")
    return lines


def _terminal_summaries(task: dict[str, Any]) -> list[tuple[str, str]]:
    summaries: list[tuple[str, str]] = []
    result = _inline(task.get("result"), limit=800)
    if result:
        summaries.append(("结果摘要", result))
    error = _inline(task.get("error"), limit=800)
    if error:
        summaries.append(("错误摘要", error))
    reason = _inline(task.get("interruption_reason"), limit=800)
    if reason:
        summaries.append(("中断原因", reason))
    return summaries


def _find_step(
    plan: list[dict[str, Any]],
    step_id: str,
) -> dict[str, Any] | None:
    if not step_id:
        return None
    return next((step for step in plan if _text(step.get("id")) == step_id), None)


def _task_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _empty_snapshot() -> dict[str, list[dict[str, Any]]]:
    return {"active_tasks": [], "recent_terminal_tasks": []}


def _inline(value: object, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _text(value: object) -> str:
    return str(value or "").strip()


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
