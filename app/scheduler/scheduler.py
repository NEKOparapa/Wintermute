from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

logger = logging.getLogger(__name__)

# 调度线程的检查间隔。1 分钟一次足够,trigger 只接受到分钟的精度。
_TICK_SECONDS = 60.0


@dataclass(frozen=True)
class _Job:
    """单个定时任务的内部表示。"""

    name: str
    predicate: Callable[[datetime], bool]
    action: Callable[[datetime], None]


class Scheduler:
    """轻量定时调度器:后台线程,每分钟检查一次所有 job 并触发到点的任务。

    设计原则:
    - 时间精度到分钟即可,不需要秒级。
    - action 必须是 idempotent 的;时钟漂移或重启可能导致重复触发,不应造成副作用。
    - 任何 action 异常都被吞掉记日志,不影响其他 job 也不会让线程退出。
    """

    def __init__(self) -> None:
        """初始化任务列表和停止信号。"""
        self._jobs: list[_Job] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # 记录"最近一次已经处理过的分钟",防止同一分钟内重复触发。
        self._last_tick: tuple[int, int, int, int, int] | None = None

    # ------------------------------------------------------------------ 注册任务

    def add_daily(
        self,
        name: str,
        hour: int,
        minute: int,
        action: Callable[[datetime], None],
    ) -> None:
        """注册每天在 (hour:minute) 触发的任务。"""

        def predicate(now: datetime) -> bool:
            return now.hour == hour and now.minute == minute

        self._jobs.append(_Job(name=name, predicate=predicate, action=action))

    def add_weekly(
        self,
        name: str,
        weekday: int,
        hour: int,
        minute: int,
        action: Callable[[datetime], None],
    ) -> None:
        """注册每周在 (weekday, hour:minute) 触发的任务。weekday 用 0=周一..6=周日。"""

        def predicate(now: datetime) -> bool:
            return now.weekday() == weekday and now.hour == hour and now.minute == minute

        self._jobs.append(_Job(name=name, predicate=predicate, action=action))

    def add_monthly(
        self,
        name: str,
        day: int,
        hour: int,
        minute: int,
        action: Callable[[datetime], None],
    ) -> None:
        """注册每月在 (day, hour:minute) 触发的任务。"""

        def predicate(now: datetime) -> bool:
            return now.day == day and now.hour == hour and now.minute == minute

        self._jobs.append(_Job(name=name, predicate=predicate, action=action))

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动后台守护线程开始监控时间。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="wintermute-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("调度器已启动 jobs=%d", len(self._jobs))

    def stop(self, timeout: float = 5.0) -> None:
        """通知后台线程退出,等待其终止。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ----------------------------------------------------------- 同步 tick(测试用)

    def tick(self, now: datetime) -> None:
        """对所有任务跑一次"是否到点"判断,主要给测试用。"""
        current = (now.year, now.month, now.day, now.hour, now.minute)
        if current == self._last_tick:
            return
        self._last_tick = current
        for job in self._jobs:
            try:
                if job.predicate(now):
                    job.action(now)
            except Exception:
                logger.exception("调度任务异常 job=%s", job.name)

    # ----------------------------------------------------------- 内部循环

    def _run_loop(self) -> None:
        """守护线程主循环:每 _TICK_SECONDS 检查一次。"""
        while not self._stop_event.is_set():
            try:
                self.tick(datetime.now().astimezone())
            except Exception:
                # 只能保命,绝对不能让线程因异常退出。
                logger.exception("调度器 tick 失败")
            self._stop_event.wait(timeout=_TICK_SECONDS)
