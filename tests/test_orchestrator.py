from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from app.memory.consolidator import Consolidator
from app.memory.memory import MemoryEntry, MemoryKind, MemoryStore
from app.memory.orchestrator import MemoryOrchestrator
from app.memory.tokens import TokenCounter
from app.storage.storage import GlobalEventStore


class CountingLLM:
    """记录每次调用的 system prompt 和返回固定文本。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, *, system: str, messages: list[dict[str, str]]) -> str:
        self.calls.append(system)
        return f"compressed:{len(self.calls)}"


def _seed_events(events_dir: Path, target_date: date, count: int) -> None:
    """在 events/{date}.json 里手工写入若干用户/助手事件。"""
    events_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(count):
        rows.append(
            {
                "id": f"u-{target_date.isoformat()}-{index}",
                "timestamp": f"{target_date.isoformat()}T10:0{index}:00+00:00",
                "source": "user",
                "type": "user_message",
                "content": f"用户消息 {index}",
                "metadata": {},
            }
        )
        rows.append(
            {
                "id": f"a-{target_date.isoformat()}-{index}",
                "timestamp": f"{target_date.isoformat()}T10:0{index}:30+00:00",
                "source": "assistant",
                "type": "assistant_response",
                "content": f"助手回复 {index}",
                "metadata": {},
            }
        )
    (events_dir / f"{target_date.isoformat()}.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8"
    )


class OrchestratorDailyTests(unittest.TestCase):
    def test_rollup_daily_creates_entry_with_full_day_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = date(2026, 5, 22)
            _seed_events(root / "events", target, count=3)

            llm = CountingLLM()
            orch = MemoryOrchestrator(
                GlobalEventStore(root),
                MemoryStore(root),
                Consolidator(llm, TokenCounter()),
            )

            entry = orch.rollup_daily(target)

            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.kind, MemoryKind.DAILY)
            self.assertEqual(entry.period_start.date(), target)
            self.assertEqual(entry.period_end.date(), target)
            self.assertEqual(len(entry.source_event_ids), 6)
            self.assertEqual(len(llm.calls), 1)

    def test_rollup_daily_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = date(2026, 5, 22)
            _seed_events(root / "events", target, count=2)

            llm = CountingLLM()
            orch = MemoryOrchestrator(
                GlobalEventStore(root),
                MemoryStore(root),
                Consolidator(llm, TokenCounter()),
            )

            first = orch.rollup_daily(target)
            second = orch.rollup_daily(target)

            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(len(llm.calls), 1)

    def test_rollup_daily_skips_days_without_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            llm = CountingLLM()
            orch = MemoryOrchestrator(
                GlobalEventStore(root),
                MemoryStore(root),
                Consolidator(llm, TokenCounter()),
            )

            entry = orch.rollup_daily(date(2026, 1, 1))

            self.assertIsNone(entry)
            self.assertEqual(llm.calls, [])


class OrchestratorWeeklyTests(unittest.TestCase):
    def test_rollup_weekly_collects_only_daily_in_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(root)

            # ISO 2026-W21 是 2026-05-18 ~ 2026-05-24。
            in_week = date(2026, 5, 20)
            out_week = date(2026, 5, 30)

            tz = timezone.utc
            store.append(
                MemoryEntry.new(
                    kind=MemoryKind.DAILY,
                    period_start=datetime.combine(in_week, datetime.min.time(), tzinfo=tz),
                    period_end=datetime.combine(in_week, datetime.max.time(), tzinfo=tz),
                    summary=f"daily {in_week}",
                    tokens=50,
                )
            )
            store.append(
                MemoryEntry.new(
                    kind=MemoryKind.DAILY,
                    period_start=datetime.combine(out_week, datetime.min.time(), tzinfo=tz),
                    period_end=datetime.combine(out_week, datetime.max.time(), tzinfo=tz),
                    summary=f"daily {out_week}",
                    tokens=50,
                )
            )

            llm = CountingLLM()
            orch = MemoryOrchestrator(
                GlobalEventStore(root),
                store,
                Consolidator(llm, TokenCounter()),
            )

            entry = orch.rollup_weekly(2026, 21)

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.kind, MemoryKind.WEEKLY)
        # 只有 in_week 那一条 daily 被纳入压缩。
        self.assertEqual(len(entry.source_memory_ids), 1)

    def test_rollup_weekly_returns_none_when_no_daily(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            llm = CountingLLM()
            orch = MemoryOrchestrator(
                GlobalEventStore(root),
                MemoryStore(root),
                Consolidator(llm, TokenCounter()),
            )

            entry = orch.rollup_weekly(2026, 21)

            self.assertIsNone(entry)
            self.assertEqual(llm.calls, [])


class OrchestratorMonthlyTests(unittest.TestCase):
    def test_rollup_monthly_uses_weekly_within_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(root)

            # 给 2026-05 写两条 weekly,给 2026-04 写一条。
            tz = timezone.utc

            def _add_weekly(period_start: datetime, period_end: datetime, label: str) -> None:
                store.append(
                    MemoryEntry.new(
                        kind=MemoryKind.WEEKLY,
                        period_start=period_start,
                        period_end=period_end,
                        summary=label,
                        tokens=50,
                    )
                )

            _add_weekly(
                datetime(2026, 5, 4, tzinfo=tz),
                datetime(2026, 5, 10, 23, 59, tzinfo=tz),
                "weekly may1",
            )
            _add_weekly(
                datetime(2026, 5, 11, tzinfo=tz),
                datetime(2026, 5, 17, 23, 59, tzinfo=tz),
                "weekly may2",
            )
            _add_weekly(
                datetime(2026, 4, 6, tzinfo=tz),
                datetime(2026, 4, 12, 23, 59, tzinfo=tz),
                "weekly apr",
            )

            llm = CountingLLM()
            orch = MemoryOrchestrator(
                GlobalEventStore(root),
                store,
                Consolidator(llm, TokenCounter()),
            )

            entry = orch.rollup_monthly(2026, 5)

        self.assertIsNotNone(entry)
        assert entry is not None
        # 只有 5 月的两条 weekly 被纳入。
        self.assertEqual(len(entry.source_memory_ids), 2)


class OrchestratorCatchUpTests(unittest.TestCase):
    def test_catch_up_recent_fills_missing_daily(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yesterday = date(2026, 5, 22)
            day_before = date(2026, 5, 21)
            _seed_events(root / "events", yesterday, count=1)
            _seed_events(root / "events", day_before, count=1)

            llm = CountingLLM()
            orch = MemoryOrchestrator(
                GlobalEventStore(root),
                MemoryStore(root),
                Consolidator(llm, TokenCounter()),
            )

            now = datetime(2026, 5, 23, 9, 0, tzinfo=timezone.utc)
            produced = orch.catch_up_recent(now=now)

        # 两天的 daily 都应该被补出来。
        self.assertEqual(set(produced["daily"]), {yesterday, day_before})


if __name__ == "__main__":
    unittest.main()
