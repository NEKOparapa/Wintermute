from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from app.storage.storage import GlobalEventStore


class GlobalEventStoreTests(unittest.TestCase):
    """覆盖按日期分文件的事件存储行为。"""

    def test_append_writes_to_today_file_under_events_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GlobalEventStore(Path(tmp))
            store.append_event(source="user", type="user_message", content="你好")
            store.append_event(
                source="assistant",
                type="assistant_response",
                content="你好。",
            )

            today = date.today().isoformat()
            today_path = Path(tmp) / "events" / f"{today}.json"
            self.assertTrue(today_path.exists())
            raw = json.loads(today_path.read_text(encoding="utf-8"))

        self.assertEqual(len(raw), 2)
        self.assertEqual(raw[0]["type"], "user_message")
        self.assertEqual(raw[1]["type"], "assistant_response")
        self.assertIn("id", raw[0])
        self.assertIn("timestamp", raw[0])

    def test_load_events_merges_all_date_files_in_chronological_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            events_dir.mkdir()
            # 手动写两天的文件，模拟跨日的历史。
            (events_dir / "2026-05-22.json").write_text(
                json.dumps(
                    [
                        _make_event(
                            "id-1",
                            "2026-05-22T10:00:00+08:00",
                            "前天上午",
                        )
                    ]
                ),
                encoding="utf-8",
            )
            (events_dir / "2026-05-23.json").write_text(
                json.dumps(
                    [
                        _make_event(
                            "id-2",
                            "2026-05-23T08:00:00+08:00",
                            "昨天清早",
                        ),
                        _make_event(
                            "id-3",
                            "2026-05-23T22:00:00+08:00",
                            "昨晚",
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            store = GlobalEventStore(Path(tmp))
            events = store.load_events()
            day_events = store.load_events_for_date(date(2026, 5, 23))
            range_events = store.load_events_in_range(
                date(2026, 5, 22),
                date(2026, 5, 23),
            )
            dates = store.list_event_dates()

        self.assertEqual(
            [event["content"] for event in events],
            ["前天上午", "昨天清早", "昨晚"],
        )
        self.assertEqual([event["content"] for event in day_events], ["昨天清早", "昨晚"])
        self.assertEqual(
            [event["content"] for event in range_events],
            ["前天上午", "昨天清早", "昨晚"],
        )
        self.assertEqual(dates, [date(2026, 5, 22), date(2026, 5, 23)])

    def test_append_assigns_unique_ids_and_isoformat_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GlobalEventStore(Path(tmp))
            event = store.append_event(
                source="user",
                type="user_message",
                content="测试",
            )

        # 时间戳应该可以被 datetime 解析回去。
        parsed = datetime.fromisoformat(event["timestamp"])
        self.assertIsNotNone(parsed.tzinfo)
        self.assertTrue(event["id"])


def _make_event(event_id: str, timestamp: str, content: str) -> dict[str, object]:
    """构造测试用的事件字典。"""
    return {
        "id": event_id,
        "timestamp": timestamp,
        "source": "user",
        "type": "user_message",
        "content": content,
        "metadata": {},
    }


if __name__ == "__main__":
    unittest.main()
