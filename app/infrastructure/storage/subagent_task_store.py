from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


TASK_STATUSES = frozenset(
    {
        "queued",
        "planning",
        "running",
        "interrupting",
        "completed",
        "interrupted",
        "failed",
    }
)
TERMINAL_TASK_STATUSES = frozenset({"completed", "interrupted", "failed"})
NON_TERMINAL_TASK_STATUSES = TASK_STATUSES - TERMINAL_TASK_STATUSES

STEP_STATUSES = frozenset(
    {
        "pending",
        "in_progress",
        "completed",
        "failed",
        "interrupted",
        "skipped",
    }
)
TERMINAL_STEP_STATUSES = frozenset(
    {"completed", "failed", "interrupted", "skipped"}
)

NOTIFICATION_STATUSES = frozenset(
    {"not_required", "pending", "accepted", "failed"}
)

_TASK_TRANSITIONS = {
    "queued": frozenset({"planning", "interrupting", "failed"}),
    "planning": frozenset({"running", "interrupting", "failed"}),
    "running": frozenset({"interrupting", "completed", "failed"}),
    "interrupting": frozenset({"interrupted"}),
    "completed": frozenset(),
    "interrupted": frozenset(),
    "failed": frozenset(),
}

_STEP_TRANSITIONS = {
    "pending": frozenset({"in_progress", "skipped"}),
    "in_progress": frozenset({"completed", "failed", "interrupted"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "interrupted": frozenset(),
    "skipped": frozenset(),
}

_NOTIFICATION_TRANSITIONS = {
    "not_required": frozenset(),
    "pending": frozenset({"accepted", "failed"}),
    "failed": frozenset({"pending", "accepted", "failed"}),
    "accepted": frozenset(),
}

_UNSET = object()
_DIRECTORY_LOCKS: dict[str, threading.RLock] = {}
_DIRECTORY_LOCKS_GUARD = threading.Lock()


class SubagentTaskStore:
    """Persist subagent tasks as one atomic JSON file per task."""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.tasks_dir = self.data_dir / "subagent_tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._lock = _directory_lock(self.tasks_dir)

    def create_task(
        self,
        *,
        title: object,
        objective: object,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a queued task and return an isolated copy."""
        title_text = _required_text(title, "title")
        objective_text = _required_text(objective, "objective")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValueError("metadata 必须是对象。")

        now = _now_iso()
        task = {
            "id": str(uuid.uuid4()),
            "title": title_text,
            "objective": objective_text,
            "status": "queued",
            "plan": [],
            "current_step_id": None,
            "result": None,
            "error": None,
            "interruption_reason": None,
            "metadata": copy.deepcopy(dict(metadata or {})),
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "notification": {
                "status": "not_required",
                "attempts": 0,
                "last_error": None,
                "notified_at": None,
                "updated_at": now,
            },
        }
        with self._lock:
            self._write_task_unlocked(task)
        return _copy(task)

    def get_task(self, task_id: object) -> dict[str, Any] | None:
        """Return a task copy, or None when the UUID does not exist."""
        path = self._path_for_id(task_id)
        with self._lock:
            if not path.exists():
                return None
            return _copy(self._load_task_unlocked(path))

    def list_tasks(
        self,
        *,
        statuses: str | Iterable[str] | None = None,
        newest_first: bool = True,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List task copies, optionally filtered by task status."""
        normalized_statuses = _normalize_status_filter(statuses)
        normalized_limit = _normalize_limit(limit)
        with self._lock:
            tasks = [
                self._load_task_unlocked(path)
                for path in sorted(self.tasks_dir.glob("*.json"))
            ]

        if normalized_statuses is not None:
            tasks = [task for task in tasks if task["status"] in normalized_statuses]
        tasks.sort(key=_task_created_sort_key, reverse=newest_first)
        if normalized_limit is not None:
            tasks = tasks[:normalized_limit]
        return _copy(tasks)

    def set_plan(
        self,
        task_id: object,
        steps: Sequence[object],
    ) -> dict[str, Any]:
        """Set the immutable 1-20 step plan while the task is planning."""
        if isinstance(steps, str | bytes) or not isinstance(steps, Sequence):
            raise ValueError("steps 必须是数组。")
        if not 1 <= len(steps) <= 20:
            raise ValueError("计划必须包含 1 到 20 个步骤。")

        with self._lock:
            task, path = self._required_task_unlocked(task_id)
            if task["status"] != "planning":
                raise ValueError("只有 planning 任务可以设置计划。")
            if task["plan"]:
                raise ValueError("任务计划已经固定，不能重复设置。")

            now = _now_iso()
            task["plan"] = [
                _new_step(index, raw_step, now=now)
                for index, raw_step in enumerate(steps, start=1)
            ]
            task["updated_at"] = now
            self._write_json_unlocked(path, task)
            return _copy(task)

    def transition_task(
        self,
        task_id: object,
        status: object,
        *,
        result: object = _UNSET,
        error: object = _UNSET,
        interruption_reason: object = _UNSET,
    ) -> dict[str, Any]:
        """Apply one legal task-state transition and related terminal bookkeeping."""
        next_status = _task_status(status)
        with self._lock:
            task, path = self._required_task_unlocked(task_id)
            current_status = task["status"]
            if next_status == current_status:
                return _copy(task)
            if next_status not in _TASK_TRANSITIONS[current_status]:
                raise ValueError(
                    f"任务状态不能从 {current_status} 转换为 {next_status}。"
                )
            if next_status == "running" and not task["plan"]:
                raise ValueError("任务必须先设置计划才能运行。")
            if next_status == "completed" and not _plan_is_completed(task["plan"]):
                raise ValueError("所有步骤必须 completed 或 skipped 后任务才能完成。")

            now = _now_iso()
            task["status"] = next_status
            task["updated_at"] = now
            if next_status == "running" and task["started_at"] is None:
                task["started_at"] = now
            if (
                next_status in {"interrupting", "interrupted"}
                and interruption_reason is not _UNSET
            ):
                task["interruption_reason"] = _optional_text(interruption_reason)
            if result is not _UNSET:
                task["result"] = _optional_text(result)
            if error is not _UNSET:
                task["error"] = _optional_text(error)

            if next_status == "failed":
                _finish_unfinished_steps(task, failure=True, now=now)
            elif next_status == "interrupted":
                _finish_unfinished_steps(task, failure=False, now=now)

            if next_status in TERMINAL_TASK_STATUSES:
                task["current_step_id"] = None
                task["finished_at"] = now
                _queue_notification(task, now=now)

            self._write_json_unlocked(path, task)
            return _copy(task)

    def transition_step(
        self,
        task_id: object,
        step_id: object,
        status: object,
        *,
        result: object = _UNSET,
        error: object = _UNSET,
    ) -> dict[str, Any]:
        """Apply a legal transition to one plan step and return the whole task."""
        next_status = _step_status(status)
        step_id_text = _required_text(step_id, "step_id")
        with self._lock:
            task, path = self._required_task_unlocked(task_id)
            if task["status"] not in {"running", "interrupting"}:
                raise ValueError("只有 running 或 interrupting 任务可以更新步骤。")
            step_index, step = _find_step(task["plan"], step_id_text)
            current_status = step["status"]
            if next_status == current_status:
                return _copy(task)
            if next_status not in _STEP_TRANSITIONS[current_status]:
                raise ValueError(
                    f"步骤状态不能从 {current_status} 转换为 {next_status}。"
                )
            if task["status"] == "interrupting" and next_status != "interrupted":
                raise ValueError("interrupting 任务只能把进行中步骤标记为 interrupted。")
            if next_status == "in_progress":
                _validate_step_start(task, step_index)
            if next_status == "skipped":
                _validate_step_skip(task, step_index)

            now = _now_iso()
            step["status"] = next_status
            step["updated_at"] = now
            if next_status == "in_progress":
                step["started_at"] = now
                task["current_step_id"] = step_id_text
            if result is not _UNSET:
                step["result"] = _optional_text(result)
            if error is not _UNSET:
                step["error"] = _optional_text(error)
            if next_status in TERMINAL_STEP_STATUSES:
                step["finished_at"] = now
                if task["current_step_id"] == step_id_text:
                    task["current_step_id"] = None

            task["updated_at"] = now
            self._write_json_unlocked(path, task)
            return _copy(task)

    def request_interrupt(
        self,
        task_id: object,
        *,
        reason: object,
    ) -> dict[str, Any]:
        """Persist an interruption request; repeated requests are idempotent."""
        reason_text = _required_text(reason, "reason")
        with self._lock:
            task, path = self._required_task_unlocked(task_id)
            if task["status"] in TERMINAL_TASK_STATUSES:
                return _copy(task)
            if task["status"] == "interrupting":
                return _copy(task)

            now = _now_iso()
            if "interrupting" not in _TASK_TRANSITIONS[task["status"]]:
                raise ValueError(f"任务状态 {task['status']} 不能请求中断。")
            task["status"] = "interrupting"
            task["interruption_reason"] = reason_text
            task["updated_at"] = now
            self._write_json_unlocked(path, task)
            return _copy(task)

    def interrupt_unfinished(
        self,
        *,
        reason: object,
    ) -> list[dict[str, Any]]:
        """Atomically reconcile every non-terminal task to interrupted."""
        reason_text = _required_text(reason, "reason")
        interrupted: list[dict[str, Any]] = []
        with self._lock:
            for path in sorted(self.tasks_dir.glob("*.json")):
                task = self._load_task_unlocked(path)
                if task["status"] in TERMINAL_TASK_STATUSES:
                    continue
                now = _now_iso()
                task["status"] = "interrupted"
                task["interruption_reason"] = reason_text
                task["updated_at"] = now
                task["finished_at"] = now
                _finish_unfinished_steps(task, failure=False, now=now)
                task["current_step_id"] = None
                _queue_notification(task, now=now)
                self._write_json_unlocked(path, task)
                interrupted.append(_copy(task))
        interrupted.sort(key=_task_created_sort_key)
        return interrupted

    def update_notification(
        self,
        task_id: object,
        status: object,
        *,
        error: object = None,
    ) -> dict[str, Any]:
        """Record the result of a terminal-task L1 notification attempt."""
        next_status = _notification_status(status)
        if next_status == "not_required":
            raise ValueError("终态任务通知不能恢复为 not_required。")

        with self._lock:
            task, path = self._required_task_unlocked(task_id)
            if task["status"] not in TERMINAL_TASK_STATUSES:
                raise ValueError("只有终态任务可以更新通知状态。")
            notification = task["notification"]
            current_status = notification["status"]
            if next_status == current_status:
                return _copy(task)
            if next_status not in _NOTIFICATION_TRANSITIONS[current_status]:
                raise ValueError(
                    f"通知状态不能从 {current_status} 转换为 {next_status}。"
                )

            now = _now_iso()
            notification["status"] = next_status
            notification["updated_at"] = now
            if next_status in {"accepted", "failed"}:
                notification["attempts"] = int(notification.get("attempts", 0)) + 1
            if next_status == "accepted":
                notification["notified_at"] = now
                notification["last_error"] = None
            elif next_status == "failed":
                notification["last_error"] = _optional_text(error)
            elif next_status == "pending":
                notification["last_error"] = None
            task["updated_at"] = now
            self._write_json_unlocked(path, task)
            return _copy(task)

    def context_snapshot(
        self,
        *,
        recent_terminal_limit: int = 5,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return all active tasks plus the most recently finished terminal tasks."""
        limit = _normalize_limit(recent_terminal_limit)
        if limit is None:
            raise ValueError("recent_terminal_limit 不能为空。")
        with self._lock:
            tasks = [
                self._load_task_unlocked(path)
                for path in sorted(self.tasks_dir.glob("*.json"))
            ]

        active = [task for task in tasks if task["status"] in NON_TERMINAL_TASK_STATUSES]
        terminal = [task for task in tasks if task["status"] in TERMINAL_TASK_STATUSES]
        active.sort(key=_task_created_sort_key)
        terminal.sort(key=_task_finished_sort_key, reverse=True)
        return {
            "active_tasks": _copy(active),
            "recent_terminal_tasks": _copy(terminal[:limit]),
        }

    def _required_task_unlocked(
        self,
        task_id: object,
    ) -> tuple[dict[str, Any], Path]:
        path = self._path_for_id(task_id)
        if not path.exists():
            raise ValueError(f"子代理任务不存在: {_normalize_task_id(task_id)}")
        return self._load_task_unlocked(path), path

    def _load_task_unlocked(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as file:
                raw = json.load(file)
        except json.JSONDecodeError as exc:
            raise ValueError(f"子代理任务文件不是有效 JSON: {path}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"子代理任务文件必须是 JSON 对象: {path}")
        task = dict(raw)
        if task.get("id") != path.stem:
            raise ValueError(f"子代理任务 id 与文件名不一致: {path}")
        if task.get("status") not in TASK_STATUSES:
            raise ValueError(f"子代理任务状态无效: {path}")
        if not isinstance(task.get("plan"), list):
            raise ValueError(f"子代理任务 plan 必须是数组: {path}")
        return task

    def _write_task_unlocked(self, task: dict[str, Any]) -> None:
        self._write_json_unlocked(self._path_for_id(task["id"]), task)

    @staticmethod
    def _write_json_unlocked(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _path_for_id(self, task_id: object) -> Path:
        return self.tasks_dir / f"{_normalize_task_id(task_id)}.json"


def _directory_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _DIRECTORY_LOCKS_GUARD:
        lock = _DIRECTORY_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DIRECTORY_LOCKS[key] = lock
        return lock


def _normalize_task_id(value: object) -> str:
    text = str(value or "").strip()
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError) as exc:
        raise ValueError("task_id 必须是 UUID。") from exc


def _new_step(index: int, raw: object, *, now: str) -> dict[str, Any]:
    if isinstance(raw, str):
        title = _required_text(raw, f"steps[{index - 1}]")
        description = ""
    elif isinstance(raw, Mapping):
        title = _required_text(raw.get("title"), f"steps[{index - 1}].title")
        description = _optional_text(raw.get("description")) or ""
    else:
        raise ValueError(f"steps[{index - 1}] 必须是字符串或对象。")
    return {
        "id": f"step-{index}",
        "index": index,
        "title": title,
        "description": description,
        "status": "pending",
        "result": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }


def _validate_step_start(task: dict[str, Any], step_index: int) -> None:
    if task["status"] != "running":
        raise ValueError("只有 running 任务可以开始步骤。")
    if task["current_step_id"] is not None:
        raise ValueError("同一任务只能有一个 in_progress 步骤。")
    previous = task["plan"][:step_index]
    if any(step["status"] not in {"completed", "skipped"} for step in previous):
        raise ValueError("必须按计划顺序执行步骤。")


def _validate_step_skip(task: dict[str, Any], step_index: int) -> None:
    if task["status"] != "running":
        raise ValueError("只有 running 任务可以跳过步骤。")
    if task["current_step_id"] is not None:
        raise ValueError("存在 in_progress 步骤时不能跳过其它步骤。")
    previous = task["plan"][:step_index]
    if any(step["status"] not in {"completed", "skipped"} for step in previous):
        raise ValueError("必须按计划顺序处理步骤。")


def _find_step(plan: list[dict[str, Any]], step_id: str) -> tuple[int, dict[str, Any]]:
    for index, step in enumerate(plan):
        if step.get("id") == step_id:
            return index, step
    raise ValueError(f"计划步骤不存在: {step_id}")


def _finish_unfinished_steps(
    task: dict[str, Any],
    *,
    failure: bool,
    now: str,
) -> None:
    current_step_id = task.get("current_step_id")
    for step in task["plan"]:
        status = step.get("status")
        if status in TERMINAL_STEP_STATUSES:
            continue
        if step.get("id") == current_step_id:
            next_status = "failed" if failure else "interrupted"
        else:
            next_status = "skipped"
        step["status"] = next_status
        step["updated_at"] = now
        step["finished_at"] = now
        if next_status == "failed" and not step.get("error"):
            step["error"] = task.get("error")


def _queue_notification(task: dict[str, Any], *, now: str) -> None:
    notification = task["notification"]
    if notification["status"] == "accepted":
        return
    notification["status"] = "pending"
    notification["last_error"] = None
    notification["updated_at"] = now


def _plan_is_completed(plan: list[dict[str, Any]]) -> bool:
    return bool(plan) and all(
        step.get("status") in {"completed", "skipped"} for step in plan
    )


def _task_status(value: object) -> str:
    status = str(value or "").strip().lower()
    if status not in TASK_STATUSES:
        raise ValueError(f"任务状态无效: {status or '(空)'}")
    return status


def _step_status(value: object) -> str:
    status = str(value or "").strip().lower()
    if status not in STEP_STATUSES:
        raise ValueError(f"步骤状态无效: {status or '(空)'}")
    return status


def _notification_status(value: object) -> str:
    status = str(value or "").strip().lower()
    if status not in NOTIFICATION_STATUSES:
        raise ValueError(f"通知状态无效: {status or '(空)'}")
    return status


def _normalize_status_filter(
    statuses: str | Iterable[str] | None,
) -> frozenset[str] | None:
    if statuses is None:
        return None
    values: Iterable[str] = (statuses,) if isinstance(statuses, str) else statuses
    normalized = frozenset(_task_status(value) for value in values)
    return normalized


def _normalize_limit(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 必须是非负整数。") from exc
    if limit < 0:
        raise ValueError("limit 必须是非负整数。")
    return limit


def _required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} 不能为空。")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _task_created_sort_key(task: dict[str, Any]) -> tuple[str, str]:
    return str(task.get("created_at") or ""), str(task.get("id") or "")


def _task_finished_sort_key(task: dict[str, Any]) -> tuple[str, str]:
    return str(task.get("finished_at") or task.get("updated_at") or ""), str(
        task.get("id") or ""
    )


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)
