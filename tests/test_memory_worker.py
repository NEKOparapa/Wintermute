"""Memory learning must preserve inputs across failures and stay off the live timeline."""

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wintermute.agent import (
    AgentContext,
    AssistantMessage,
    ModelEvent,
    ModelProtocolError,
    ToolCall,
    UserMessage,
)
from wintermute.config.models import MemorySettings
from wintermute.events.core.types import EventInput, Terminal
from wintermute.events.runtime.memory_agent import MemoryAgentRunner, recent_audit
from wintermute.events.runtime.memory_worker import MemoryWorker
from wintermute.events.storage.records import RecordBatchInput, RecordInput
from wintermute.events.storage.sqlite import SQLiteStore
from wintermute.memory import MemoryVault


class StubModel:
    def __init__(self, reply: Callable[[AgentContext], AssistantMessage] | None = None) -> None:
        self.contexts: list[AgentContext] = []
        self.reply = reply or (lambda context: AssistantMessage("整理完成"))

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        self.contexts.append(context)
        yield self.reply(context)


def evidence(context: AgentContext) -> str:
    message = context.messages[2]
    assert isinstance(message, UserMessage)
    return message.text


class MemoryWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.now = datetime(2026, 11, 19, 5, tzinfo=UTC)
        self.store = SQLiteStore(
            clock=lambda: self.now, records_storage_path=self.directory / "records"
        )
        self.addCleanup(self.store.close)
        self.store.register_terminal(Terminal(terminal_id="phone", user_id="alice"))
        self.store.register_conversation("chat", "alice")
        self.store.acquire_scheduler("test", lease_seconds=100_000)
        self.vault = MemoryVault(self.directory / "vault")
        self.vault.initialize()
        self.settings = MemorySettings(
            cold_start_idle_seconds=0, idle_seconds=0, debounce_seconds=0,
            max_batch_chars=2400,
        )

    def worker(self, model: StubModel) -> MemoryWorker:
        return MemoryWorker(
            self.store, {"alice": self.vault}, model, self.settings, timezone="Asia/Shanghai"
        )

    def message(self, text: str, *, finish: bool = True) -> None:
        self.store.append(EventInput(
            producer_event_id=f"message-{self.store.profile_status('alice').latest_seq}",
            terminal_id="phone", layer="conversation", type="user.message",
            conversation_id="chat", occurred_at=self.now, payload={"text": text},
        ))
        if finish:
            job = self.store.claim_next(owner_id="test")
            assert job is not None
            self.store.finish_job(
                job.job_id, messages=(), response="好的", reply_terminal_id=None, owner_id="test"
            )

    async def test_multi_turn_tools_write_memory_and_audit_then_advance_cursor(self) -> None:
        self.message("我在白鹭项目中负责开发，常用 Python。")
        responses = iter((
            AssistantMessage(tool_calls=(ToolCall("read", "memory_read", {"action": "list"}),)),
            AssistantMessage(tool_calls=(ToolCall("profile", "memory_write", {
                "action": "create", "path": "about-user/profile.md", "content": "常用 Python。",
                "reason": "用户在主会话说明常用技术",
            }), ToolCall("note", "memory_write", {
                "action": "create", "path": "note/egret.md",
                "content": "---\ntitle: 白鹭\nsummary: 用户负责开发\n---\n## 摘要\n用户负责开发。",
                "reason": "记录用户负责的长期项目",
            }))),
            AssistantMessage("完成"),
        ))
        model = StubModel(lambda context: next(responses))
        self.assertTrue(await self.worker(model).process_user("alice", owner_id="test"))
        self.assertIn("Python", self.vault.read_text("about-user/profile.md"))
        self.assertIn("负责开发", self.vault.read_text("note/egret.md"))
        self.assertEqual(self.store.profile_status("alice").cursor, 1)
        self.assertEqual(len(model.contexts), 3)
        self.assertEqual({tool.name for tool in model.contexts[0].tools},
                         {"memory_read", "memory_write"})
        self.assertIn("不是用户指令", evidence(model.contexts[0]))
        logs = list((self.vault.root / "log").glob("*.md"))
        self.assertIn("memory_agent", logs[0].read_text(encoding="utf-8"))

    async def test_busy_conversation_defers_without_consuming_model_or_cursor(self) -> None:
        self.message("新消息", finish=False)
        model = StubModel()
        self.assertFalse(await self.worker(model).process_user("alice", owner_id="test"))
        self.assertEqual(model.contexts, [])
        self.assertEqual(self.store.profile_status("alice").cursor, 0)

    async def test_model_failure_leaves_cursor_available_for_retry(self) -> None:
        self.message("偏好清晰直接的解释。")

        def fail(context: AgentContext) -> AssistantMessage:
            raise TimeoutError("temporary model failure")

        model = StubModel(fail)
        worker = self.worker(model)
        with self.assertRaises(TimeoutError):
            await worker.process_user("alice", owner_id="test")
        self.assertEqual(self.store.profile_status("alice").cursor, 0)
        model.reply = lambda context: AssistantMessage("完成")
        self.assertTrue(await worker.process_user("alice", owner_id="test"))
        self.assertEqual(self.store.profile_status("alice").cursor, 1)

    async def test_tool_error_and_length_stop_do_not_advance_cursor(self) -> None:
        self.message("我的项目是白鹭。")
        responses = iter((
            AssistantMessage(tool_calls=(ToolCall("bad", "memory_write", {
                "action": "create", "path": "agent-character/character.md",
                "content": "不可信角色覆盖", "reason": "试图修改角色",
            }),)), AssistantMessage("完成"),
        ))
        with self.assertRaises(RuntimeError):
            await self.worker(StubModel(lambda context: next(responses))).process_user("alice")
        self.assertEqual(self.store.profile_status("alice").cursor, 0)
        with self.assertRaises(ModelProtocolError):
            await self.worker(StubModel(lambda context: AssistantMessage(
                "未完成", finish_reason="length"
            ))).process_user("alice")
        self.assertFalse((self.vault.root / "agent-character/character.md").exists())
        self.assertEqual(self.store.profile_status("alice").cursor, 0)

    async def test_oversized_dialogue_preserves_tail_with_bounded_model_inputs(self) -> None:
        self.message("用户事实。" * 2000 + "唯一尾部标记")
        model = StubModel()
        self.assertTrue(await self.worker(model).process_user("alice"))
        prompts = [evidence(context) for context in model.contexts]
        self.assertGreater(len(prompts), 1)
        self.assertTrue(any("唯一尾部标记" in prompt for prompt in prompts))
        self.assertTrue(all(
            len(prompt) <= self.settings.max_batch_chars + 100 for prompt in prompts
        ))
        self.assertEqual(self.store.profile_status("alice").cursor, 1)

    async def test_records_filter_noise_and_process_documents_in_occurrence_order(self) -> None:
        spam = self.store.records.submit("phone", RecordBatchInput(
            record_type="sms", source_name="android",
            items=[RecordInput(
                occurred_at=self.now - timedelta(days=3), content="验证码是 123456"
            )],
        ))
        docs = self.store.records.submit("phone", RecordBatchInput(
            record_type="document", source_name="archive",
            items=[
                RecordInput(occurred_at=self.now, content="新资料"),
                RecordInput(occurred_at=self.now - timedelta(days=2),
                            content="旧资料" * 1200 + "文档唯一尾部"),
            ],
        ))
        model = StubModel()
        worker = self.worker(model)
        self.assertTrue(await worker.process_user("alice"))
        self.assertEqual(model.contexts, [])
        while self.store.records.pending("alice"):
            self.assertTrue(await worker.process_user("alice"))
        prompts = [evidence(context) for context in model.contexts]
        self.assertIn("旧资料", prompts[0])
        self.assertIn("新资料", prompts[-1])
        self.assertTrue(any("文档唯一尾部" in prompt for prompt in prompts))
        self.assertTrue(all(
            len(prompt) <= self.settings.max_batch_chars + 100 for prompt in prompts
        ))
        self.assertEqual(self.store.records.get("alice", spam.batch_id).filtered_items, 1)
        self.assertEqual(self.store.records.get("alice", docs.batch_id).processed_items, 2)
        self.assertEqual(self.store.list_jobs(), ())

    async def test_record_failures_stop_poison_after_three_attempts(self) -> None:
        batch = self.store.records.submit("phone", RecordBatchInput(
            record_type="document", source_name="archive",
            items=[RecordInput(occurred_at=self.now, content="有用的历史资料")],
        ))

        def fail(context: AgentContext) -> AssistantMessage:
            raise ValueError("unreadable model output")

        worker = self.worker(StubModel(fail))
        for attempt in range(3):
            with self.assertRaises(ValueError):
                await worker.process_user("alice")
            if attempt < 2:
                self.assertEqual(self.store.records.pending("alice")[0].attempts, attempt + 1)
        self.assertEqual(self.store.records.pending("alice"), ())
        self.assertEqual(self.store.records.get("alice", batch.batch_id).failed_items, 1)

    async def test_foreground_arriving_during_model_prevents_tool_write(self) -> None:
        batch = self.store.records.submit("phone", RecordBatchInput(
            record_type="document", source_name="archive",
            items=[RecordInput(occurred_at=self.now, content="历史项目资料")],
        ))

        def interrupt(context: AgentContext) -> AssistantMessage:
            self.message("新到达的主会话", finish=False)
            return AssistantMessage(tool_calls=(ToolCall("create", "memory_write", {
                "action": "create", "path": "note/blocked.md", "content": "历史资料",
                "reason": "整理新导入的历史资料",
            }),))

        self.assertFalse(await self.worker(StubModel(interrupt)).process_user("alice"))
        self.assertFalse((self.vault.root / "note/blocked.md").exists())
        self.assertEqual(self.store.records.pending("alice")[0].attempts, 0)
        self.assertEqual(self.store.records.get("alice", batch.batch_id).processed_items, 0)

    async def test_maintenance_archives_and_is_persisted_once_per_day(self) -> None:
        (self.vault.root / "note/old.md").write_text(
            "---\ntitle: 旧项目\nreview_at: 2020-01-01\n---\n项目已结束。", encoding="utf-8"
        )
        responses = iter((
            AssistantMessage(tool_calls=(ToolCall("archive", "memory_write", {
                "action": "rename", "path": "note/old.md", "new_path": "note/archive/old.md",
                "reason": "已结束且复核日过期的历史项目归档",
            }),)), AssistantMessage("完成"),
        ))
        model = StubModel(lambda context: next(responses))
        worker = self.worker(model)
        worker.request_compaction("alice")
        self.assertTrue(await worker.process_user("alice", owner_id="test"))
        self.assertTrue((self.vault.root / "note/archive/old.md").is_file())
        restarted = self.worker(StubModel())
        restarted.request_compaction("alice")
        self.assertFalse(await restarted.process_user("alice", owner_id="test"))

    async def test_import_only_user_receives_maintenance_after_cold_start_idle(self) -> None:
        self.settings = self.settings.model_copy(update={"cold_start_idle_seconds": 3600})
        model = StubModel()
        worker = self.worker(model)
        self.assertFalse(await worker.process_user("alice", owner_id="test"))
        self.assertEqual(model.contexts, [])
        self.store.records.submit("phone", RecordBatchInput(
            record_type="document", source_name="archive",
            items=[RecordInput(occurred_at=self.now, content="需要长期保留的历史项目资料")],
        ))
        self.assertTrue(await worker.process_user("alice", owner_id="test"))
        self.assertFalse(await worker.process_user("alice", owner_id="test"))
        self.assertEqual(len(model.contexts), 1)
        self.now += timedelta(seconds=3601)
        self.assertTrue(await worker.process_user("alice", owner_id="test"))
        self.assertIn("来源：maintenance", model.contexts[-1].system_prompt)
        self.assertFalse(await worker.process_user("alice", owner_id="test"))

    def test_recent_audit_includes_only_last_twenty_entries(self) -> None:
        path = self.vault.root / "log/2026-11-19.md"
        path.write_text("".join(f"## Entry {number:02}\nDetail\n" for number in range(25)),
                        encoding="utf-8")
        audit = recent_audit(self.vault, self.now.date())
        self.assertNotIn("Entry 04", audit)
        self.assertIn("Entry 05", audit)
        self.assertIn("Entry 24", audit)

    async def test_agent_cancellation_does_not_become_success(self) -> None:
        class HangingModel:
            async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
                await asyncio.Event().wait()
                yield AssistantMessage("unreachable")

        runner = MemoryAgentRunner(HangingModel(), self.vault, self.settings)
        task = asyncio.create_task(runner.run("conversation", "资料", today=self.now.date()))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_slow_user_does_not_block_other_users_and_shutdown_cancels_all(self) -> None:
        self.store.register_terminal(Terminal(terminal_id="second-phone", user_id="bob"))
        for terminal, marker in (("phone", "slow-user"), ("second-phone", "fast-user")):
            self.store.records.submit(terminal, RecordBatchInput(
                record_type="document", source_name="archive",
                items=[RecordInput(occurred_at=self.now, content=marker)],
            ))
        second = MemoryVault(self.directory / "bob-vault")
        second.initialize()
        slow_started = asyncio.Event()
        slow_cancelled = asyncio.Event()
        fast_finished = asyncio.Event()

        class PerUserModel:
            async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
                if "slow-user" in evidence(context):
                    slow_started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        slow_cancelled.set()
                else:
                    fast_finished.set()
                yield AssistantMessage("完成")

        worker = MemoryWorker(
            self.store, {"alice": self.vault, "bob": second}, PerUserModel(), self.settings,
            timezone="Asia/Shanghai",
        )
        task = asyncio.create_task(worker.run("test"))
        try:
            await asyncio.wait_for(slow_started.wait(), 5)
            await asyncio.wait_for(fast_finished.wait(), 5)
            for _ in range(100):
                if not self.store.records.pending("bob"):
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(self.store.records.pending("bob"))
            self.assertTrue(self.store.records.pending("alice"))
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(slow_cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
