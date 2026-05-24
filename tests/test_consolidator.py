from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.memory.consolidator import Consolidator
from app.memory.memory import MemoryKind
from app.memory.tokens import TokenCounter


class FakeLLM:
    """记录调用并返回预设文本的 LLM 替身。"""

    def __init__(self, response: str = "压缩后的要点。") -> None:
        self.response = response
        self.calls: list[dict] = []

    def complete(self, *, system: str, messages: list[dict[str, str]]) -> str:
        self.calls.append({"system": system, "messages": messages})
        return self.response


class FailingLLM:
    """每次调用都抛错的 LLM 替身。"""

    def complete(self, *, system: str, messages: list[dict[str, str]]) -> str:
        raise RuntimeError("LLM unavailable")


def _event(event_id: str, role: str, content: str) -> dict[str, object]:
    """构造测试事件。"""
    event_type = "user_message" if role == "user" else "assistant_response"
    return {
        "id": event_id,
        "timestamp": "2026-05-23T10:00:00+08:00",
        "source": role,
        "type": event_type,
        "content": content,
        "metadata": {},
    }


class ConsolidatorTests(unittest.TestCase):
    def test_compress_to_session_creates_memory_entry_with_metadata(self) -> None:
        llm = FakeLLM(response="用户在准备周末爬山。")
        consolidator = Consolidator(llm, TokenCounter())

        events = [
            _event("e1", "user", "周末想去爬山"),
            _event("e2", "assistant", "推荐去香山。"),
            _event("e3", "user", "好，定下来。"),
        ]
        start = datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 23, 11, 0, tzinfo=timezone.utc)

        entry = consolidator.compress_to_session(
            events, period_start=start, period_end=end
        )

        self.assertEqual(entry.kind, MemoryKind.SESSION)
        self.assertEqual(entry.summary, "用户在准备周末爬山。")
        self.assertEqual(entry.source_event_ids, ["e1", "e2", "e3"])
        self.assertEqual(entry.period_start, start)
        self.assertEqual(entry.period_end, end)
        self.assertGreater(entry.tokens, 0)

    def test_dialogue_text_includes_user_and_assistant_blocks(self) -> None:
        llm = FakeLLM()
        Consolidator(llm, TokenCounter()).compress_to_session(
            [
                _event("e1", "user", "hello"),
                _event("e2", "assistant", "world"),
            ],
            period_start=datetime(2026, 5, 23, 10, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 23, 11, tzinfo=timezone.utc),
        )

        sent_user_message = llm.calls[0]["messages"][0]["content"]
        self.assertIn("[user]", sent_user_message)
        self.assertIn("hello", sent_user_message)
        self.assertIn("[assistant]", sent_user_message)
        self.assertIn("world", sent_user_message)

    def test_empty_events_raises(self) -> None:
        consolidator = Consolidator(FakeLLM(), TokenCounter())
        with self.assertRaises(ValueError):
            consolidator.compress_to_session(
                [],
                period_start=datetime(2026, 5, 23, 10, tzinfo=timezone.utc),
                period_end=datetime(2026, 5, 23, 11, tzinfo=timezone.utc),
            )

    def test_empty_summary_from_llm_raises(self) -> None:
        consolidator = Consolidator(FakeLLM(response="   "), TokenCounter())
        with self.assertRaises(ValueError):
            consolidator.compress_to_session(
                [_event("e1", "user", "ping")],
                period_start=datetime(2026, 5, 23, 10, tzinfo=timezone.utc),
                period_end=datetime(2026, 5, 23, 11, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
