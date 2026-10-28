"""以独立 AgentRunner 和 asyncio 任务实现单次子智能体委派。"""

import asyncio
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Self
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wintermute.agent import (
    AgentEvent,
    AgentRunner,
    ContextTransform,
    Model,
    Tool,
    ToolContext,
    ToolResult,
)

type NonemptyText = Annotated[str, Field(min_length=1, pattern=r"\S")]

if TYPE_CHECKING:
    from wintermute.agent.context import ContextManagerFactory


class SubagentTask(BaseModel):
    """交给一个已注册角色的任务，不包含父会话历史。"""

    model_config = ConfigDict(extra="forbid")
    agent: NonemptyText
    task: NonemptyText


class SubagentInput(BaseModel):
    """单任务与批量任务二选一，批量内各项相互独立。"""

    model_config = ConfigDict(extra="forbid")
    agent: NonemptyText | None = None
    task: NonemptyText | None = None
    tasks: list[SubagentTask] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.tasks is not None:
            if self.agent is not None or self.task is not None:
                raise ValueError("Use either agent/task or tasks, not both")
        elif self.agent is None or self.task is None:
            raise ValueError("Provide both agent and task, or a nonempty tasks list")
        return self


@dataclass(frozen=True, slots=True)
class SubagentConfig:
    """角色配置；模型和工具执行器不会复制，复用时须支持并发调用。

    每个任务重新创建会话，不自动继承父提示词、历史或工具。
    timeout 限制取得并发额度后的完整运行时间，不包括排队时间。
    """

    name: str
    model: Model
    description: str = ""
    system_prompt: str = ""
    tools: Sequence[Tool[Any]] = ()
    max_turns: int = 20
    tool_timeout: float | None = None
    model_timeout: float | None = None
    timeout: float | None = None
    transform_context: ContextTransform | None = None
    context_manager_factory: "ContextManagerFactory | None" = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Subagent name must not be empty")
        if self.timeout is not None and (
            isinstance(self.timeout, bool) or not math.isfinite(self.timeout) or self.timeout <= 0
        ):
            raise ValueError("Subagent timeout must be finite and positive")
        # 固定工具集合，同时保留执行器及其外部资源的身份。
        object.__setattr__(self, "tools", tuple(self.tools))
        if self.context_manager_factory is not None and not callable(self.context_manager_factory):
            raise TypeError("context_manager_factory must be callable")


@dataclass(frozen=True, slots=True)
class SubagentEvent:
    """携带父工具调用标识和从零开始的任务索引的子事件。"""

    call_id: str
    task_index: int
    agent: str
    task: str
    event: AgentEvent


type SubagentListener = Callable[[SubagentEvent], Awaitable[None] | None]


def _create_runner(config: SubagentConfig, *, with_context: bool = True) -> AgentRunner:
    manager = None
    if with_context and config.context_manager_factory is not None:
        manager = config.context_manager_factory()
        if manager is None:
            raise TypeError("context_manager_factory must return a ContextManager")
    return AgentRunner(
        config.model,
        system_prompt=config.system_prompt,
        tools=config.tools,
        max_turns=config.max_turns,
        tool_timeout=config.tool_timeout,
        model_timeout=config.model_timeout,
        transform_context=config.transform_context,
        context_manager=manager,
    )


