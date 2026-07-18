from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..infrastructure.storage.subagent_task_store import (
    TERMINAL_TASK_STATUSES,
    SubagentTaskStore,
)
from .service import SubagentService

logger = logging.getLogger(__name__)


class _SubmitResultLike(Protocol):
    status: str
    error: str | None


Notifier = Callable[[Any], _SubmitResultLike]


@dataclass
class _TaskHandle:
    cancel_event: threading.Event
    start_gate: threading.Event
    future: Future[dict[str, Any]]


class SubagentManager:
    """Own the bounded worker pool and lifecycle of persisted subagent tasks."""

    def __init__(
        self,
        store: SubagentTaskStore,
        service: SubagentService,
        *,
        max_concurrency: int = 2,
    ) -> None:
        self.store = store
        self.service = service
        self.max_concurrency = max(1, int(max_concurrency))
        self._executor: ThreadPoolExecutor | None = None
        self._notifier: Notifier | None = None
        self._handles: dict[str, _TaskHandle] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._notifying_ids: set[str] = set()
        self._state = "stopped"
        self._started = False
        self._accepting = False

    def start(self, notifier: Notifier) -> None:
        """Start workers, reconcile crash leftovers, and notify their interruption."""
        with self._condition:
            while self._state in {"starting", "stopping"}:
                self._condition.wait()
            if self._state == "running":
                return
            self._state = "starting"
            self._notifier = notifier
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_concurrency,
                thread_name_prefix="wintermute-subagent",
            )
            self._started = True
            self._accepting = False
        try:
            # Do not retry a previous process's ambiguous terminal notification.
            for task in self.store.list_tasks(
                statuses=TERMINAL_TASK_STATUSES,
                newest_first=False,
            ):
                notification = task.get("notification")
                if isinstance(notification, dict) and notification.get("status") == "pending":
                    self.store.update_notification(
                        task["id"],
                        "failed",
                        error="服务重启前未确认 L1 通知是否已入队；按不重试策略关闭。",
                    )

            recovered = self.store.interrupt_unfinished(reason="service_restarted")
            for task in recovered:
                self._notify_terminal(task)
        except Exception:
            executor = self._executor
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            with self._condition:
                self._executor = None
                self._notifier = None
                self._started = False
                self._accepting = False
                self._state = "stopped"
                self._condition.notify_all()
            raise
        with self._condition:
            self._accepting = True
            self._state = "running"
            self._condition.notify_all()

    def create_task(
        self,
        objective: object,
        *,
        origin: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        objective_text = str(objective or "").strip()
        if not objective_text:
            raise ValueError("objective 不能为空。")
        with self._lock:
            if not self._started or not self._accepting or self._executor is None:
                raise RuntimeError("子代理管理器未运行。")
            task = self.store.create_task(
                title=_task_title(objective_text),
                objective=objective_text,
                metadata={"origin": _clean_origin(origin)},
            )
            cancel_event = threading.Event()
            start_gate = threading.Event()
            try:
                future = self._executor.submit(
                    self._run_task,
                    str(task["id"]),
                    cancel_event,
                    start_gate,
                )
            except Exception as exc:
                cancel_event.set()
                start_gate.set()
                failed = self.store.transition_task(
                    task["id"],
                    "failed",
                    error=f"子代理入队失败: {exc}",
                )
                self._notify_terminal(failed)
                return failed
            self._handles[str(task["id"])] = _TaskHandle(
                cancel_event=cancel_event,
                start_gate=start_gate,
                future=future,
            )
            start_gate.set()
            return task

    def get_task(self, task_id: object) -> dict[str, Any] | None:
        return self.store.get_task(task_id)

    def list_tasks(
        self,
        *,
        status: object = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        statuses = None if status is None or status == "" else str(status).strip().lower()
        return self.store.list_tasks(statuses=statuses, limit=limit)

    def interrupt_task(
        self,
        task_id: object,
        *,
        reason: object = "requested_by_main_ai",
    ) -> dict[str, Any]:
        with self._lock:
            task = self.store.get_task(task_id)
            if task is None:
                raise ValueError(f"子代理任务不存在: {task_id}")
            if task["status"] in TERMINAL_TASK_STATUSES:
                return task
            task_id_text = str(task["id"])
            task = self.store.request_interrupt(task_id_text, reason=reason)
            if task["status"] in TERMINAL_TASK_STATUSES:
                return task
            handle = self._handles.get(task_id_text)
            if handle is not None:
                handle.cancel_event.set()
                cancelled_before_start = handle.future.cancel()
            else:
                cancelled_before_start = True
            if cancelled_before_start:
                task = self.store.transition_task(task_id_text, "interrupted")
                self._handles.pop(task_id_text, None)
                # Reserve and finish notification while holding the re-entrant manager
                # lock so stop() cannot pass the handle-to-notifier handoff gap.
                self._notify_terminal(task)
        return self.store.get_task(task_id_text) or task

    def stop(self) -> None:
        """Stop accepting work and cooperatively interrupt every active task."""
        with self._condition:
            while self._state == "starting":
                self._condition.wait()
            if self._state == "stopped":
                return
            if self._state == "stopping":
                while self._state == "stopping":
                    self._condition.wait()
                return
            self._state = "stopping"
            self._accepting = False
            task_ids = list(self._handles)
            executor = self._executor
        for task_id in task_ids:
            try:
                self.interrupt_task(task_id, reason="service_shutdown")
            except Exception:  # noqa: BLE001 - continue stopping remaining tasks
                logger.exception("关闭时中断子代理失败 task_id=%s", task_id)
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
        with self._condition:
            while self._notifying_ids:
                self._condition.wait()
            self._handles.clear()
            self._executor = None
            self._notifier = None
            self._started = False
            self._state = "stopped"
            self._condition.notify_all()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._handles)

    def _run_task(
        self,
        task_id: str,
        cancel_event: threading.Event,
        start_gate: threading.Event,
    ) -> dict[str, Any]:
        start_gate.wait()
        try:
            return self.service.run(task_id, cancel_event)
        except Exception as exc:  # noqa: BLE001 - final safety net around a worker
            logger.exception("子代理 worker 异常 task_id=%s", task_id)
            task = self.store.get_task(task_id)
            if task is None:
                raise
            if task["status"] not in TERMINAL_TASK_STATUSES:
                if task["status"] == "interrupting":
                    task = self.store.transition_task(task_id, "interrupted")
                else:
                    try:
                        task = self.store.transition_task(task_id, "failed", error=str(exc))
                    except ValueError:
                        latest = self.store.get_task(task_id)
                        if latest is not None and latest.get("status") == "interrupting":
                            task = self.store.transition_task(task_id, "interrupted")
                        else:
                            raise
            return task
        finally:
            with self._lock:
                self._handles.pop(task_id, None)
            task = self.store.get_task(task_id)
            if task is not None and task["status"] in TERMINAL_TASK_STATUSES:
                self._notify_terminal(task)

    def _notify_terminal(self, task: dict[str, Any]) -> None:
        # Late import avoids a tools -> subagent -> flow runtime -> dialogue -> tools cycle.
        from ..flows.flow_runtime import FlowSubmitRequest

        task_id = str(task["id"])
        reserved = False
        try:
            with self._condition:
                if task_id in self._notifying_ids:
                    return
                latest = self.store.get_task(task_id)
                notification = latest.get("notification") if latest is not None else None
                if (
                    latest is None
                    or not isinstance(notification, dict)
                    or notification.get("status") != "pending"
                ):
                    return
                self._notifying_ids.add(task_id)
                reserved = True

            notifier = self._notifier
            if notifier is None:
                self.store.update_notification(
                    task_id,
                    "failed",
                    error="L1 notifier 未配置。",
                )
                return

            origin = latest.get("metadata", {}).get("origin", {})
            reply_target = origin.get("reply_target") if isinstance(origin, dict) else None
            input_interface = (
                str(origin.get("input_interface") or "").strip() or None
                if isinstance(origin, dict)
                else None
            )
            request = FlowSubmitRequest(
                level="L1",
                message=_notification_message(latest),
                source="subagent",
                type=f"subagent_task_{latest['status']}",
                input_interface=input_interface,
                reply_target=(
                    dict(reply_target) if isinstance(reply_target, dict) else None
                ),
                metadata={
                    "subagent_task_id": latest["id"],
                    "subagent_task_status": latest["status"],
                    "subagent_task": latest,
                    "origin_input_interface": input_interface,
                },
            )
            result = notifier(request)
            if result.status == "error":
                self.store.update_notification(
                    task_id,
                    "failed",
                    error=result.error or "L1 通知被拒绝。",
                )
            else:
                self.store.update_notification(task_id, "accepted")
        except Exception as exc:  # noqa: BLE001 - notification must not roll back task
            logger.exception("子代理 L1 通知提交失败 task_id=%s", task_id)
            latest = self.store.get_task(task_id)
            notification = latest.get("notification") if latest is not None else None
            if isinstance(notification, dict) and notification.get("status") == "pending":
                self.store.update_notification(task_id, "failed", error=str(exc))
        finally:
            if reserved:
                with self._condition:
                    self._notifying_ids.discard(task_id)
                    self._condition.notify_all()


def _task_title(objective: str) -> str:
    first_line = next((line.strip() for line in objective.splitlines() if line.strip()), objective)
    return first_line[:80]


def _clean_origin(origin: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(origin, dict):
        return {"input_interface": None, "reply_target": None}
    reply_target = origin.get("reply_target")
    return {
        "input_interface": str(origin.get("input_interface") or "").strip() or None,
        "reply_target": dict(reply_target) if isinstance(reply_target, dict) else None,
    }


def _notification_message(task: dict[str, Any]) -> str:
    plan = task.get("plan") if isinstance(task.get("plan"), list) else []
    completed = sum(1 for step in plan if step.get("status") == "completed")
    lines = [
        "子代理任务状态更新",
        f"任务 ID：{task.get('id')}",
        f"目标：{_bounded_text(task.get('objective'), 1000)}",
        f"状态：{task.get('status')}",
        f"步骤：{completed}/{len(plan)} 已完成",
    ]
    if task.get("result"):
        lines.append(f"结果：{_bounded_text(task['result'], 3000)}")
    if task.get("interruption_reason"):
        lines.append(f"中断原因：{_bounded_text(task['interruption_reason'], 1000)}")
    if task.get("error"):
        lines.append(f"错误：{_bounded_text(task['error'], 1000)}")
    return "\n".join(lines)


def _bounded_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"
