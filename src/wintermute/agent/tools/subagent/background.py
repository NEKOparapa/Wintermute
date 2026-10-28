"""后台子智能体、完成通知与主会话的串行消息调度。"""

import asyncio
import inspect
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from functools import partial
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from wintermute.agent import (
    AgentBusyError,
    AgentEvent,
    AgentRunner,
    RunResult,
    Tool,
    ToolContext,
    ToolResult,
    UserMessage,
)
from wintermute.agent.tools.subagent.foreground import (
    NonemptyText,
    SubagentConfig,
    SubagentEvent,
    SubagentInput,
    SubagentTask,
    _bounded_text,
    create_subagent_tool,
)
from wintermute.logging import log_context

logger = logging.getLogger(__name__)

type BackgroundStatus = Literal[
    "queued", "running", "cancelling", "completed", "error", "cancelled"
]
type NotificationStatus = Literal[
    "none", "pending", "processing", "published", "delivered", "failed", "suppressed"
]


@dataclass(frozen=True, slots=True)
class BackgroundTaskState:
    """任务的不可变快照；取消完成后才进入 cancelled 状态。"""

    task_id: str
    agent: str
    task: str
    status: BackgroundStatus = "queued"
    output: str = ""
    error: str | None = None
    turns: int = 0
    truncated: bool = False
    notification_status: NotificationStatus = "none"
    notification_error: str | None = None
    call_id: str = ""


@dataclass(frozen=True, slots=True)
class BackgroundSubagentEvent:
    """携带后台任务 ID 的子 Agent 流式事件。"""

    task_id: str
    call_id: str
    agent: str
    task: str
    event: AgentEvent


type BackgroundSubagentListener = Callable[[BackgroundSubagentEvent], Awaitable[None] | None]
type BackgroundNotificationPublisher = Callable[[BackgroundTaskState], Awaitable[None]]


class BackgroundStatusInput(BaseModel):
    """省略任务 ID 时列出当前保留的全部任务。"""

    model_config = ConfigDict(extra="forbid")
    task_id: NonemptyText | None = None


class BackgroundCancelInput(BaseModel):
    """请求取消指定的排队中或运行中的任务。"""

    model_config = ConfigDict(extra="forbid")
    task_id: NonemptyText


@dataclass(slots=True)
class _Job:
    state: BackgroundTaskState
    worker: asyncio.Task[dict[str, Any]] | None = None
    finished: bool = False


@dataclass(slots=True)
class _Delivery:
    message: UserMessage
    job: _Job | None = None
    result: asyncio.Future[RunResult] | None = None


