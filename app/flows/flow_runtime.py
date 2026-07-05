"""分层流程运行时。

外部接口、定时任务或进程内调用方把输入包装成 FlowSubmitRequest 后提交到这里。
运行时按 L0/L1/L2/L3 分别排队处理，保证同一层内有序，同时避免低优先级
背景事件阻塞用户主动对话。
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..config.config import Settings
from ..infrastructure.llm.llm import OpenAICompatibleLLM
from ..infrastructure.storage.attachments import process_event_attachments
from .dialogue import DialogueService
from .ingest import L2EventIngestService, L3EventIngestService
from .proactive import L1ProactiveService

logger = logging.getLogger(__name__)

LEVELS = ("L0", "L1", "L2", "L3")


class RuntimeConfigError(ValueError):
    """流程运行时配置错误。保留给外部调用方捕获旧异常类型。"""


@dataclass(frozen=True)
class FlowSubmitRequest:
    """外部接口提交给分层流程的一条输入。"""

    # level 决定进入哪条处理链：L0 用户对话、L1 主动触发、L2 或 L3 背景事件。
    level: str
    # message 和 attachments 会原样进入事件 dict；不在运行时做结构校验。
    message: str | None = None
    attachments: list[dict[str, Any]] | None = None
    # source/type 会写入事件历史；未提供时根据接口名和层级推断默认值。
    source: str = "user"
    type: str | None = None
    # input_interface 只用于路由追踪，不再做白名单校验。
    input_interface: str | None = None
    # reply_target 是外部接口回消息所需的目标信息，例如 chat_id、thread_id。
    reply_target: dict[str, Any] | None = None
    # metadata 保留接口侧补充信息，会随事件一起进入历史。
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowSubmitResult:
    """流程提交结果。L0 返回最终回复，其他层默认只返回 accepted。"""

    # status 只表达提交/处理状态；业务回复放在 message，落库事件 ID 放在 event_id。
    status: str
    level: str
    task_id: str | None = None
    message: str | None = None
    event_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class InterfaceOutput:
    """流程发送给外部接口的一条输出。"""

    # interface 指向具体输出适配器名，target 是该适配器理解的发送目标。
    interface: str
    target: dict[str, Any]
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


class InterfaceAdapter(Protocol):
    """外部接口适配器协议。"""

    name: str

    def start(self, submit: Callable[[FlowSubmitRequest], FlowSubmitResult]) -> None:
        """启动输入监听。"""

    def stop(self) -> None:
        """停止输入监听。"""

    def send(self, output: InterfaceOutput) -> None:
        """发送流程输出。"""


class OutputDispatcher(Protocol):
    """流程输出分发器协议。"""

    def send(self, output: InterfaceOutput) -> None:
        """发送流程输出。"""


@dataclass(frozen=True)
class FlowConfig:
    """单个流程层的输入输出配置。"""

    level: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    # 只有 L0 允许同步等待最终回复，其他层必须快速返回 accepted。
    wait_for_result: bool = False


@dataclass
class _FlowTask:
    """运行时内部任务对象，用 done/result 在 submit 线程和 worker 线程之间同步。"""

    id: str
    request: FlowSubmitRequest
    event: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: FlowSubmitResult | None = None


_STOP = object()


class FlowRuntime:
    """L0/L1/L2/L3 分层流程运行时，每层一个有序队列和 worker 线程。"""

    def __init__(
        self,
        dialogue_service: DialogueService,
        proactive_service: L1ProactiveService,
        l2_ingest_service: L2EventIngestService,
        l3_ingest_service: L3EventIngestService,
        settings: Settings,
        *,
        flow_configs: dict[str, FlowConfig] | None = None,
        output_dispatcher: OutputDispatcher | None = None,
        interface_names: object = (),
    ) -> None:
        self.dialogue_service = dialogue_service
        self.proactive_service = proactive_service
        self.l2_ingest_service = l2_ingest_service
        self.l3_ingest_service = l3_ingest_service
        self.settings = settings
        self.attachment_llm = OpenAICompatibleLLM(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )
        self.flow_configs = flow_configs or default_flow_configs()
        self.output_dispatcher = output_dispatcher
        self.interface_names = frozenset(interface_names)

        # 每个层级一个队列，保证同层事件 FIFO；不同层之间并行处理。
        self._queues: dict[str, queue.Queue[_FlowTask | object]] = {
            level: queue.Queue() for level in LEVELS
        }
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False

    def start(self) -> None:
        """启动四个流程 worker。"""
        with self._lock:
            if self._started:
                return
            if self._stopped:
                raise RuntimeError("流程运行时已停止，不能重新启动。")
            for level in LEVELS:
                thread = threading.Thread(
                    target=self._run_worker,
                    args=(level,),
                    name=f"wintermute-{level}-worker",
                    daemon=True,
                )
                thread.start()
                self._threads[level] = thread
            self._started = True

    def stop(self, *, timeout: float = 5.0) -> None:
        """停止所有 worker。"""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            for task_queue in self._queues.values():
                task_queue.put(_STOP)
        for thread in self._threads.values():
            thread.join(timeout=timeout)

    def submit(self, request: FlowSubmitRequest) -> FlowSubmitResult:
        """提交输入事件。L0 同步等待回复，L1/L2/L3 只确认已接收。"""
        task_id = str(uuid.uuid4())
        level = _level_text(request.level)
        try:
            config = self.flow_configs[level]
            event = _event_from_request(request, level)
        except Exception as exc:
            return FlowSubmitResult(
                status="error",
                level=level,
                task_id=task_id,
                error=str(exc),
            )

        if not self._started or self._stopped:
            return FlowSubmitResult(
                status="error",
                level=level,
                task_id=task_id,
                error="flow_runtime_not_running",
            )

        try:
            event = process_event_attachments(
                event,
                data_dir=self.settings.data_dir,
                llm=self.attachment_llm,
                poll_interval_seconds=self.settings.file_upload_poll_interval_seconds,
                wait_timeout_seconds=self.settings.file_upload_timeout_seconds,
            )
        except Exception as exc:
            return FlowSubmitResult(
                status="error",
                level=level,
                task_id=task_id,
                error=str(exc),
            )

        task = _FlowTask(id=task_id, request=request, event=event)
        self._queues[level].put(task)
        if not config.wait_for_result:
            return FlowSubmitResult(status="accepted", level=level, task_id=task_id)

        task.done.wait()
        if task.result is None:
            return FlowSubmitResult(
                status="error",
                level=level,
                task_id=task_id,
                error="flow_task_finished_without_result",
            )
        return task.result

    def _run_worker(self, level: str) -> None:
        task_queue = self._queues[level]
        while True:
            item = task_queue.get()
            try:
                if item is _STOP:
                    return
                task = item
                if not isinstance(task, _FlowTask):
                    continue
                task.result = self._handle_task(level, task)
            except Exception as exc:  # noqa: BLE001 - worker 不能因单条事件退出
                logger.exception("%s 流程处理失败", level)
                if isinstance(item, _FlowTask):
                    item.result = FlowSubmitResult(
                        status="error",
                        level=level,
                        task_id=item.id,
                        error=str(exc),
                    )
            finally:
                if isinstance(item, _FlowTask):
                    item.done.set()
                task_queue.task_done()

    def _handle_task(self, level: str, task: _FlowTask) -> FlowSubmitResult:
        config = self.flow_configs[level]
        if level == "L0":
            result = self.dialogue_service.handle_event(task.event)
            self._dispatch_outputs(config, task, result.message)
            return FlowSubmitResult(
                status="ok",
                level=level,
                task_id=task.id,
                message=result.message,
            )
        if level == "L1":
            result = self.proactive_service.handle_event(task.event)
            self._dispatch_outputs(config, task, result.message)
            return FlowSubmitResult(
                status="ok",
                level=level,
                task_id=task.id,
                message=result.message,
            )

        if level == "L2":
            ingested = self.l2_ingest_service.handle_event(task.event)
            return FlowSubmitResult(
                status="ok",
                level=level,
                task_id=task.id,
                event_id=ingested.event_id,
            )

        ingested = self.l3_ingest_service.handle_event(task.event)
        return FlowSubmitResult(
            status="ok",
            level=level,
            task_id=task.id,
            event_id=ingested.event_id,
        )

    def _dispatch_outputs(
        self,
        config: FlowConfig,
        task: _FlowTask,
        message: str | None,
    ) -> None:
        if not message:
            return
        target = dict(task.request.reply_target or {})
        for name in config.outputs:
            if self.output_dispatcher is None:
                logger.error("输出分发器未配置 name=%s level=%s", name, config.level)
                continue
            try:
                self.output_dispatcher.send(
                    InterfaceOutput(
                        interface=name,
                        target=target,
                        message=message,
                        metadata={
                            "level": config.level,
                            "task_id": task.id,
                            "input_interface": task.request.input_interface,
                        },
                    )
                )
            except Exception:  # noqa: BLE001 - 外发失败不回滚流程结果
                logger.exception("输出接口发送失败 name=%s level=%s", name, config.level)


def default_flow_configs() -> dict[str, FlowConfig]:
    """默认不绑定任何外部输入，仍可通过进程内 submit 调用。"""
    return {
        "L0": FlowConfig("L0", wait_for_result=True),
        "L1": FlowConfig("L1"),
        "L2": FlowConfig("L2"),
        "L3": FlowConfig("L3"),
    }


def input_levels_by_interface(flow_configs: dict[str, FlowConfig]) -> dict[str, str]:
    """返回输入接口到流程层级的映射；重复配置时后者覆盖前者。"""
    levels: dict[str, str] = {}
    for level, config in flow_configs.items():
        for name in config.inputs:
            levels[name] = config.level or level
    return levels


def flow_config_from_mapping(level: str, raw: dict[str, Any]) -> FlowConfig:
    """把配置文件中的流程配置转换成运行时配置。"""
    parsed_level = _level_text(level)
    return FlowConfig(
        level=parsed_level,
        inputs=_string_tuple(raw.get("inputs")),
        outputs=_string_tuple(raw.get("outputs")),
        wait_for_result=_bool(raw.get("wait_for_result"), default=parsed_level == "L0"),
    )


def _event_from_request(
    request: FlowSubmitRequest,
    level: str,
) -> dict[str, Any]:
    metadata = dict(request.metadata or {})
    if request.attachments is not None:
        metadata["attachments"] = request.attachments
    return {
        "source": _clean_str(request.source) or _default_source(request),
        "type": _clean_str(request.type) or default_event_type(level),
        "content": str(request.message or "").strip(),
        "metadata": metadata,
        "attention_level": level,
    }


def default_event_type(level: str) -> str:
    """按层级推断默认事件类型，减少外部接口必须填写的字段。"""
    if level == "L0":
        return "user_message"
    if level == "L1":
        return "l1_trigger"
    return "observation"


def _default_source(request: FlowSubmitRequest) -> str:
    """优先使用输入接口名作为事件来源，方便回溯消息来自哪个外部通道。"""
    return _clean_str(request.input_interface) or "user"


def _string_tuple(value: object) -> tuple[str, ...]:
    """把配置中的 inputs/outputs 规整成字符串元组，不做类型拒绝。"""
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, list | tuple):
        value = (value,)
    return tuple(text for item in value if (text := str(item).strip()))


def _bool(value: object, *, default: bool) -> bool:
    """解析配置里的布尔值；无法识别时回退默认值。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _level_text(value: object) -> str:
    return str(value or "").strip().upper()


def _clean_str(value: object) -> str | None:
    """把可选字符串规整为非空文本或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