def _bounded_text(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


async def _collect(tasks: Sequence[asyncio.Task[dict[str, Any]]]) -> list[dict[str, Any]]:
    """收集结果，并在取消时等所有子任务完成清理后再退出。"""
    batch = asyncio.gather(*tasks)
    try:
        # 父取消由本层显式传播，避免重复 cancel 打断子任务的异步清理。
        return await asyncio.shield(batch)
    finally:
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        cleanup = asyncio.gather(*tasks, return_exceptions=True)
        cancelled_during_cleanup = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled_during_cleanup = True
        # shield 被取消后，原收集器仍可能以异常结束，必须取走其异常。
        if batch.done() and not batch.cancelled():
            batch.exception()
        if cancelled_during_cleanup:
            raise asyncio.CancelledError


def create_subagent_tool(
    agents: Sequence[SubagentConfig],
    *,
    max_concurrency: int = 4,
    max_tasks: int = 8,
    max_output_bytes: int = 50 * 1024,
    on_event: SubagentListener | None = None,
) -> Tool[SubagentInput]:
    """创建可直接注册到 AgentRunner 的 subagent 工具。

    同一工具实例在同一事件循环内共享并发额度。先检查全部任务，再创建
    子任务；普通错误各自返回，取消则传播到整批并等待清理。子任务结果
    按输入顺序返回，只携带最终文本、状态和轮数，不回传完整子会话。
    on_event 可观察流式进度；其普通异常由子 Runner 的订阅机制隔离。
    """
    for name, value in (
        ("max_concurrency", max_concurrency),
        ("max_tasks", max_tasks),
        ("max_output_bytes", max_output_bytes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    registry = {agent.name: agent for agent in agents}
    if not registry or len(registry) != len(agents):
        raise ValueError("Subagents must have nonempty, unique names")
    for config in registry.values():
        # 在注册时校验 Runner 配置，避免执行委派后才发现重复工具等错误。
        _create_runner(config, with_context=False)
    # 执行中的调用持有信号量；空闲后释放它，避免信号量反向引用旧事件循环。
    limiters: WeakValueDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
        WeakValueDictionary()
    )

    async def run_one(
        index: int,
        request: SubagentTask,
        context: ToolContext,
        limiter: asyncio.Semaphore,
        owner: asyncio.Task[Any] | None,
    ) -> dict[str, Any]:
        worker = asyncio.current_task()

        async def check_owner_cancelled() -> None:
            if (
                owner is not None
                and owner.cancelling()
                and worker is not None
                and not worker.cancelling()
            ):
                # 同步订阅回调可能取消父 Runner；不等父协调层恢复调度，
                # 立即停止当前子任务。通过 worker 取消，避免清理阶段重复注入。
                worker.cancel()
                await asyncio.sleep(0)
                raise asyncio.CancelledError

        async with limiter:
            await check_owner_cancelled()
            config = registry[request.agent]
            turns = 0

            async def forward(event: AgentEvent) -> None:
                nonlocal turns
                turns = max(turns, event.turn)
                try:
                    if on_event is not None:
                        outcome = on_event(
                            SubagentEvent(
                                context.call_id, index, request.agent, request.task, event
                            )
                        )
                        if inspect.isawaitable(outcome):
                            await outcome
                finally:
                    if event.type != "agent_end":
                        await check_owner_cancelled()

            item: dict[str, Any] = {"agent": request.agent, "task": request.task}
            try:
                runner = _create_runner(config)
                runner.subscribe(forward)
                async with asyncio.timeout(config.timeout):
                    result = await runner.run(request.task)
                output, truncated = _bounded_text(result.response.text, max_output_bytes)
                item.update(
                    status="completed", output=output, turns=result.turns, truncated=truncated
                )
            except Exception as exc:
                error, truncated = _bounded_text(f"{type(exc).__name__}: {exc}", max_output_bytes)
                item.update(
                    status="error", output="", turns=turns, error=error, truncated=truncated
                )
            return item

    async def execute(parameters: SubagentInput, context: ToolContext) -> ToolResult:
        if parameters.tasks is not None:
            requests = parameters.tasks
        else:
            # 参数模型已保证单任务的两个字段同时存在。
            assert parameters.agent is not None and parameters.task is not None
            requests = [SubagentTask(agent=parameters.agent, task=parameters.task)]
        if len(requests) > max_tasks:
            return ToolResult(f"At most {max_tasks} subagent tasks are allowed per call", True)
        unknown = sorted({request.agent for request in requests if request.agent not in registry})
        if unknown:
            return ToolResult(f"Unknown subagents: {', '.join(unknown)}", True)

        loop = asyncio.get_running_loop()
        owner = asyncio.current_task()
        limiter = limiters.setdefault(loop, asyncio.Semaphore(max_concurrency))
        tasks = [
            asyncio.create_task(
                run_one(index, request, context, limiter, owner),
                name=f"wintermute-subagent-{context.call_id}-{index}-{request.agent}",
            )
            for index, request in enumerate(requests)
        ]
        results = await _collect(tasks)
        return ToolResult(
            json.dumps({"results": results}, ensure_ascii=False),
            is_error=any(item["status"] == "error" for item in results),
        )

    descriptions = "; ".join(
        f"{config.name}: {config.description}" if config.description else config.name
        for config in registry.values()
    )
    return Tool(
        "subagent",
        "Delegate tasks to agents with independent contexts. "
        "Use agent/task for one task or tasks for a parallel batch, never both. "
        "Include all necessary context in each task; parent history and tools are not inherited. "
        f"At most {max_tasks} tasks per call, {max_concurrency} running concurrently. "
        "Results are returned in input order; truncated=true marks shortened output/error text. "
        f"Available agents: {descriptions}",
        SubagentInput,
        execute,
    )
