"""按会话串行、跨会话并行的持久化事件调度。"""

import asyncio
import inspect
import logging
import math
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, Self
from uuid import uuid4

from wintermute.agent import AgentEvent, AgentRunner, Message
from wintermute.events.core.types import EventError, JobRecord
from wintermute.events.ingress.gateway import EventGateway
from wintermute.events.runtime.context import ContextAssembler
from wintermute.events.runtime.output import OutputRouter
from wintermute.events.slices import SliceProcessor
from wintermute.logging import log_context

logger = logging.getLogger(__name__)

type RunnerFactory = Callable[[str], AgentRunner]


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """为 Runner 的流式事件补充持久化任务及会话标识。"""

    job_id: str
    event_id: str
    user_id: str
    conversation_id: str
    event: AgentEvent


type RuntimeListener = Callable[[RuntimeEvent], Awaitable[None] | None]


class RunScheduler:
    """使用数据库待办自动唤醒 AgentRunner，同一数据库仅一个活动调度器。

    排队任务可在重启后继续。接管过期租约时将遗留运行标为 interrupted，
    防止自动重放可能已经产生副作用的工具。runner_factory 为每个会话创建
    独立 Runner；运行期间应通过入口提交输入，不直接操作其运行和消息队列。
    """

    def __init__(
        self,
        gateway: EventGateway,
        runner_factory: RunnerFactory,
        *,
        assembler: ContextAssembler | None = None,
        output_router: OutputRouter | None = None,
        slice_processor: SliceProcessor | Literal[False] | None = None,
        max_concurrency: int = 4,
        poll_interval: float = 0.1,
        lease_seconds: float = 30.0,
        delivery_retry_interval: float = 2.0,
    ) -> None:
        if isinstance(max_concurrency, bool) or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        if not isinstance(max_concurrency, int):
            raise TypeError("max_concurrency must be an integer")
        for name, value in (
            ("poll_interval", poll_interval),
            ("lease_seconds", lease_seconds),
            ("delivery_retry_interval", delivery_retry_interval),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if poll_interval >= lease_seconds / 3:
            raise ValueError("poll_interval must be less than one third of the lease")
        self.gateway = gateway
        self.store = gateway.store
        self.runner_factory = runner_factory
        self.slice_processor = (
            SliceProcessor(self.store)
            if slice_processor is None
            else None
            if slice_processor is False
            else slice_processor
        )
        if self.slice_processor is not None and self.slice_processor.store is not self.store:
            raise ValueError("Slice processor must share the scheduler store instance")
        self.assembler = (
            assembler
            if assembler is not None
            else ContextAssembler(
                self.store,
                max_slice_items=8 if self.slice_processor is not None else 0,
                slice_timezone=(
                    self.slice_processor.timezone
                    if self.slice_processor is not None
                    else "Asia/Shanghai"
                ),
            )
        )
        self.output_router = (
            output_router if output_router is not None else OutputRouter(self.store)
        )
        if self.assembler.store is not self.store or self.output_router.store is not self.store:
            raise ValueError("Scheduler components must share the same store instance")
        self.max_concurrency = max_concurrency
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.delivery_retry_interval = delivery_retry_interval
        self._owner_id = f"scheduler_{uuid4().hex}"
        self._runners: dict[str, AgentRunner] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._active_conversations: set[str] = set()
        self._listeners: list[RuntimeListener] = []
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dispatcher: asyncio.Task[None] | None = None
        self._delivery: asyncio.Task[int] | None = None
        self._slice_task: asyncio.Task[int] | None = None
        self._deliveries: set[asyncio.Task[int]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._stopping = False
        self._error: BaseException | None = None
        self._next_delivery_at = 0.0
        self._managed: ContextVar[bool] = ContextVar(f"wintermute-events-{id(self)}", default=False)

    def subscribe(self, listener: RuntimeListener) -> Callable[[], None]:
        """订阅带有会话 ID 的文本增量、工具和运行事件。"""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def wakeup(self) -> None:
        self._wake.set()
        self._changed.set()

    def _check_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("Scheduler must be used in the same event loop")
        return loop

    def _ensure_external_wait(self) -> None:
        if self._managed.get():
            raise RuntimeError("Managed models, tools and callbacks cannot wait on their scheduler")

    async def start(self) -> None:
        self._check_loop()
        if self._stopping:
            raise RuntimeError("Scheduler is closed")
        if self._dispatcher is not None:
            if self._dispatcher.done():
                raise RuntimeError("Scheduler has stopped") from self._error
            return
        self.store.acquire_scheduler(self._owner_id, lease_seconds=self.lease_seconds)
        self._unsubscribe = self.gateway.subscribe(self.wakeup)
        self._dispatcher = asyncio.create_task(self._dispatch(), name="wintermute-event-scheduler")

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def _dispatch(self) -> None:
        loop = asyncio.get_running_loop()
        next_heartbeat = loop.time() + self.lease_seconds / 3
        try:
            while not self._stopping:
                self._wake.clear()
                if loop.time() >= next_heartbeat:
                    self.store.heartbeat_scheduler(self._owner_id, lease_seconds=self.lease_seconds)
                    next_heartbeat = loop.time() + self.lease_seconds / 3
                if self._slice_task is not None and self._slice_task.done():
                    # 压缩器已处理可重试的策略错误；数据库等运行故障交给调度器收尾。
                    self._slice_task.result()
                    self._slice_task = None
                if (
                    self.slice_processor is not None
                    and self._slice_task is None
                    and self.store.pending_slices(limit=1)
                ):
                    self._slice_task = asyncio.create_task(
                        self._process_slices(), name="wintermute-slice-processing"
                    )
                while len(self._workers) < self.max_concurrency:
                    job = self.store.claim_next(
                        excluded_conversations=tuple(self._active_conversations),
                        owner_id=self._owner_id,
                    )
                    if job is None:
                        break
                    self._active_conversations.add(job.conversation_id)
                    self._workers[job.job_id] = asyncio.create_task(
                        self._execute(job), name=f"wintermute-event-{job.job_id}"
                    )
                if self._delivery is not None and self._delivery.done():
                    try:
                        self._delivery.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logger.error(
                            "终端投递失败，持久化输出仍可重试",
                            extra={"error_type": type(exc).__name__},
                        )
                    self._delivery = None
                if self._delivery is None and loop.time() >= self._next_delivery_at:
                    self._delivery = self._spawn_delivery()
                    self._next_delivery_at = loop.time() + self.delivery_retry_interval
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            self._stopping = True
        except Exception as exc:
            self._error = exc
            self._stopping = True
            logger.error("事件调度器停止", extra={"error_type": type(exc).__name__})
        finally:
            owned_job_ids = tuple(self._workers)
            workers = tuple(self._workers.values())
            deliveries = tuple(self._deliveries)
            # 先向所有任务发出取消，再等待可能较慢的网络流关闭。
            for worker in workers:
                worker.cancel()
            for delivery in deliveries:
                delivery.cancel()
            if self._slice_task is not None:
                self._slice_task.cancel()
                await asyncio.gather(self._slice_task, return_exceptions=True)
                self._slice_task = None
            await asyncio.gather(*workers, return_exceptions=True)
            # 尚未进入协程的任务被取消时不会执行自身 finally，显式收尾其记录。
            for job_id in owned_job_ids:
                if self.store.get_job(job_id).status == "running":
                    try:
                        self.store.fail_job(
                            job_id,
                            "Scheduler stopped before execution completed",
                            interrupted=True,
                            owner_id=self._owner_id,
                        )
                    except Exception as exc:
                        logger.error(
                            "中断记录等待租约接管恢复",
                            extra={"job_id": job_id, "error_type": type(exc).__name__},
                        )
            self._workers.clear()
            self._active_conversations.clear()
            await asyncio.gather(*deliveries, return_exceptions=True)
            self._deliveries.clear()
            if self._unsubscribe is not None:
                self._unsubscribe()
            self.store.release_scheduler(self._owner_id)
            self._changed.set()

    async def _process_slices(self) -> int:
        """切片只有独立的投影任务，不占用会话并发额度或创建 Agent 工作项。"""
        assert self.slice_processor is not None
        token = self._managed.set(True)
        try:
            count = await self.slice_processor.process_pending(owner_id=self._owner_id)
            if count:
                self.wakeup()
            return count
        finally:
            self._managed.reset(token)
            self._changed.set()

    def _spawn_delivery(self) -> asyncio.Task[int]:
        """所有投递都归调度器所有，等待者取消不撤回投递，关闭统一回收。"""
        task = asyncio.create_task(self._deliver(), name="wintermute-output-delivery")
        self._deliveries.add(task)

        def finished(completed: asyncio.Task[int]) -> None:
            self._deliveries.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logger.error(
                    "终端投递任务失败",
                    extra={"error_type": type(completed.exception()).__name__},
                )
            self._changed.set()

        task.add_done_callback(finished)
        return task

    async def _deliver(self) -> int:
        token = self._managed.set(True)
        try:
            return await self.output_router.deliver_pending()
        finally:
            self._managed.reset(token)
            self._changed.set()

    async def _execute(self, job: JobRecord) -> None:
        with log_context(
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            event_id=job.event_id,
            job_id=job.job_id,
        ):
            await self._execute_job(job)

    async def _execute_job(self, job: JobRecord) -> None:
        token = self._managed.set(True)
        captured: list[Message] = []
        unsubscribe: Callable[[], None] | None = None
        runner: AgentRunner | None = None
        history_start = 0

        async def observe(event: AgentEvent) -> None:
            if event.type == "message_end" and event.message is not None:
                captured.append(deepcopy(event.message))
            elif event.type == "agent_end" and runner is not None:
                # 取消可能中断某个 message_end 回调；结束时读取已补齐工具结果的历史。
                # 临时通知运行此时尚未恢复交流历史，因此也能取得完整执行记录。
                captured[:] = runner.state.messages[history_start:]
            envelope = RuntimeEvent(
                job.job_id, job.event_id, job.user_id, job.conversation_id, deepcopy(event)
            )
            for listener in tuple(self._listeners):
                try:
                    result = listener(deepcopy(envelope))
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    logger.error(
                        "事件运行的流式订阅回调失败",
                        extra={"error_type": type(exc).__name__},
                    )

        try:
            # 支持立即执行的任务工厂，等待 _workers 完成登记后再进入业务逻辑。
            await asyncio.sleep(0)
            logger.info("事件任务开始")
            event = self.store.get_event(job.event_id)
            bundle = self.assembler.assemble(event)
            history_start = len(bundle.history)
            runner = self._runners.get(job.conversation_id)
            if runner is None:
                runner = self.runner_factory(job.conversation_id)
                if not isinstance(runner, AgentRunner):
                    raise TypeError("runner_factory must return AgentRunner")
                if any(existing is runner for existing in self._runners.values()):
                    raise ValueError("Each conversation requires a separate AgentRunner")
                self._runners[job.conversation_id] = runner
            runner.restore_messages(bundle.history)
            unsubscribe = runner.subscribe(observe)
            result = await runner.run(
                bundle.prompt,
                references=bundle.references,
                record_history=event.layer == "conversation",
            )
            self.store.finish_job(
                job.job_id,
                messages=result.messages,
                response=result.response.text,
                reply_terminal_id=event.reply_terminal_id,
                owner_id=self._owner_id,
            )
            self._next_delivery_at = 0.0
            logger.info("事件任务完成")
        except asyncio.CancelledError:
            logger.info("事件任务已取消")
            self._record_failure(
                job,
                "Execution cancelled; inspect side effects before retrying",
                tuple(captured),
                interrupted=True,
            )
            raise
        except Exception as exc:
            logger.warning("事件任务失败", extra={"error_type": type(exc).__name__})
            self._record_failure(job, f"{type(exc).__name__}: {exc}", tuple(captured))
        finally:
            if unsubscribe is not None:
                unsubscribe()
            self._workers.pop(job.job_id, None)
            self._active_conversations.discard(job.conversation_id)
            self._managed.reset(token)
            self.wakeup()

    def _record_failure(
        self,
        job: JobRecord,
        error: str,
        messages: tuple[Message, ...],
        *,
        interrupted: bool = False,
    ) -> None:
        try:
            try:
                self.store.fail_job(
                    job.job_id,
                    error,
                    messages=messages,
                    interrupted=interrupted,
                    owner_id=self._owner_id,
                )
            except (EventError, TypeError, ValueError) as exc:
                # 非法服务商元数据或被回调打断的转录不能让会话永久停在 running。
                self.store.fail_job(
                    job.job_id,
                    f"{error}; execution transcript could not be saved: {exc}",
                    interrupted=interrupted,
                    owner_id=self._owner_id,
                )
        except Exception as exc:
            logger.error(
                "任务失败状态无法提交，等待租约接管恢复",
                extra={"job_id": job.job_id, "error_type": type(exc).__name__},
            )

    def _ensure_running(self) -> None:
        if self._dispatcher is None or self._dispatcher.done() or self._stopping:
            raise RuntimeError("Scheduler is not running") from self._error

    async def _wait_change(self) -> None:
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=self.poll_interval)
        except TimeoutError:
            pass

    async def wait(self, job_id: str) -> JobRecord:
        """等待任务终态；取消等待者不撤回已接收事件，也不取消执行。"""
        self._ensure_external_wait()
        self._check_loop()
        while True:
            job = self.store.get_job(job_id)
            if job.status not in {"queued", "running"}:
                return job
            self._ensure_running()
            await self._wait_change()

    async def wait_until_idle(self) -> None:
        """等待已入库的工作结束并尝试投递；不等待尚未发布结果的后台任务。"""
        self._ensure_external_wait()
        self._check_loop()
        while True:
            self._ensure_running()
            if not self._workers and not self.store.list_jobs(status="queued", limit=1):
                await asyncio.shield(self._spawn_delivery())
                self._ensure_running()
                if not self._workers and not self.store.list_jobs(status="queued", limit=1):
                    return
            await self._wait_change()

    async def wait_until_stopped(self) -> None:
        """等待调度循环退出并传播故障；取消等待者不会停止调度器。"""
        self._ensure_external_wait()
        self._check_loop()
        if self._dispatcher is None:
            raise RuntimeError("Scheduler has not started")
        await asyncio.shield(self._dispatcher)
        if self._error is not None:
            raise RuntimeError("Event scheduler stopped after a failure") from self._error

    async def wait_for_slices(self, *, timeout: float | None = 30) -> None:  # noqa: ASYNC109
        """等待切片投影含延迟重试完成；取消等待不取消压缩，持续输入可能超时。"""
        self._ensure_external_wait()
        self._check_loop()
        if self.slice_processor is None:
            raise RuntimeError("Slice processing is disabled")
        if timeout is not None and (
            isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ValueError("timeout must be finite and positive")
        async with asyncio.timeout(timeout):
            while True:
                self._ensure_running()
                if not self.store.has_unprocessed_slices():
                    return
                await self._wait_change()

    async def aclose(self) -> None:
        """停止领取任务，取消自身运行并完成清理；排队事件保留供下次启动。"""
        self._ensure_external_wait()
        self._check_loop()
        if self._close_task is None:
            self._stopping = True
            self.wakeup()
            self._close_task = asyncio.create_task(self._close(), name="wintermute-events-close")
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
        if self._dispatcher is not None:
            await self._dispatcher
