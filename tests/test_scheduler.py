from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.scheduler.scheduler import Scheduler


class SchedulerTickTests(unittest.TestCase):
    def test_daily_action_only_fires_at_target_minute(self) -> None:
        calls: list[datetime] = []
        scheduler = Scheduler()
        scheduler.add_daily(
            name="t",
            hour=3,
            minute=0,
            action=lambda now: calls.append(now),
        )

        # 不到点不触发。
        scheduler.tick(datetime(2026, 5, 23, 2, 59, tzinfo=timezone.utc))
        scheduler.tick(datetime(2026, 5, 23, 3, 1, tzinfo=timezone.utc))
        self.assertEqual(calls, [])

        # 到点触发一次。
        target = datetime(2026, 5, 23, 3, 0, tzinfo=timezone.utc)
        scheduler.tick(target)
        self.assertEqual(calls, [target])

        # 同一分钟内重复 tick 不再触发(_last_tick 去重)。
        scheduler.tick(target)
        self.assertEqual(calls, [target])

    def test_weekly_action_uses_python_weekday(self) -> None:
        calls: list[datetime] = []
        scheduler = Scheduler()
        # weekday=0 = 周一
        scheduler.add_weekly(
            name="t",
            weekday=0,
            hour=3,
            minute=30,
            action=lambda now: calls.append(now),
        )

        # 2026-05-25 是周一。
        monday = datetime(2026, 5, 25, 3, 30, tzinfo=timezone.utc)
        sunday = datetime(2026, 5, 24, 3, 30, tzinfo=timezone.utc)

        scheduler.tick(sunday)
        self.assertEqual(calls, [])

        scheduler.tick(monday)
        self.assertEqual(calls, [monday])

    def test_monthly_action_fires_on_target_day(self) -> None:
        calls: list[datetime] = []
        scheduler = Scheduler()
        scheduler.add_monthly(
            name="t",
            day=1,
            hour=4,
            minute=0,
            action=lambda now: calls.append(now),
        )

        scheduler.tick(datetime(2026, 5, 31, 4, 0, tzinfo=timezone.utc))
        scheduler.tick(datetime(2026, 6, 1, 3, 59, tzinfo=timezone.utc))
        self.assertEqual(calls, [])

        target = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)
        scheduler.tick(target)
        self.assertEqual(calls, [target])

    def test_action_exception_is_swallowed_and_logged(self) -> None:
        scheduler = Scheduler()

        def boom(now: datetime) -> None:
            raise RuntimeError("planned failure")

        scheduler.add_daily(name="t", hour=3, minute=0, action=boom)

        # 抛错也不能让 tick 自己崩溃。
        scheduler.tick(datetime(2026, 5, 23, 3, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
