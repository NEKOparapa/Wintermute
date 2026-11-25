"""参考 pi agent-core 实现的有状态异步智能体循环。"""

import asyncio
import inspect
import logging
import math
from collections import deque
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from purin.agent.core.errors import AgentBusyError, MaxTurnsExceeded, ModelProtocolError
from purin.agent.core.tools import Tool, ToolContext
from purin.agent.core.types import (
    AgentContext,
    AgentEvent,
    AgentState,
    AssistantMessage,
    ContextTransform,
    EventListener,
    Message,
    Model,
    RunResult,
    RunStatus,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from purin.logging import log_context

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from purin.agent.context import (
        ContextCheckpoint,
        ContextManager,
        ContextState,
        ManagedContext,
    )

_MEMORY_TRUNCATION_NOTICE = (
    "\n[长期记忆已按上下文预算截断，请用 memory_read 读取完整文件。]"
    "\n[/长期记忆参考资料]"
)


@dataclass(frozen=True, slots=True)
class RunControl:
    """由调度器提供的持久化边界；回调同步执行，失败会终止运行。

    wake 是提示，has_inputs 判断是否有需要立即处理的输入。adopt 在每次模型请求前
    接纳新输入和此时可见的活动时间线，并提供该批对应的新参考资料。
    finish 原子地检查候选回答依赖的输入是否仍是最新，或要求 Runner 在此边界续跑。
    各回调收到的 transcript 仅包含本次运行新增消息。
    """

    wake: asyncio.Event
    has_inputs: Callable[[], bool]
    adopt: Callable[[tuple[Message, ...]], tuple[tuple[UserMessage, ...], tuple[UserMessage, ...]]]
    finish: Callable[[tuple[Message, ...], AssistantMessage], bool]
    declare_tools: Callable[[tuple[Message, ...], AssistantMessage], None]
    start_tool: Callable[[ToolCall], bool]
    end_tool: Callable[[ToolResultMessage], None]
    reply_task: Callable[[], UserMessage | None] | None = None
    # 只由返回完整有效响应的主模型推进；摘要器、已取消的流均不确认投递。
    model_read_count: int | None = None
    record_model_read: (
        Callable[[int, tuple[Message, ...], "ContextCheckpoint | None"], None] | None
    ) = None
    run_id: str | None = None
    send_replies: Callable[[ToolResultMessage, tuple[str, ...]], None] | None = None
    end_session: Callable[[tuple[ToolResultMessage, ...]], None] | None = None


class AgentRunner:
    """管理一个会话，并在同一事件循环中逐次执行任务。

    模型和工具通过异步实现注入，工具按声明顺序执行。
    引导消息在当前工具批次结束后注入；后续消息仅在智能体即将结束时逐条注入。
    订阅回调的异常会记录日志并隔离；回调应尽快返回，且不能等待 Runner 空闲。
    """

    def __init__(
        self,
        model: Model,
        *,
        system_prompt: str = "",
        tools: Sequence[Tool[Any]] = (),
        messages: Sequence[Message] = (),
        max_turns: int = 20,
        tool_timeout: float | None = None,
        model_timeout: float | None = None,
        transform_context: ContextTransform | None = None,
        context_manager: "ContextManager | None" = None,
    ) -> None:
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
            raise ValueError("max_turns must be a positive integer")
        for name, value in (("tool_timeout", tool_timeout), ("model_timeout", model_timeout)):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and positive")
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Tool names must be unique")
        self._model = model
        self._system_prompt = system_prompt
        self._run_system_prompt = system_prompt
        self._run_system_suffix = ""
        self._trim_system_prompt_suffix = False
        self._messages = list(deepcopy(messages))
        self._validate_history(self._messages)
        self._max_turns = max_turns
        self._tool_timeout = tool_timeout
        self._model_timeout = model_timeout
        self._transform_context = transform_context
        self._context_manager = context_manager
        self._references: tuple[UserMessage, ...] = ()
        self._record_history = True
        self._listeners: dict[object, tuple[EventListener, Callable[[], bool] | None]] = {}
        self._steering: deque[UserMessage] = deque()
        self._follow_ups: deque[UserMessage] = deque()
        self._active: asyncio.Task[RunResult] | None = None
        self._committed = False
        self._pending: set[str] = set()
        self._error: str | None = None
        self._turn = 0
        self._run_context: ContextVar[bool] = ContextVar(
            f"purin-runner-{id(self)}", default=False
        )

    @property
    def in_run_context(self) -> bool:
        """当前代码是否属于本 Runner 的运行或其派生任务，用于阻止等待自身。"""
        return self._run_context.get()

    @property
    def state(self) -> AgentState:
        """返回独立快照，调用方无法借此修改实时会话记录。"""
        return AgentState(
            messages=deepcopy(tuple(self._messages)),
            is_running=self._active is not None,
            pending_tool_calls=frozenset(self._pending),
            error=self._error,
            steering_count=len(self._steering),
            follow_up_count=len(self._follow_ups),
        )

    @property
    def context_state(self) -> "ContextState | None":
        """获取上下文管理快照；未配置管理器时返回 None。"""
        return deepcopy(self._context_manager.state) if self._context_manager is not None else None

    @property
    def context_checkpoint(self) -> "ContextCheckpoint | None":
        from purin.agent.context import SummaryContextManager

        manager = self._context_manager
        return manager.checkpoint if isinstance(manager, SummaryContextManager) else None

    def restore_context_checkpoint(self, checkpoint: "ContextCheckpoint") -> None:
        """从会话存储装入摘要，管理器在下一次 prepare 时校验真实历史。"""
        from purin.agent.context import SummaryContextManager

        self._ensure_idle()
        if isinstance(self._context_manager, SummaryContextManager):
            self._context_manager.restore_checkpoint(checkpoint)

    def subscribe(
        self, listener: EventListener, *, has_observers: Callable[[], bool] | None = None
    ) -> Callable[[], None]:
        """注册订阅并返回退订函数。转发器可提供当前是否有下游订阅的同步查询。

        查询仅用于标记实际播出的文本，各订阅照常接收全部事件。
        """
        key = object()
        self._listeners[key] = (listener, has_observers)

        def unsubscribe() -> None:
            self._listeners.pop(key, None)

        return unsubscribe

    async def run(
        self,
        prompt: str | UserMessage,
        *,
        references: Sequence[UserMessage] = (),
        record_history: bool = True,
    ) -> RunResult:
        """执行一轮任务；临时资料仅参与本轮模型输入与预算。

        record_history=False 时使用交流历史的副本，成功、失败或取消后均
        恢复原历史；执行记录仍通过返回值和事件提供。临时运行不消费排队
        的交流输入，调度方可在结束后继续处理它们。
        """
        return await self._start(
            self._user_message(prompt), references=references, record_history=record_history
        )

    async def run_controlled(
        self,
        prompt: str | UserMessage | None,
        *,
        references: Sequence[UserMessage] = (),
        control: RunControl,
        system_prompt_suffix: str = "",
        trim_system_prompt_suffix: bool = False,
    ) -> RunResult:
        """执行可接纳新事件的持久会话；None 表示触发输入已在恢复的历史中。

        附加系统提示在整次运行内生效，包括中途接纳输入后的请求。
        可选的记忆后缀裁剪逐轮服从上下文预算，不截断基础系统指令或会话输入。
        """
        if self._steering or self._follow_ups:
            raise AgentBusyError("Cannot mix queued SDK inputs with a controlled run")
        return await self._start(
            self._user_message(prompt) if prompt is not None else None,
            references=references,
            control=control,
            system_prompt_suffix=system_prompt_suffix,
            trim_system_prompt_suffix=trim_system_prompt_suffix,
        )

    async def continue_run(self) -> RunResult:
        """从用户消息或工具结果末尾重试，或在回答后处理排队的输入。"""
        self._ensure_idle()
        if self._steering or self._follow_ups:
            return await self._start(None)
        if not self._messages or isinstance(self._messages[-1], AssistantMessage):
            raise ValueError("Cannot continue without a user/tool tail or a queued message")
        return await self._start(None)

    def steer(self, message: str | UserMessage) -> None:
        """将用户输入排入队列，在当前工具批次后的模型轮次注入。"""
        self._steering.append(self._user_message(message))

    def follow_up(self, message: str | UserMessage) -> None:
        """将用户输入排入队列，等待当前工具循环自然结束后处理。"""
        self._follow_ups.append(self._user_message(message))

    def cancel(self) -> bool:
        """请求取消；等待运行协程或 wait_until_idle，以确保清理完成。"""
        if self._active is None or self._active.done() or self._committed:
            return False
        return self._active.cancel()

    async def wait_until_idle(self) -> None:
        """等待运行结束，不接管运行异常；取消等待者不会取消运行任务。"""
        task = self._active
        if task is None:
            return
        if task is asyncio.current_task() or self.in_run_context:
            raise RuntimeError("A runner callback cannot wait for its own run")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            # 运行异常由调用方处理，此方法只负责等待。
            pass

    def reset(self) -> None:
        """空闲时清空历史和队列，保留模型、工具与订阅回调。"""
        self._ensure_idle()
        if self._context_manager is not None:
            self._context_manager.reset()
        self._messages.clear()
        self._steering.clear()
        self._follow_ups.clear()
        self._pending.clear()
        self._error = None
        self._turn = 0

    def restore_messages(self, messages: Sequence[Message]) -> None:
        """空闲时恢复持久化历史，摘要缓存由管理器按前缀自行校验。

        排队输入尚未处理时拒绝替换，避免无意丢弃用户消息。
        """
        self._ensure_idle()
        if self._steering or self._follow_ups:
            raise AgentBusyError("Cannot restore history while input is queued")
        restored = list(deepcopy(messages))
        self._validate_history(restored)
        self._messages = restored
        self._pending.clear()
        self._error = None
        self._turn = 0

    def _ensure_idle(self) -> None:
        if self._active is not None:
            raise AgentBusyError("AgentRunner is running; use steer() or follow_up()")

    @staticmethod
    def _user_message(prompt: str | UserMessage) -> UserMessage:
        if isinstance(prompt, str):
            return UserMessage(prompt)
        if not isinstance(prompt, UserMessage):
            raise TypeError("Expected a string or UserMessage")
        return prompt

    async def _start(
        self,
        prompt: UserMessage | None,
        *,
        references: Sequence[UserMessage] = (),
        record_history: bool = True,
        control: RunControl | None = None,
        system_prompt_suffix: str = "",
        trim_system_prompt_suffix: bool = False,
    ) -> RunResult:
        self._ensure_idle()
        if not isinstance(record_history, bool):
            raise TypeError("record_history must be a bool")
        copied_references = deepcopy(tuple(references))
        if not all(isinstance(message, UserMessage) for message in copied_references):
            raise TypeError("References must contain only UserMessage values")
        original_messages = self._messages
        if not record_history:
            self._messages = deepcopy(original_messages)
        self._references = copied_references
        self._record_history = record_history
        self._error = None
        self._turn = 0
        try:
            self._run_system_suffix = system_prompt_suffix
            self._trim_system_prompt_suffix = trim_system_prompt_suffix
            self._run_system_prompt = "\n\n".join(
                filter(None, (self._system_prompt, system_prompt_suffix))
            )
            task = asyncio.create_task(self._execute(prompt, control), name="purin-agent-run")
            self._active = task
            return await task
        finally:
            if not record_history:
                self._messages = original_messages
            self._references = ()
            self._run_system_prompt = self._system_prompt
            self._run_system_suffix = ""
            self._trim_system_prompt_suffix = False
            self._record_history = True
            self._active = None
            self._committed = False
            self._pending.clear()

    async def _execute(self, prompt: UserMessage | None, control: RunControl | None) -> RunResult:
        with log_context(run_id=uuid4().hex):
            return await self._execute_run(prompt, control)

    async def _execute_run(
        self, prompt: UserMessage | None, control: RunControl | None
    ) -> RunResult:
        context_token = self._run_context.set(True)
        start = len(self._messages)
        status: RunStatus = "completed"
        try:
            # 立即执行的任务工厂必须等到 _active 赋值后才能进入回调。
            await asyncio.sleep(0)
            logger.info("Agent 运行开始", extra={"record_history": self._record_history})
            await self._emit(AgentEvent("agent_start"))
            await self._check_cancelled()
            if prompt is not None:
                await self._append(prompt)
            if control is not None:
                return await self._controlled_loop(control, start)
            # 排队的后续消息可以继续已有回答的会话。
            if (
                self._record_history
                and prompt is None
                and not self._steering
                and self._follow_ups
                and (not self._messages or isinstance(self._messages[-1], AssistantMessage))
            ):
                await self._append(self._follow_ups.popleft())
            while True:
                await self._check_cancelled()
                if self._turn >= self._max_turns:
                    raise MaxTurnsExceeded(f"Agent exceeded {self._max_turns} model turns")
                self._turn += 1
                await self._emit(AgentEvent("turn_start", self._turn))
                await self._check_cancelled()
                # 固定本次队列长度，避免订阅回调持续入队导致处理无法结束。
                for _ in range(len(self._steering) if self._record_history else 0):
                    await self._check_cancelled()
                    await self._append(self._steering.popleft())
                async with asyncio.timeout(self._model_timeout):
                    response, context = await self._call_model()
                outcome = await self._execute_tools(response, context)
                await self._emit(AgentEvent("turn_end", self._turn, message=response))
                await self._check_cancelled()
                if outcome == "ended":
                    return RunResult(deepcopy(tuple(self._messages[start:])), response, self._turn)
                if response.tool_calls or (self._record_history and self._steering):
                    continue
                if self._record_history and self._follow_ups:
                    # 剩余模型调用轮数不足时，不取出排队的输入。
                    if self._turn >= self._max_turns:
                        raise MaxTurnsExceeded(f"Agent exceeded {self._max_turns} model turns")
                    await self._append(self._follow_ups.popleft())
                    continue
                return RunResult(
                    deepcopy(tuple(self._messages[start:])), deepcopy(response), self._turn
                )
        except asyncio.CancelledError:
            # 候选回答已持久提交后，观察方的取消只影响等待，不改变运行终态。
            status = "completed" if self._committed else "cancelled"
            raise
        except Exception as exc:
            status = "error"
            logger.warning("Agent 运行失败", extra={"error_type": type(exc).__name__})
            self._error = f"{type(exc).__name__}: {exc}"
            await self._emit(AgentEvent("error", self._turn, error=self._error))
            raise
        finally:
            try:
                await self._emit(
                    AgentEvent("agent_end", self._turn, status=status, error=self._error)
                )
            finally:
                logger.info("Agent 运行结束", extra={"status": status, "turns": self._turn})
                self._run_context.reset(context_token)

    async def _controlled_loop(self, control: RunControl, start: int) -> RunResult:
        # 已完成的工具轮和被打断的模型尝试分开计数，避免频繁更新导致无限请求。
        completed = interruptions = 0
        read_count = start if control.model_read_count is None else control.model_read_count
        if not 0 <= read_count <= start:
            raise ValueError("Model read boundary is outside the restored session history")
        while completed < self._max_turns:
            await self._check_cancelled()
            incoming, references = control.adopt(tuple(self._messages[start:]))
            if incoming:
                self._references = tuple(references)
                # adopt 已提交全批次；先完整镜像内存历史，再调可取消的观察回调。
                self._messages.extend(incoming)
                for message in incoming:
                    await self._emit(AgentEvent("message_start", self._turn, message=message))
                    await self._emit(AgentEvent("message_end", self._turn, message=message))
            self._turn += 1
            await self._emit(AgentEvent("turn_start", self._turn))
            partial: list[str] = []
            async with asyncio.timeout(self._model_timeout):
                attempt = await self._race_model(control, partial, read_count)
            await self._check_cancelled()
            if attempt is not None:
                response, context = attempt
                transcript = tuple(self._messages[start:])
                read_count = len(self._messages)
                if control.record_model_read is not None:
                    control.record_model_read(read_count, transcript, self.context_checkpoint)
                if response.tool_calls:
                    control.declare_tools(transcript, response)
                    outcome = await self._execute_tools(response, context, control)
                    if outcome != "skipped":
                        completed += 1
                        await self._emit(AgentEvent("turn_end", self._turn, message=response))
                        if outcome == "ended":
                            return RunResult(
                                deepcopy(tuple(self._messages[start:])), response, self._turn
                            )
                        continue
                    # 已提交完整的调用声明与 skipped 结果，不能重复保存流式片段。
                    partial.clear()
                elif control.finish((*transcript, response), response):
                    self._committed = True
                    await self._append(response, started=True)
                    await self._emit(AgentEvent("turn_end", self._turn, message=response))
                    return RunResult(deepcopy(tuple(self._messages[start:])), response, self._turn)
            if partial:
                fragment = AssistantMessage("".join(partial) + "\n[本轮回答被新输入中断]")
                await self._append(fragment, started=True)
            await self._emit(AgentEvent("turn_interrupted", self._turn))
            interruptions += 1
            if interruptions > self._max_turns:
                raise MaxTurnsExceeded("Too many interrupted model requests in one run")
        raise MaxTurnsExceeded(f"Agent exceeded {self._max_turns} model turns")

    async def _race_model(
        self, control: RunControl, partial: list[str], read_count: int
    ) -> tuple[AssistantMessage, AgentContext] | None:
        """等待完整请求或持久新输入；无首 token 时同样可以终止当前网络请求。"""
        control.wake.clear()
        if control.has_inputs():
            return None
        reply_task = control.reply_task() if control.reply_task is not None else None
        if reply_task is not None and not isinstance(reply_task, UserMessage):
            raise TypeError("reply_task must supply a UserMessage or None")
        model = asyncio.create_task(
            self._call_model(partial, reply_task, read_count), name="purin-model-request"
        )
        wake = asyncio.create_task(control.wake.wait())
        try:
            while True:
                await asyncio.wait((model, wake), return_when=asyncio.FIRST_COMPLETED)
                if control.has_inputs():
                    return None
                if model.done():
                    return model.result()
                # 唤醒可能来自其他会话或重复输入；继续等待同一个模型请求。
                control.wake.clear()
                wake = asyncio.create_task(control.wake.wait())
        finally:
            for task in (model, wake):
                if not task.done() and not task.cancelling():
                    task.cancel()
            cleanup = asyncio.gather(model, wake, return_exceptions=True)
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    async def _call_model(
        self,
        partial: list[str] | None = None,
        reply_task: UserMessage | None = None,
        read_count: int | None = None,
    ) -> tuple[AssistantMessage, AgentContext]:
        await self._check_cancelled()
        messages = deepcopy(tuple(self._messages))
        unread: tuple[Message, ...] = ()
        if read_count is not None:
            # 最后生成的回答和已结算的工具结果可以进入旧历史摘要。一旦遇到未读输入，
            # 同批活动、通知以及其后所有消息须保持原文及顺序。
            for index in range(read_count, len(messages)):
                if isinstance(messages[index], UserMessage):
                    unread = messages[index:]
                    break
        if self._transform_context is not None:
            messages = tuple(await self._transform_context(messages))
            if unread and messages[-len(unread) :] != unread:
                raise ValueError("Context transform must preserve unread session inputs")
        self._validate_history(messages)
        context = AgentContext(
            self._run_system_prompt,
            deepcopy(messages),
            tuple(tool.definition for tool in self._tools.values()),
            deepcopy(self._references),
            reply_task,
        )
        if self._context_manager is not None:
            from purin.agent.context import SummaryContextManager

            protected_from = len(messages) - len(unread) if unread else None
            if isinstance(self._context_manager, SummaryContextManager):
                prepared = await self._prepare_with_memory_budget(context, protected_from)
            else:
                prepared = await self._context_manager.prepare(context)
            logger.debug(
                "上下文预算已计算",
                extra={
                    "context_tokens": prepared.estimated_tokens,
                    "context_limit": prepared.input_limit,
                    "compacted": prepared.compacted,
                },
            )
            context = deepcopy(prepared.context)
            if unread and context.messages[-len(unread) :] != unread:
                raise ValueError("Context manager must preserve unread session inputs")
            if context.reference_messages != self._references:
                raise ValueError("Context manager must preserve reference_messages")
            if context.reply_task != reply_task:
                raise ValueError("Context manager must preserve reply_task")
            self._validate_history(context.messages)
            await self._emit(
                AgentEvent(
                    "context_prepared",
                    self._turn,
                    context_tokens=prepared.estimated_tokens,
                    context_limit=prepared.input_limit,
                    compacted=prepared.compacted,
                )
            )
            await self._check_cancelled()
        model_input = context.for_model()
        self._validate_history(model_input.messages)
        await self._emit(AgentEvent("message_start", self._turn, message=AssistantMessage()))
        await self._check_cancelled()
        stream = self._model.stream(model_input)
        final: AssistantMessage | None = None
        try:
            async for event in stream:
                await self._check_cancelled()
                if final is not None:
                    raise ModelProtocolError("Model emitted events after its final message")
                if isinstance(event, TextDelta):
                    await self._emit(
                        AgentEvent("message_update", self._turn, delta=event.text), partial=partial
                    )
                elif isinstance(event, AssistantMessage):
                    final = deepcopy(event)
                else:
                    raise ModelProtocolError(f"Unexpected model event: {type(event).__name__}")
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        if final is None:
            raise ModelProtocolError("Model stream ended without a final message")
        await self._check_cancelled()
        if final.finish_reason == "length":
            raise ModelProtocolError("Model response was truncated; no tools were executed")
        self._validate_calls(final.tool_calls)
        if final.finish_reason == "tool_calls" and not final.tool_calls:
            raise ModelProtocolError("Model stopped for tool calls without supplying any")
        return final, context

    async def _prepare_with_memory_budget(
        self, context: AgentContext, protected_from: int | None
    ) -> "ManagedContext":
        """Only an opted-in, supplemental memory section may yield to required context."""
        from purin.agent.context import ContextLimitError, SummaryContextManager

        manager = self._context_manager
        assert isinstance(manager, SummaryContextManager)
        if not self._trim_system_prompt_suffix or not self._run_system_suffix:
            return await manager.prepare(context, protected_from=protected_from)
        counter = manager.token_counter
        limit = manager.budget.input_limit

        def count(candidate: AgentContext) -> int:
            result = counter.count(candidate.for_model())
            if isinstance(result, bool) or not isinstance(result, int) or result < 0:
                raise ValueError("Token counters must return nonnegative integers")
            return result

        if count(context) <= limit:
            try:
                return await manager.prepare(context, protected_from=protected_from)
            except ContextLimitError:
                # A restored summary and its protocol markers can be larger than the short
                # original prefix. Re-budget against the actual projection in that case too.
                pass

        def prompt(suffix: str) -> str:
            return "\n\n".join(filter(None, (self._system_prompt, suffix)))

        # Let the existing manager retain all unread/atomic messages and compact old history.
        # Including the whole notice here prevents a byte slice from hiding the truncation.
        baseline = replace(context, system_prompt=prompt(_MEMORY_TRUNCATION_NOTICE))
        try:
            prepared = await manager.prepare(baseline, protected_from=protected_from)
        except ContextLimitError:
            # If even the notice does not fit, preserve every required instruction/input.
            # A genuinely oversized base request still fails in the ordinary manager.
            logger.warning("长期记忆已省略：上下文没有空间容纳完整截断提示")
            return await manager.prepare(
                replace(context, system_prompt=self._system_prompt),
                protected_from=protected_from,
            )

        suffix = self._run_system_suffix
        full_prompt = prompt(suffix)
        if count(replace(prepared.context, system_prompt=full_prompt)) <= limit:
            selected = full_prompt
        else:
            low, high = 0, len(suffix)
            selected = prompt(_MEMORY_TRUNCATION_NOTICE)
            while low < high:
                middle = (low + high + 1) // 2
                candidate = prompt(suffix[:middle] + _MEMORY_TRUNCATION_NOTICE)
                if count(replace(prepared.context, system_prompt=candidate)) <= limit:
                    low, selected = middle, candidate
                else:
                    high = middle - 1
            logger.warning("长期记忆已按当前会话上下文预算截断")
        # Reuse the summary from baseline preparation and publish accurate final counts.
        final = await manager.prepare(
            replace(context, system_prompt=selected), protected_from=protected_from
        )
        return replace(final, compacted=prepared.compacted or final.compacted)

    async def _execute_tools(
        self, response: AssistantMessage, context: AgentContext, control: RunControl | None = None
    ) -> Literal["executed", "ended", "skipped"]:
        calls = response.tool_calls
        self._pending.update(call.id for call in calls)
        started_call: str | None = None
        batch_start = len(self._messages)
        skip_remaining = False
        any_started = False
        try:
            await self._append(response, started=True)
            await self._check_cancelled()
            for index, call in enumerate(calls):
                if control is not None:
                    skip_remaining = skip_remaining or control.has_inputs()
                if not skip_remaining:
                    logger.debug("工具执行开始", extra={"tool": call.name, "call_id": call.id})
                    await self._emit(AgentEvent("tool_execution_start", self._turn, tool_call=call))
                    await self._check_cancelled()
                    if control is not None:
                        skip_remaining = not control.start_tool(call)
                if skip_remaining:
                    result = ToolResult(
                        "Tool skipped before execution because new input arrived", True
                    )
                else:
                    any_started = True
                    started_call = call.id
                    tool_context = ToolContext(
                        call.id,
                        self._turn,
                        control.run_id if control else None,
                        self._result_fits(context, batch_start, calls[index:]),
                    )
                    result = await self._execute_tool(call, control, tool_context)
                if result.end_session and not result.is_error:
                    skipped = ToolResult("本轮已终止，此工具未执行。", True)
                    remaining = [(call, result), *((item, skipped) for item in calls[index + 1 :])]
                    if control is not None:
                        assert control.end_session is not None
                        control.end_session(tuple(self._tool_message(c, r) for c, r in remaining))
                    self._committed = True
                    await self._finish_tools(remaining)
                    return "ended"
                if control is not None:
                    message = self._tool_message(call, result)
                    if result.replies:
                        assert control.send_replies is not None
                        control.send_replies(message, result.replies)
                    else:
                        control.end_tool(message)
                await self._finish_tools(((call, result),))
                started_call = None
        finally:
            # 取消批次后，会话历史中不能留下缺少结果的工具调用。
            # 等待订阅回调前先补齐所有占位结果，保证再次取消时历史仍完整。
            unresolved: list[tuple[ToolCall, ToolResult]] = []
            for call in calls:
                if call.id not in self._pending:
                    continue
                if control is None:
                    detail = "Tool execution cancelled before a result was available"
                elif call.id == started_call:
                    detail = "Tool execution was interrupted; its effects are unknown"
                else:
                    detail = "Tool execution was interrupted before the tool was started"
                unresolved.append((call, ToolResult(detail, True)))
            await self._finish_tools(unresolved)
        return "executed" if any_started else "skipped"

    def _result_fits(
        self, context: AgentContext, batch_start: int, remaining: tuple[ToolCall, ...]
    ) -> Callable[[str], bool] | None:
        from purin.agent.context import SummaryContextManager

        manager = self._context_manager
        if not isinstance(manager, SummaryContextManager):
            return None

        def fits(content: str) -> bool:
            # Keep the prepared summary, references and reply task. Charge the assistant
            # declaration and earlier receipts once, and share space among pending calls.
            receipts = (ToolResultMessage(c.id, c.name, content) for c in remaining)
            messages = (*context.messages, *self._messages[batch_start:], *receipts)
            candidate = replace(context, messages=messages)
            return manager.token_counter.count(candidate.for_model()) <= manager.budget.input_limit

        return fits

    async def _execute_tool(
        self, call: ToolCall, control: RunControl | None, context: ToolContext
    ) -> ToolResult:
        await self._check_cancelled()
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(f"Unknown tool: {call.name}", True)
        try:
            parameters = tool.parameters.model_validate(deepcopy(call.arguments), strict=True)
            async with asyncio.timeout(self._tool_timeout):
                result = await tool.execute(parameters, context)
            if not isinstance(result, ToolResult):
                raise TypeError("Tools must return ToolResult")
            if result.replies and (control is None or control.send_replies is None):
                return ToolResult("当前运行不支持发送聊天消息。", True)
            if result.end_session and control is not None and control.end_session is None:
                return ToolResult("当前运行不支持静默结束。", True)
            return result
        except Exception as exc:
            # Validation and outer timeouts can fail before the tool returns its own receipt.
            kind = type(exc).__name__
            return context.error(f"{kind}: {exc}", kind)

    async def _finish_tools(self, results: Sequence[tuple[ToolCall, ToolResult]]) -> None:
        # Mirror every committed result before a listener can cancel the run.
        for call, result in results:
            self._messages.append(self._tool_message(call, result))
            self._pending.discard(call.id)
        for call, result in results:
            logger.debug(
                "工具执行结束",
                extra={"tool": call.name, "call_id": call.id, "is_error": result.is_error},
            )
            message = self._tool_message(call, result)
            await self._emit(
                AgentEvent("tool_execution_end", self._turn, tool_call=call, result=result)
            )
            await self._emit(AgentEvent("message_start", self._turn, message=message))
            await self._emit(AgentEvent("message_end", self._turn, message=message))

    @staticmethod
    async def _check_cancelled() -> None:
        # 等待立即完成的协程不会形成取消检查点。
        # 在继续调用模型或工具并产生副作用前，先检查 cancel() 请求。
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # 在进入清理阶段的等待前，先接收尚未抛出的 Task.cancel 取消异常。
            await asyncio.sleep(0)
            raise asyncio.CancelledError

    @staticmethod
    def _tool_message(call: ToolCall, result: ToolResult) -> ToolResultMessage:
        return ToolResultMessage(call.id, call.name, result.content, result.is_error)

    async def _append(self, message: Message, *, started: bool = False) -> None:
        self._messages.append(deepcopy(message))
        if not started:
            await self._emit(AgentEvent("message_start", self._turn, message=message))
        await self._emit(AgentEvent("message_end", self._turn, message=message))

    async def _emit(self, event: AgentEvent, *, partial: list[str] | None = None) -> None:
        for listener, has_observers in tuple(self._listeners.values()):
            try:
                if (
                    partial is not None
                    and event.delta
                    and (has_observers is None or has_observers())
                ):
                    # 回调可能在已经展示增量后被中断；交付第一个真实订阅者时即记录。
                    partial.append(event.delta)
                    partial = None
                outcome = listener(deepcopy(event))
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception as exc:
                # 订阅器的异常可能包含用户正文或工具结果，只记录错误类型。
                logger.error(
                    "Agent 事件订阅回调失败",
                    extra={"event_type": event.type, "error_type": type(exc).__name__},
                )

    @staticmethod
    def _validate_calls(calls: tuple[ToolCall, ...]) -> None:
        ids: set[str] = set()
        for call in calls:
            if not call.id or not call.name or call.id in ids:
                raise ModelProtocolError("Tool calls require nonempty, unique IDs and names")
            ids.add(call.id)

    @classmethod
    def _validate_history(cls, messages: Sequence[Message]) -> None:
        pending: dict[str, str] = {}
        for message in messages:
            if isinstance(message, ToolResultMessage):
                if pending.get(message.tool_call_id) != message.name:
                    raise ValueError("History contains an unmatched tool result")
                del pending[message.tool_call_id]
            else:
                if pending:
                    raise ValueError("History contains tool calls without results")
                if isinstance(message, AssistantMessage):
                    cls._validate_calls(message.tool_calls)
                    pending = {call.id: call.name for call in message.tool_calls}
                elif not isinstance(message, UserMessage):
                    raise TypeError("History contains an unsupported message type")
        if pending:
            raise ValueError("History ends with tool calls without results")
