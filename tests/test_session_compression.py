from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.dialogue.dialogue import DialogueService
from app.event.event import normalize_message_event
from app.memory.consolidator import Consolidator
from app.memory.memory import MemoryKind, MemoryStore
from app.memory.tokens import TokenCounter
from app.storage.storage import GlobalEventStore


class FakeChatLLM:
    """每次都返回固定回复的 LLM,用于 dialogue 主流程。"""

    def __init__(self, response: str = "好的。") -> None:
        self.response = response

    def complete(self, *, system: str, messages: list[dict[str, str]]) -> str:
        return self.response


class FakeCompressLLM:
    """专门给 Consolidator 用的 LLM,记录是否被调用。"""

    def __init__(self, response: str = "压缩后的对话要点。") -> None:
        self.response = response
        self.call_count = 0

    def complete(self, *, system: str, messages: list[dict[str, str]]) -> str:
        self.call_count += 1
        return self.response


class FailingCompressLLM:
    """压缩时总是抛错,用于验证容错路径。"""

    def complete(self, *, system: str, messages: list[dict[str, str]]) -> str:
        raise RuntimeError("LLM down")


def _build_service(
    tmp: str,
    *,
    chat_llm,
    compress_llm,
    trigger_tokens: int,
    recent_rounds: int = 5,
) -> tuple[DialogueService, GlobalEventStore, MemoryStore]:
    """组装一个隔离的 DialogueService,可控制阈值和 LLM 替身。"""
    events_store = GlobalEventStore(Path(tmp))
    memory_store = MemoryStore(Path(tmp))
    counter = TokenCounter()
    consolidator = Consolidator(compress_llm, counter)
    service = DialogueService(
        events_store,
        chat_llm,
        memory_store,
        consolidator,
        counter,
        recent_rounds=recent_rounds,
        session_compress_trigger_tokens=trigger_tokens,
    )
    return service, events_store, memory_store


class SessionCompressionTests(unittest.TestCase):
    def test_no_compression_when_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            compress_llm = FakeCompressLLM()
            service, _, memory_store = _build_service(
                tmp,
                chat_llm=FakeChatLLM(),
                compress_llm=compress_llm,
                trigger_tokens=10_000,  # 远高于实际,永远不会触发
                recent_rounds=2,
            )

            for index in range(4):
                service.handle_event(normalize_message_event(f"消息 {index}"))

            self.assertEqual(compress_llm.call_count, 0)
            self.assertEqual(memory_store.load_by_kind(MemoryKind.SESSION), [])

    def test_compression_triggers_when_old_events_exceed_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            compress_llm = FakeCompressLLM()
            # 把阈值设得足够低,几条消息就能触发。
            service, events_store, memory_store = _build_service(
                tmp,
                chat_llm=FakeChatLLM(),
                compress_llm=compress_llm,
                trigger_tokens=20,
                recent_rounds=2,
            )

            for index in range(6):
                service.handle_event(
                    normalize_message_event(
                        f"用户的第{index}条消息内容用来制造足够的 token"
                    )
                )

            session_memories = memory_store.load_by_kind(MemoryKind.SESSION)

        self.assertGreaterEqual(compress_llm.call_count, 1)
        self.assertGreaterEqual(len(session_memories), 1)
        first = session_memories[0]
        self.assertEqual(first.summary, "压缩后的对话要点。")
        self.assertGreater(len(first.source_event_ids), 0)

    def test_compressed_events_excluded_from_next_prompt(self) -> None:
        captured: list[dict] = []

        class CapturingLLM:
            def complete(self_inner, *, system, messages):
                captured.append({"system": system, "messages": list(messages)})
                return "已收到。"

        with tempfile.TemporaryDirectory() as tmp:
            compress_llm = FakeCompressLLM()
            service, _, memory_store = _build_service(
                tmp,
                chat_llm=CapturingLLM(),
                compress_llm=compress_llm,
                trigger_tokens=20,
                recent_rounds=2,
            )

            for index in range(6):
                service.handle_event(
                    normalize_message_event(
                        f"消息内容{index}加上更多文字来触发压缩"
                    )
                )

            self.assertGreaterEqual(compress_llm.call_count, 1)
            last_messages = captured[-1]["messages"]
            user_messages = [m for m in last_messages if m["role"] == "user"]

            # recent_rounds=2 → 最多两条 user 消息;旧消息已经被压缩走。
            self.assertLessEqual(len(user_messages), 2)
            # 长期记忆段进入 system prompt。
            self.assertIn("<long_term_memory>", captured[-1]["system"])

    def test_compression_failure_does_not_break_dialogue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _, memory_store = _build_service(
                tmp,
                chat_llm=FakeChatLLM("没事。"),
                compress_llm=FailingCompressLLM(),
                trigger_tokens=20,
                recent_rounds=2,
            )

            # 即使压缩 LLM 抛错,handle_event 也应该正常返回结果。
            for index in range(6):
                result = service.handle_event(
                    normalize_message_event(f"消息内容{index}加点字数")
                )
                self.assertEqual(result.message, "没事。")

            # 没有写入 session 记忆。
            self.assertEqual(memory_store.load_by_kind(MemoryKind.SESSION), [])


if __name__ == "__main__":
    unittest.main()
