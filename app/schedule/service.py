from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

from ..flows.flow_runtime import FlowSubmitRequest, FlowSubmitResult
from ..infrastructure.storage.schedule_store import ScheduleStore

logger = logging.getLogger(__name__)


class ScheduleTriggerService:
    """Background checker that submits due schedules into the L1 flow."""

    def __init__(
        self,
        store: ScheduleStore,
        submit: Callable[[FlowSubmitRequest], FlowSubmitResult],
        *,
        poll_interval_seconds: float = 30.0,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.submit = submit
        self.poll_interval_seconds = max(1.0, poll_interval_seconds)
        self.now_func = now_func or (lambda: datetime.now().astimezone())
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="wintermute-schedule-trigger",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def check_once(self) -> None:
        now = self.now_func()
        for schedule in self.store.due_schedules(now):
            schedule_id = str(schedule.get("id", "")).strip()
            if not schedule_id:
                continue
            request = FlowSubmitRequest(
                level="L1",
                message=_trigger_message(schedule),
                source="schedule",
                type="schedule_trigger",
                metadata={
                    "schedule_id": schedule_id,
                    "schedule": schedule,
                    "next_trigger_at": schedule.get("next_trigger_at"),
                },
            )
            try:
                result = self.submit(request)
            except Exception:  # noqa: BLE001 - 后台线程不能因单条提交失败退出
                logger.exception("日程触发提交 L1 失败 schedule_id=%s", schedule_id)
                continue
            if result.status == "error":
                logger.error(
                    "日程触发提交 L1 被拒绝 schedule_id=%s error=%s",
                    schedule_id,
                    result.error,
                )
                continue
            self.store.mark_triggered(schedule_id, triggered_at=now)
            logger.info("日程已触发 schedule_id=%s task_id=%s", schedule_id, result.task_id)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("日程触发检查失败")
            if self._stop_event.wait(self.poll_interval_seconds):
                return


def _trigger_message(schedule: dict[str, object]) -> str:
    title = str(schedule.get("title") or "").strip()
    content = str(schedule.get("content") or "").strip()
    trigger_at = str(schedule.get("next_trigger_at") or "").strip()
    if content:
        return f"日程触发：{title}\n时间：{trigger_at}\n内容：{content}"
    return f"日程触发：{title}\n时间：{trigger_at}"
