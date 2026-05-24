from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.memory.memory import MemoryEntry, MemoryKind, MemoryStore
from app.prompt.prompt import SYSTEM_PROMPT_BASE, build_messages


def _event(event_id: str, role: str, content: str) -> dict[str, object]:
    """构造测试用事件字典。"""
    event_type = "user_message" if role == "user" else "assistant_response"
    return {
        "id": event_id,
        "timestamp": "2026-05-23T10:00:00+08:00",
        "source": role,
        "type": event_type,
        "content": content,
        "metadata": {},
    }


class PromptWithMemoryTests(unittest.TestCase):
    def test_empty_memory_returns_base_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))

            prompt = build_messages(
                events=[
                    _event("e1", "user", "你好"),
                    _event("e2", "assistant", "你好。"),
                ],
                memory_store=store,
            )

        self.assertEqual(prompt.system, SYSTEM_PROMPT_BASE)
        self.assertEqual(
            prompt.messages,
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好。"},
            ],
        )

    def test_memory_block_injected_into_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            store.append(
                MemoryEntry.new(
                    kind=MemoryKind.DAILY,
                    period_start=datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc),
                    period_end=datetime(2026, 5, 22, 23, 59, tzinfo=timezone.utc),
                    summary="昨天讨论了健身计划",
                    tokens=50,
                )
            )

            prompt = build_messages(
                events=[_event("e1", "user", "今天怎么样？")],
                memory_store=store,
            )

        self.assertIn("<long_term_memory>", prompt.system)
        self.assertIn("[日] 2026-05-22", prompt.system)
        self.assertIn("昨天讨论了健身计划", prompt.system)
        self.assertIn("</long_term_memory>", prompt.system)

    def test_daily_entry_is_dropped_when_covered_by_weekly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            # 周覆盖 2026-05-18 ~ 2026-05-24（2026-W21）。
            store.append(
                MemoryEntry.new(
                    kind=MemoryKind.WEEKLY,
                    period_start=datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc),
                    period_end=datetime(2026, 5, 24, 23, 59, tzinfo=timezone.utc),
                    summary="本周整体汇总",
                    tokens=100,
                )
            )
            # 这一天落在上面那一周内，应该被剔除掉。
            store.append(
                MemoryEntry.new(
                    kind=MemoryKind.DAILY,
                    period_start=datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc),
                    period_end=datetime(2026, 5, 22, 23, 59, tzinfo=timezone.utc),
                    summary="2026-05-22 内容（应该被周覆盖）",
                    tokens=80,
                )
            )

            prompt = build_messages(
                events=[_event("e1", "user", "你好")],
                memory_store=store,
            )

        self.assertIn("本周整体汇总", prompt.system)
        self.assertNotIn("应该被周覆盖", prompt.system)

    def test_compressed_events_excluded_from_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            store.append(
                MemoryEntry.new(
                    kind=MemoryKind.SESSION,
                    period_start=datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc),
                    period_end=datetime(2026, 5, 23, 11, 0, tzinfo=timezone.utc),
                    summary="上午片段已压缩",
                    tokens=40,
                    source_event_ids=["old-1", "old-2"],
                )
            )

            prompt = build_messages(
                events=[
                    _event("old-1", "user", "旧消息会被剔除"),
                    _event("old-2", "assistant", "旧回复也会被剔除"),
                    _event("new-1", "user", "新消息留着"),
                ],
                memory_store=store,
            )

        self.assertEqual(
            prompt.messages,
            [{"role": "user", "content": "新消息留着"}],
        )

    def test_recent_rounds_truncates_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))

            events = []
            for i in range(8):
                events.append(_event(f"u{i}", "user", f"用户第{i}轮"))
                events.append(_event(f"a{i}", "assistant", f"助手第{i}轮"))

            prompt = build_messages(
                events=events,
                memory_store=store,
                recent_rounds=3,
            )

        # 期望只保留最近 3 个 user 轮，每轮 user+assistant 共 6 条消息。
        self.assertEqual(len(prompt.messages), 6)
        self.assertEqual(prompt.messages[0], {"role": "user", "content": "用户第5轮"})
        self.assertEqual(prompt.messages[-1], {"role": "assistant", "content": "助手第7轮"})


if __name__ == "__main__":
    unittest.main()
