from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable

from .consolidator import MemoryConsolidator, previous_day, previous_month_start, previous_week_start

logger = logging.getLogger(__name__)


@dataclass
class _ScheduledTask:
    name: str
    next_run: datetime
    run: Callable[[datetime], None]
    next_after: Callable[[datetime], datetime]


class MemoryScheduler:
    """标准库后台线程调度 daily/weekly/monthly 压缩任务。"""

    def __init__(
        self,
        consolidator: MemoryConsolidator,
        *,
        profile_updater=None,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self.consolidator = consolidator
        self.profile_updater = profile_updater
        self.now_func = now_func or (lambda: datetime.now().astimezone())
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tasks: list[_ScheduledTask] = []

    def start(self) -> None:
        """启动后台线程；只安排未来任务，不补跑历史缺口。"""
        if self._thread is not None and self._thread.is_alive():
            return
        now = self.now_func()
        self._tasks = [
            _ScheduledTask("daily", next_daily_run(now), self._run_daily, next_daily_run),
            _ScheduledTask("weekly", next_weekly_run(now), self._run_weekly, next_weekly_run),
            _ScheduledTask("monthly", next_monthly_run(now), self._run_monthly, next_monthly_run),
        ]
        if self.profile_updater is not None and getattr(self.profile_updater, "enabled", False):
            # 画像刷新排在对应记忆压缩之后：user 在 daily 之后，persona 在 weekly 之后。
            self._tasks.append(
                _ScheduledTask(
                    "profile_user",
                    next_profile_user_run(now),
                    self._run_profile_user,
                    next_profile_user_run,
                )
            )
            self._tasks.append(
                _ScheduledTask(
                    "profile_persona",
                    next_profile_persona_run(now),
                    self._run_profile_persona,
                    next_profile_persona_run,
                )
            )
        self._thread = threading.Thread(target=self._loop, name="wintermute-memory-scheduler", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """停止后台线程。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            now = self.now_func()
            next_task_time = min(task.next_run for task in self._tasks)
            wait_seconds = max(0.0, (next_task_time - now).total_seconds())
            if self._stop_event.wait(wait_seconds):
                return

            now = self.now_func()
            for task in self._tasks:
                if task.next_run <= now:
                    run_time = task.next_run
                    try:
                        task.run(run_time)
                    except Exception:
                        logger.exception("记忆定时任务失败 task=%s", task.name)
                    task.next_run = task.next_after(now)

    def _run_daily(self, run_time: datetime) -> None:
        result = self.consolidator.consolidate_daily(previous_day(run_time))
        logger.info("daily 记忆任务完成 created=%s reason=%s", result.created, result.reason)

    def _run_weekly(self, run_time: datetime) -> None:
        result = self.consolidator.consolidate_weekly(previous_week_start(run_time))
        logger.info("weekly 记忆任务完成 created=%s reason=%s", result.created, result.reason)

    def _run_monthly(self, run_time: datetime) -> None:
        result = self.consolidator.consolidate_monthly(previous_month_start(run_time))
        logger.info("monthly 记忆任务完成 created=%s reason=%s", result.created, result.reason)

    def _run_profile_user(self, run_time: datetime) -> None:
        result = self.profile_updater.update_user(previous_day(run_time))
        logger.info("user 画像任务完成 updated=%s reason=%s", result.updated, result.reason)

    def _run_profile_persona(self, run_time: datetime) -> None:
        result = self.profile_updater.update_persona(previous_week_start(run_time))
        logger.info("persona 画像任务完成 updated=%s reason=%s", result.updated, result.reason)


def next_daily_run(now: datetime) -> datetime:
    return _next_wall_time(now, weekday=None, day=now.day, wall_time=time(3, 0))


def next_weekly_run(now: datetime) -> datetime:
    return _next_wall_time(now, weekday=0, day=None, wall_time=time(3, 30))


def next_monthly_run(now: datetime) -> datetime:
    candidate = now.replace(day=1, hour=4, minute=0, second=0, microsecond=0)
    if candidate <= now:
        if now.month == 12:
            candidate = candidate.replace(year=now.year + 1, month=1)
        else:
            candidate = candidate.replace(month=now.month + 1)
    return candidate


def next_profile_user_run(now: datetime) -> datetime:
    # 03:15，排在 daily 压缩（03:00）之后，消化前一天的日记忆。
    return _next_wall_time(now, weekday=None, day=now.day, wall_time=time(3, 15))


def next_profile_persona_run(now: datetime) -> datetime:
    # 周一 03:45，排在 weekly 压缩（周一 03:30）之后，消化上一周的周记忆。
    return _next_wall_time(now, weekday=0, day=None, wall_time=time(3, 45))


def _next_wall_time(
    now: datetime,
    *,
    weekday: int | None,
    day: int | None,
    wall_time: time,
) -> datetime:
    candidate = now.replace(
        hour=wall_time.hour,
        minute=wall_time.minute,
        second=0,
        microsecond=0,
    )
    if weekday is not None:
        days_ahead = (weekday - now.weekday()) % 7
        candidate += timedelta(days=days_ahead)
    if day is not None:
        candidate = candidate.replace(day=day)
    if candidate <= now:
        candidate += timedelta(days=7 if weekday is not None else 1)
    return candidate
