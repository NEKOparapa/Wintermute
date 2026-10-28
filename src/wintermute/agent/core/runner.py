"""参考 pi agent-core 实现的有状态异步智能体循环。"""

import asyncio
import inspect
import logging
import math
from collections import deque
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from wintermute.agent.core.errors import AgentBusyError, MaxTurnsExceeded, ModelProtocolError
from wintermute.agent.core.tools import Tool, ToolContext
from wintermute.agent.core.types import (
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
from wintermute.logging import log_context

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from wintermute.agent.context import ContextManager, ContextState


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
        self._messages = list(deepcopy(messages))
        self._validate_history(self._messages)
        self._max_turns = max_turns
        self._tool_timeout = tool_timeout
        self._model_timeout = model_timeout
        self._transform_context = transform_context
        self._context_manager = context_manager
        self._references: tuple[UserMessage, ...] = ()
        self._record_history = True
        self._listeners: list[EventListener] = []
        self._steering: deque[UserMessage] = deque()
        self._follow_ups: deque[UserMessage] = deque()
        self._active: asyncio.Task[RunResult] | None = None
        self._pending: set[str] = set()
        self._error: str | None = None
        self._turn = 0
        self._run_context: ContextVar[bool] = ContextVar(
            f"wintermute-runner-{id(self)}", default=False
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

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """注册同步或异步订阅回调，返回可重复调用的取消订阅函数。"""
        self._listeners.append(listener)
        removed = False

        def unsubscribe() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            if listener in self._listeners:
                self._listeners.remove(listener)

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
        if self._active is None or self._active.done():
            return False
        return self._active.cancel()

    async def wait_until_idle(self) -> None:
        """等待运行结束，不接管运行异常；取消等待者不会取消运行任务。"""
        task = self._active
        if task is None:
            return
        if task is asyncio.current_task():
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
            task = asyncio.create_task(self._execute(prompt), name="wintermute-agent-run")
            self._active = task
            return await task
        finally:
            if not record_history:
                self._messages = original_messages
            self._references = ()
            self._record_history = True
            self._active = None
            self._pending.clear()

    async def _execute(self, prompt: UserMessage | None) -> RunResult:
        with log_context(run_id=uuid4().hex):
            return await self._execute_run(prompt)

    async def _execute_run(self, prompt: UserMessage | None) -> RunResult:
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
                    response = await self._call_model()
                await self._execute_tools(response)
                await self._emit(AgentEvent("turn_end", self._turn, message=response))
                await self._check_cancelled()
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
            status = "cancelled"
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

    async def _call_model(self) -> AssistantMessage:
        await self._check_cancelled()
        messages = deepcopy(tuple(self._messages))
        if self._transform_context is not None:
            messages = tuple(await self._transform_context(messages))
        self._validate_history(messages)
        context = AgentContext(
            self._system_prompt,
            deepcopy(messages),
            tuple(tool.definition for tool in self._tools.values()),
            deepcopy(self._references),
        )
        if self._context_manager is not None:
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
            if context.reference_messages != self._references:
                raise ValueError("Context manager must preserve reference_messages")
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
        context = context.for_model()
        self._validate_history(context.messages)
        await self._emit(AgentEvent("message_start", self._turn, message=AssistantMessage()))
        await self._check_cancelled()
        stream = self._model.stream(context)
        final: AssistantMessage | None = None
        try:
            async for event in stream:
                await self._check_cancelled()
                if final is not None:
                    raise ModelProtocolError("Model emitted events after its final message")
                if isinstance(event, TextDelta):
                    await self._emit(AgentEvent("message_update", self._turn, delta=event.text))
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
        return final

    async def _execute_tools(self, response: AssistantMessage) -> None:
        calls = response.tool_calls
        self._pending.update(call.id for call in calls)
        try:
            await self._append(response, started=True)
            await self._check_cancelled()
            for call in calls:
                logger.debug("工具执行开始", extra={"tool": call.name, "call_id": call.id})
                await self._emit(AgentEvent("tool_execution_start", self._turn, tool_call=call))
                result = await self._execute_tool(call)
                await self._finish_tool(call, result)
        finally:
            # 取消批次后，会话历史中不能留下缺少结果的工具调用。
            # 等待订阅回调前先补齐所有占位结果，保证再次取消时历史仍完整。
            remaining = [call for call in calls if call.id in self._pending]
            result = ToolResult("Tool execution cancelled before a result was available", True)
            for call in remaining:
                self._messages.append(self._tool_message(call, result))
                self._pending.discard(call.id)
            for call in remaining:
                message = self._tool_message(call, result)
                await self._emit(
                    AgentEvent("tool_execution_end", self._turn, tool_call=call, result=result)
                )
                await self._emit(AgentEvent("message_start", self._turn, message=message))
                await self._emit(AgentEvent("message_end", self._turn, message=message))

    async def _execute_tool(self, call: ToolCall) -> ToolResult:
        await self._check_cancelled()
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(f"Unknown tool: {call.name}", True)
        try:
            parameters = tool.parameters.model_validate(deepcopy(call.arguments), strict=True)
            async with asyncio.timeout(self._tool_timeout):
                result = await tool.execute(parameters, ToolContext(call.id, self._turn))
            if not isinstance(result, ToolResult):
                raise TypeError("Tools must return ToolResult")
            return result
        except Exception as exc:
            return ToolResult(f"{type(exc).__name__}: {exc}", True)

    async def _finish_tool(self, call: ToolCall, result: ToolResult) -> None:
        logger.debug(
            "工具执行结束",
            extra={"tool": call.name, "call_id": call.id, "is_error": result.is_error},
        )
        message = self._tool_message(call, result)
        self._messages.append(message)
        self._pending.discard(call.id)
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

    async def _emit(self, event: AgentEvent) -> None:
        for listener in tuple(self._listeners):
            try:
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
