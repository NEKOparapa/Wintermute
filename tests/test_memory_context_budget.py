"""Optional memory must not crowd out required instructions, inputs or tool boundaries."""

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from wintermute.agent import (
    AgentContext,
    AgentRunner,
    AssistantMessage,
    ModelEvent,
    RunControl,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from wintermute.agent.context import (
    ContextBudget,
    ContextCheckpoint,
    ContextLimitError,
    EstimatedTokenCounter,
    MainContextManager,
    SummaryRequest,
)
from wintermute.agent.context.types import history_digest
from wintermute.memory import MemoryVault, build_memory_prompt, create_memory_tools


class Summary:
    async def summarize(self, request: SummaryRequest) -> str:
        return "Previous conversation."


class CheckingModel:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.contexts: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        actual = EstimatedTokenCounter().count(context)
        if actual > self.limit:
            raise AssertionError(f"Model received {actual} bytes with budget {self.limit}")
        self.contexts.append(context)
        yield AssistantMessage("Done")


def control(
    *,
    adopt: Callable | None = None,
    finish: Callable | None = None,
    read_count: int | None = None,
) -> RunControl:
    return RunControl(
        wake=asyncio.Event(),
        has_inputs=lambda: False,
        adopt=adopt or (lambda transcript: ((), ())),
        finish=finish or (lambda transcript, response: True),
        declare_tools=lambda transcript, batch: None,
        start_tool=lambda call: True,
        end_tool=lambda result: None,
        model_read_count=read_count,
    )


class MemoryContextBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.budget = ContextBudget(
            context_window=16000,
            reserve_tokens=2048,
            keep_recent_tokens=4096,
            max_summary_tokens=1024,
        )
        self.base = "基础指令必须完整保留，不可由记忆替换。"
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.vault = MemoryVault(Path(self.directory.name) / "memory")
        self.vault.initialize()

    def runner(self, *, messages=(), tools=()):
        model = CheckingModel(self.budget.input_limit)
        manager = MainContextManager(self.budget, Summary())
        runner = AgentRunner(
            model,
            system_prompt=self.base,
            messages=messages,
            tools=tools,
            context_manager=manager,
        )
        return runner, model, manager

    async def test_16k_profile_is_trimmed_against_tools_references_and_json_bytes(self) -> None:
        # Quotes, backslashes and newlines consume more wire bytes than their raw string size.
        await self.vault.write(
            "create",
            "about-user/profile.md",
            actor="human_api",
            reason="初始化测试档案",
            content="# 用户资料\n" + '中文偏好"\\\n' * 2500,
        )
        suffix = build_memory_prompt(self.vault).text
        runner, model, manager = self.runner(
            tools=create_memory_tools(self.vault, actor="main_agent")
        )
        question = "请保留这条完整问题。"
        reference = UserMessage("必要参考资料：" + "r" * 1000)
        await runner.run_controlled(
            question,
            references=(reference,),
            control=control(),
            system_prompt_suffix=suffix,
            trim_system_prompt_suffix=True,
        )
        context = model.contexts[0]
        self.assertTrue(context.system_prompt.startswith(self.base + "\n\n"))
        self.assertIn("memory_read 读取完整文件", context.system_prompt)
        self.assertTrue(context.system_prompt.endswith("[/长期记忆参考资料]"))
        self.assertLess(len(context.system_prompt), len(self.base + suffix))
        self.assertEqual(context.messages[-1], UserMessage(question))
        self.assertIn(reference.text, context.messages[0].text)
        self.assertEqual(manager.state.last_input_tokens, EstimatedTokenCounter().count(context))

    async def test_opt_out_suffix_and_base_instruction_are_never_truncated(self) -> None:
        runner, model, _ = self.runner()
        with self.assertRaises(ContextLimitError):
            await runner.run_controlled(
                "question", control=control(), system_prompt_suffix="mandatory" * 3000
            )
        self.assertEqual(model.contexts, [])
        model = CheckingModel(self.budget.input_limit)
        runner = AgentRunner(
            model,
            system_prompt="mandatory base" * 2000,
            context_manager=MainContextManager(self.budget, Summary()),
        )
        with self.assertRaises(ContextLimitError):
            await runner.run_controlled(
                "question",
                control=control(),
                system_prompt_suffix="optional",
                trim_system_prompt_suffix=True,
            )
        self.assertEqual(model.contexts, [])

    async def test_rebudgets_each_turn_and_restores_suffix_when_references_shrink(self) -> None:
        runner, model, _ = self.runner()
        rounds = 0

        def adopt(transcript):
            nonlocal rounds
            rounds += 1
            reference = (UserMessage("动态资料" + "x" * 7000),) if rounds == 2 else ()
            return (UserMessage(f"完整用户问题{rounds}"),), reference

        await runner.run_controlled(
            None,
            control=control(adopt=adopt, finish=lambda transcript, response: rounds == 3),
            system_prompt_suffix="记忆" * 6000,
            trim_system_prompt_suffix=True,
        )
        first, second, third = model.contexts
        self.assertLess(len(second.system_prompt), len(first.system_prompt) - 1000)
        self.assertGreater(len(third.system_prompt), len(second.system_prompt) + 1000)
        for index, context in enumerate(model.contexts, 1):
            self.assertEqual(context.messages[-1], UserMessage(f"完整用户问题{index}"))
            self.assertTrue(context.system_prompt.startswith(self.base))

    async def test_unread_inputs_and_atomic_tool_result_remain_complete(self) -> None:
        question = UserMessage("保留用户原话" + "u" * 1500)
        call = AssistantMessage(
            tool_calls=(ToolCall("lookup", "memory_read", {"action": "list"}),),
            finish_reason="tool_calls",
        )
        result = ToolResultMessage("lookup", "memory_read", "工具结果" + "v" * 3000)
        runner, model, _ = self.runner(messages=(question, call, result))
        await runner.run_controlled(
            None,
            control=control(read_count=0),
            system_prompt_suffix="m" * 20000,
            trim_system_prompt_suffix=True,
        )
        self.assertEqual(model.contexts[0].messages, (question, call, result))
        self.assertIn("memory_read 读取完整文件", model.contexts[0].system_prompt)

    async def test_no_space_for_notice_keeps_base_and_required_input(self) -> None:
        runner, model, _ = self.runner()
        empty = AgentContext(self.base, (UserMessage(""),), ())
        available = self.budget.input_limit - EstimatedTokenCounter().count(empty) - 5
        message = UserMessage("x" * available)
        await runner.run_controlled(
            message,
            control=control(),
            system_prompt_suffix="optional memory" * 2000,
            trim_system_prompt_suffix=True,
        )
        self.assertEqual(model.contexts[0].system_prompt, self.base)
        self.assertEqual(model.contexts[0].messages, (message,))

    async def test_small_suffix_stays_intact_and_does_not_leak_to_next_run(self) -> None:
        runner, model, _ = self.runner()
        await runner.run_controlled(
            "first",
            control=control(),
            system_prompt_suffix="short memory",
            trim_system_prompt_suffix=True,
        )
        self.assertEqual(model.contexts[0].system_prompt, self.base + "\n\nshort memory")
        await runner.run("second")
        self.assertEqual(model.contexts[1].system_prompt, self.base)

    async def test_cached_summary_markers_are_part_of_the_actual_budget(self) -> None:
        old, current = UserMessage("old"), UserMessage("current")
        runner, model, manager = self.runner(messages=(old, current))
        manager.restore_checkpoint(
            ContextCheckpoint(
                summary="Earlier context",
                summarized_messages=1,
                prefix_digest=history_digest((old,)),
                compactions=1,
            )
        )
        base_context = AgentContext(self.base + "\n\n", (old, current), ())
        available = self.budget.input_limit - EstimatedTokenCounter().count(base_context) - 5
        await runner.run_controlled(
            None,
            control=control(read_count=1),
            system_prompt_suffix="m" * available,
            trim_system_prompt_suffix=True,
        )
        self.assertEqual(model.contexts[0].messages[-1], current)
        self.assertIn("Earlier context", model.contexts[0].messages[0].text)
        self.assertIn("memory_read 读取完整文件", model.contexts[0].system_prompt)