class BackgroundSubagentManager:
    """管理后台任务，并将完成通知交给主会话或外部事件入口。

    默认先将 tools 注册到主 Runner，再调用 bind。配置 notification_publisher
    后无需绑定 Runner，通知交给外部事件入口，主运行由外部调度器负责。
    后台任务不随主运行取消；使用任务 ID 取消，或由 aclose 统一回收。
    模型和工具须支持并发。
    同一实例只在一个事件循环中使用，使用 async with 确保退出时清理。
    """

    def __init__(
        self,
        agents: Sequence[SubagentConfig],
        *,
        max_concurrency: int = 4,
        max_tasks: int = 128,
        max_output_bytes: int = 50 * 1024,
        on_event: BackgroundSubagentListener | None = None,
        notification_publisher: BackgroundNotificationPublisher | None = None,
    ) -> None:
        if isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks < 1:
            raise ValueError("max_tasks must be a positive integer")
        if notification_publisher is not None and not callable(notification_publisher):
            raise TypeError("notification_publisher must be callable")
        self._agents = {agent.name: agent for agent in agents}
        self._delegate = create_subagent_tool(
            agents,
            max_concurrency=max_concurrency,
            max_tasks=1,
            max_output_bytes=max_output_bytes,
            on_event=self._forward,
        )
        self._max_tasks = max_tasks
        self._max_output_bytes = max_output_bytes
        self._on_event = on_event
        self._notification_publisher = notification_publisher
        self._parent: AgentRunner | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._jobs: dict[str, _Job] = {}
        self._outbox: deque[_Delivery] = deque()
        self._dispatcher: asyncio.Task[None] | None = None
        self._publications: set[asyncio.Task[None]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._changed = asyncio.Event()
        self._managed: ContextVar[bool] = ContextVar(
            f"wintermute-background-{id(self)}", default=False
        )
        descriptions = "; ".join(
            f"{agent.name}: {agent.description}" for agent in self._agents.values()
        )
        self._tools: tuple[Tool[Any], ...] = (
            Tool(
                "subagent_start",
                "Start independent background agents and immediately return task IDs. "
                "Use agent/task or tasks (up to 8), never both. Include all required context. "
                "Completion, errors and cancellation are automatically reported later; "
                "continue the conversation without polling. "
                f"Available agents: {descriptions}",
                SubagentInput,
                self._start_tool,
            ),
            Tool(
                "subagent_status",
                "Get a background task's status and result by task_id; omit task_id to list tasks.",
                BackgroundStatusInput,
                self._status_tool,
            ),
            Tool(
                "subagent_cancel",
                "Request cancellation of a background task by task_id. "
                "Cancelling means cleanup is still in progress; "
                "cancelled means cleanup is complete.",
                BackgroundCancelInput,
                self._cancel_tool,
            ),
        )

    @property
    def tools(self) -> tuple[Tool[Any], ...]:
        return self._tools

    def bind(self, runner: AgentRunner) -> None:
        """绑定唯一的主会话；重复绑定同一实例不会产生额外订阅或调度器。"""
        if self._closed:
            raise RuntimeError("Background manager is closed")
        if self._parent is not None and self._parent is not runner:
            raise ValueError("Background manager is already bound to another runner")
        self._parent = runner

    def _check_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("Background manager must be used in the same event loop")
        return loop

    def _ensure_open(self) -> asyncio.AbstractEventLoop:
        loop = self._check_loop()
        if self._closed:
            raise RuntimeError("Background manager is closed")
        if self._parent is None and self._notification_publisher is None:
            raise RuntimeError("Bind a parent AgentRunner before using background tasks")
        return loop

    def _ensure_external_wait(self) -> None:
        if self._managed.get() or (self._parent is not None and self._parent.in_run_context):
            raise RuntimeError("Managed tools and callbacks cannot wait on their own manager")

    async def __aenter__(self) -> Self:
        self._ensure_open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    def start(self, agent: str, task: str, *, call_id: str = "") -> str:
        """启动单个后台任务，立即返回 ID，不等待模型或工具执行。"""
        request = SubagentTask.model_validate({"agent": agent, "task": task}, strict=True)
        return self._start_many([request], call_id)[0].task_id

    def _start_many(
        self, requests: Sequence[SubagentTask], call_id: str
    ) -> list[BackgroundTaskState]:
        self._ensure_open()
        if not 1 <= len(requests) <= 8:
            raise ValueError("Provide between 1 and 8 background tasks per call")
        unknown = sorted(
            {request.agent for request in requests if request.agent not in self._agents}
        )
        if unknown:
            raise ValueError(f"Unknown subagents: {', '.join(unknown)}")
        # 只有结果通知已结束的旧记录可以淘汰；先检查容量，再创建任何任务。
        needed = max(0, len(self._jobs) + len(requests) - self._max_tasks)
        removable = [
            task_id
            for task_id, job in self._jobs.items()
            if job.finished
            and job.state.notification_status in {"published", "delivered", "failed", "suppressed"}
        ]
        if len(removable) < needed:
            raise ValueError("Background task capacity reached; wait for existing tasks to finish")
        for task_id in removable[:needed]:
            del self._jobs[task_id]
        states: list[BackgroundTaskState] = []
        for request in requests:
            task_id = f"subagent_{uuid4().hex}"
            state = BackgroundTaskState(task_id, request.agent, request.task, call_id=call_id)
            job = _Job(state)
            self._jobs[task_id] = job
            worker = asyncio.create_task(
                self._run_job(job), name=f"wintermute-background-{task_id}"
            )
            job.worker = worker
            worker.add_done_callback(partial(self._finish_job, job))
            states.append(state)
            logger.info("后台子 Agent 已排队", extra={"task_id": task_id, "agent": request.agent})
        self._changed.set()
        return states

    async def _run_job(self, job: _Job) -> dict[str, Any]:
        with log_context(task_id=job.state.task_id, agent=job.state.agent):
            return await self._execute_job(job)

    async def _execute_job(self, job: _Job) -> dict[str, Any]:
        token = self._managed.set(True)
        try:
            # 兼容立即执行的任务工厂，先完成 ID 与任务句柄登记。
            await asyncio.sleep(0)
            result = await self._delegate.execute(
                SubagentInput(agent=job.state.agent, task=job.state.task),
                ToolContext(job.state.task_id, 1),
            )
            data: dict[str, Any] = json.loads(result.content)["results"][0]
            return data
        finally:
            self._managed.reset(token)

    async def _forward(self, update: SubagentEvent) -> None:
        job = self._jobs[update.call_id]
        state = job.state
        status = state.status
        if update.event.type == "agent_start" and status == "queued":
            status = "running"
        job.state = replace(state, status=status, turns=max(state.turns, update.event.turn))
        self._changed.set()
        if self._on_event is not None:
            outcome = self._on_event(
                BackgroundSubagentEvent(
                    state.task_id, state.call_id, state.agent, state.task, update.event
                )
            )
            if inspect.isawaitable(outcome):
                await outcome

    def _finish_job(self, job: _Job, worker: asyncio.Task[dict[str, Any]]) -> None:
        if job.finished:
            return
        # 包括尚未进入协程就被取消的任务，统一在完成回调中登记结果。
        job.finished = True
        try:
            result = worker.result()
        except asyncio.CancelledError:
            job.state = replace(job.state, status="cancelled", error="Task cancelled")
        except Exception as exc:
            error, truncated = _bounded_text(f"{type(exc).__name__}: {exc}", self._max_output_bytes)
            job.state = replace(job.state, status="error", error=error, truncated=truncated)
        else:
            job.state = replace(
                job.state,
                status=result["status"],
                output=result["output"],
                error=result.get("error"),
                turns=result["turns"],
                truncated=result["truncated"],
            )
        logger.info(
            "后台子 Agent 已结束",
            extra={
                "task_id": job.state.task_id,
                "agent": job.state.agent,
                "status": job.state.status,
            },
        )
        if self._closed:
            job.state = replace(job.state, notification_status="suppressed")
        elif self._notification_publisher is not None:
            job.state = replace(job.state, notification_status="pending")
            publication = asyncio.create_task(
                self._publish_notification(job),
                name=f"wintermute-background-publish-{job.state.task_id}",
            )
            self._publications.add(publication)
            publication.add_done_callback(partial(self._publication_done, job))
        else:
            job.state = replace(job.state, notification_status="pending")
            state = job.state
            payload = {
                "source": "background_subagent",
                "task_id": state.task_id,
                "agent": state.agent,
                "task": state.task,
                "status": state.status,
                "output": state.output,
                "error": state.error,
                "turns": state.turns,
                "truncated": state.truncated,
            }
            self._outbox.append(
                _Delivery(UserMessage(json.dumps(payload, ensure_ascii=False)), job)
            )
            self._schedule_dispatch()
        self._changed.set()

    async def _publish_notification(self, job: _Job) -> None:
        """发布结果事件；成功只表示入口已接收，不表示主 Agent 已处理。"""
        token = self._managed.set(True)
        try:
            # 先登记任务句柄，兼容立即执行的任务工厂及关闭时的取消。
            await asyncio.sleep(0)
            if self._closed:
                job.state = replace(job.state, notification_status="suppressed")
                return
            job.state = replace(job.state, notification_status="processing")
            publisher = self._notification_publisher
            assert publisher is not None
            await publisher(job.state)
        except asyncio.CancelledError:
            job.state = replace(
                job.state,
                notification_status="suppressed" if self._closed else "failed",
                notification_error="CancelledError: notification publication cancelled",
            )
            raise
        except Exception as exc:
            error, _ = _bounded_text(f"{type(exc).__name__}: {exc}", self._max_output_bytes)
            job.state = replace(job.state, notification_status="failed", notification_error=error)
        else:
            job.state = replace(job.state, notification_status="published", notification_error=None)
            logger.info(
                "后台子 Agent 通知已发布",
                extra={"task_id": job.state.task_id, "agent": job.state.agent},
            )
        finally:
            self._managed.reset(token)
            self._changed.set()

    def _publication_done(self, job: _Job, publication: asyncio.Task[None]) -> None:
        """收回发布句柄，并处理协程首次执行前就收到的取消。"""
        self._publications.discard(publication)
        if publication.cancelled():
            if job.state.notification_status in {"pending", "processing"}:
                job.state = replace(
                    job.state,
                    notification_status="suppressed" if self._closed else "failed",
                    notification_error="CancelledError: notification publication cancelled",
                )
        elif publication.exception() is not None:
            # 常规发布异常已在协程中转为状态；此处只兜底内部意外错误。
            error, _ = _bounded_text(str(publication.exception()), self._max_output_bytes)
            job.state = replace(job.state, notification_status="failed", notification_error=error)
            logger.error(
                "后台子 Agent 通知发布失败",
                extra={
                    "task_id": job.state.task_id,
                    "error_type": type(publication.exception()).__name__,
                },
            )
        self._changed.set()

    def get(self, task_id: str) -> BackgroundTaskState:
        """获取快照；未知或已淘汰的 ID 抛出 KeyError。"""
        return self._jobs[task_id].state

    def list_tasks(self) -> tuple[BackgroundTaskState, ...]:
        return tuple(job.state for job in self._jobs.values())

    def cancel(self, task_id: str) -> bool:
        """只注入一次取消；使用 wait 等待实际清理结束。"""
        self._check_loop()
        job = self._jobs[task_id]
        worker = job.worker
        if worker is None or worker.done() or worker.cancelling():
            return False
        job.state = replace(job.state, status="cancelling")
        self._changed.set()
        return worker.cancel()

    async def wait(self, task_id: str) -> BackgroundTaskState:
        """等待子任务清理，不等待通知发布或消费；取消等待者不取消子任务。"""
        self._ensure_external_wait()
        self._check_loop()
        job = self._jobs[task_id]
        worker = job.worker
        assert worker is not None
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            pass
        self._finish_job(job, worker)
        return job.state

    async def send(self, prompt: str | UserMessage) -> RunResult:
        """将用户输入与完成通知串行处理，返回这条输入对应的主运行结果。

        取消等待者不会撤回已入队的消息；需要中止正在生成的回复时调用
        主 Runner.cancel()。工具和回调不能等待此方法，避免等待自身。
        """
        self._ensure_external_wait()
        loop = self._ensure_open()
        if self._notification_publisher is not None:
            raise RuntimeError(
                "Use the conversation event ingress when notification_publisher is set"
            )
        message = UserMessage(prompt) if isinstance(prompt, str) else prompt
        if not isinstance(message, UserMessage):
            raise TypeError("Expected a string or UserMessage")
        future: asyncio.Future[RunResult] = loop.create_future()
        # 即使发送方取消等待，也取走异常，避免无人接收的 Future 报警。
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        self._outbox.append(_Delivery(message, result=future))
        self._changed.set()
        self._schedule_dispatch()
        return await asyncio.shield(future)

    def _schedule_dispatch(self) -> None:
        if (
            not self._closed
            and self._outbox
            and (self._dispatcher is None or self._dispatcher.done())
        ):
            self._dispatcher = asyncio.create_task(
                self._dispatch(), name="wintermute-background-dispatch"
            )
            self._dispatcher.add_done_callback(self._dispatch_done)

    def _dispatch_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "后台子 Agent 调度失败", extra={"error_type": type(task.exception()).__name__}
            )
        self._changed.set()
        self._schedule_dispatch()

    async def _dispatch(self) -> None:
        token = self._managed.set(True)
        try:
            await asyncio.sleep(0)
            parent = self._parent
            assert parent is not None
            while self._outbox and not self._closed:
                delivery = self._outbox[0]
                # wait_until_idle 不会将本调度器的取消传给外部发起的主运行。
                await parent.wait_until_idle()
                if self._closed:
                    break
                if delivery.job is not None:
                    delivery.job.state = replace(
                        delivery.job.state, notification_status="processing"
                    )
                try:
                    result = await parent.run(delivery.message)
                except AgentBusyError:
                    # 原运行的 finally 尚未清理，或新用户输入恰好先占用了 Runner。
                    if delivery.job is not None:
                        delivery.job.state = replace(
                            delivery.job.state, notification_status="pending"
                        )
                    await asyncio.sleep(0)
                    continue
                except asyncio.CancelledError:
                    self._delivery_failed(delivery, "CancelledError: parent run cancelled")
                    self._outbox.popleft()
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    # 主 Runner.cancel() 只结束当前回复，不停止后续通知服务。
                except Exception as exc:
                    error, _ = _bounded_text(f"{type(exc).__name__}: {exc}", self._max_output_bytes)
                    self._delivery_failed(delivery, error, exc)
                    self._outbox.popleft()
                else:
                    if delivery.job is not None:
                        delivery.job.state = replace(
                            delivery.job.state, notification_status="delivered"
                        )
                    if delivery.result is not None and not delivery.result.done():
                        delivery.result.set_result(result)
                    self._outbox.popleft()
                self._changed.set()
        finally:
            self._managed.reset(token)

    def _delivery_failed(
        self, delivery: _Delivery, error: str, exc: Exception | None = None
    ) -> None:
        if delivery.job is not None:
            delivery.job.state = replace(
                delivery.job.state,
                notification_status="suppressed" if self._closed else "failed",
                notification_error=error,
            )
        if delivery.result is not None and not delivery.result.done():
            if exc is None:
                delivery.result.cancel()
            else:
                delivery.result.set_exception(exc)

    async def wait_until_idle(self) -> None:
        """等待所有后台任务与通知发布、消息投递结束，不负责停止管理器。"""
        self._ensure_external_wait()
        self._check_loop()
        while (
            any(not job.finished for job in self._jobs.values())
            or self._outbox
            or self._publications
            or (self._dispatcher is not None and not self._dispatcher.done())
        ):
            self._changed.clear()
            await self._changed.wait()

    async def aclose(self) -> None:
        """关闭时取消工作与发布，抑制关闭产生的通知，并等待资源清理。"""
        self._ensure_external_wait()
        self._check_loop()
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                self._close(), name="wintermute-background-close"
            )
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        owned: list[asyncio.Task[Any]] = []
        for task_id, job in self._jobs.items():
            self.cancel(task_id)
            if job.worker is not None:
                owned.append(job.worker)
        if self._dispatcher is not None:
            if not self._dispatcher.done() and not self._dispatcher.cancelling():
                self._dispatcher.cancel()
            owned.append(self._dispatcher)
        for publication in tuple(self._publications):
            if not publication.done() and not publication.cancelling():
                publication.cancel()
            owned.append(publication)
        await asyncio.gather(*owned, return_exceptions=True)
        while self._outbox:
            self._delivery_failed(self._outbox.popleft(), "Background manager closed")
        self._changed.set()

    async def _start_tool(self, params: SubagentInput, context: ToolContext) -> ToolResult:
        if params.tasks is not None:
            requests = params.tasks
        else:
            assert params.agent is not None and params.task is not None
            requests = [SubagentTask(agent=params.agent, task=params.task)]
        states = self._start_many(requests, context.call_id)
        return ToolResult(
            json.dumps(
                {
                    "tasks": [
                        {"task_id": state.task_id, "agent": state.agent, "status": state.status}
                        for state in states
                    ]
                },
                ensure_ascii=False,
            )
        )

    async def _status_tool(self, params: BackgroundStatusInput, context: ToolContext) -> ToolResult:
        self._ensure_open()
        payload = (
            {"task": asdict(self.get(params.task_id))}
            if params.task_id is not None
            else {"tasks": [asdict(state) for state in self.list_tasks()]}
        )
        return ToolResult(json.dumps(payload, ensure_ascii=False))

    async def _cancel_tool(self, params: BackgroundCancelInput, context: ToolContext) -> ToolResult:
        self._ensure_open()
        requested = self.cancel(params.task_id)
        return ToolResult(
            json.dumps(
                {"cancel_requested": requested, "task": asdict(self.get(params.task_id))},
                ensure_ascii=False,
            )
        )
