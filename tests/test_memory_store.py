from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.memory.memory import MemoryEntry, MemoryKind, MemoryStore


class MemoryStoreTests(unittest.TestCase):
    """覆盖按 kind 分目录、按 period 分文件的记忆存储。"""

    def test_session_entries_grouped_by_period_start_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            entry = MemoryEntry.new(
                kind=MemoryKind.SESSION,
                period_start=datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc),
                summary="上午对话压缩",
                tokens=120,
                source_event_ids=["e1", "e2"],
            )
            store.append(entry)

            file_path = Path(tmp) / "memories" / "session" / "2026-05-23.json"
            self.assertTrue(file_path.exists())

            loaded = store.load_by_kind(MemoryKind.SESSION)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].summary, "上午对话压缩")
        self.assertEqual(loaded[0].source_event_ids, ["e1", "e2"])

    def test_weekly_entry_filename_uses_iso_year_and_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            # 2026-01-01 在 ISO 周里属于 2026-W01。
            entry = MemoryEntry.new(
                kind=MemoryKind.WEEKLY,
                period_start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 1, 4, 23, 59, tzinfo=timezone.utc),
                summary="2026 第一周",
                tokens=300,
            )
            store.append(entry)

            file_path = Path(tmp) / "memories" / "weekly" / "2026-W01.json"
            self.assertTrue(file_path.exists())

    def test_monthly_entry_filename_uses_year_and_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            entry = MemoryEntry.new(
                kind=MemoryKind.MONTHLY,
                period_start=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 5, 31, 23, 59, tzinfo=timezone.utc),
                summary="五月",
                tokens=500,
            )
            store.append(entry)

            file_path = Path(tmp) / "memories" / "monthly" / "2026-05.json"
            self.assertTrue(file_path.exists())

    def test_has_for_period_is_true_only_after_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            start = datetime(2026, 5, 23, 0, 0, tzinfo=timezone.utc)
            end = datetime(2026, 5, 23, 23, 59, tzinfo=timezone.utc)

            self.assertFalse(store.has_for_period(MemoryKind.DAILY, start, end))

            store.append(
                MemoryEntry.new(
                    kind=MemoryKind.DAILY,
                    period_start=start,
                    period_end=end,
                    summary="日总结",
                    tokens=200,
                )
            )

            self.assertTrue(store.has_for_period(MemoryKind.DAILY, start, end))

    def test_compressed_event_ids_collects_only_session_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            store.append(
                MemoryEntry.new(
                    kind=MemoryKind.SESSION,
                    period_start=datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc),
                    period_end=datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc),
                    summary="片段 A",
                    tokens=100,
                    source_event_ids=["e1", "e2"],
                )
            )
            store.append(
                MemoryEntry.new(
                    kind=MemoryKind.SESSION,
                    period_start=datetime(2026, 5, 23, 13, 0, tzinfo=timezone.utc),
                    period_end=datetime(2026, 5, 23, 15, 0, tzinfo=timezone.utc),
                    summary="片段 B",
                    tokens=150,
                    source_event_ids=["e3"],
                )
            )
            store.append(
                MemoryEntry.new(
                    kind=MemoryKind.DAILY,
                    period_start=datetime(2026, 5, 23, 0, 0, tzinfo=timezone.utc),
                    period_end=datetime(2026, 5, 23, 23, 59, tzinfo=timezone.utc),
                    summary="日总结",
                    tokens=400,
                    source_event_ids=["e1", "e2", "e3", "e4"],
                )
            )

            ids = store.compressed_event_ids()

        # daily 的 source_event_ids 不进入这个集合；只有 session 的事件被算作"已压缩"。
        self.assertEqual(ids, {"e1", "e2", "e3"})


if __name__ == "__main__":
    unittest.main()
