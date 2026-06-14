from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..attention.attention import AttentionLevel, parse_level
from ..event.event import StandardEvent, normalize_event
from .dialogue import DialogueService
from .ingest import EventIngestService
from .proactive import L1ProactiveService

logger = logging.getLogger(__name__)


class RuntimeConfigError(ValueError):
    """流程运行时配置无效。"""


@dataclass(frozen=True)
class FlowSubmitRequest:
    """外部接口提交给分层流程的一条输入。"""

    level: str
    message: str | None = None
    attachments: list[dict[str, Any]] | None = None
    source: str = "user"
    type: str | None = None
    input_interface: str | None = None
    reply_target: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowSubmitResult:
    """流程提交结果。L0 返回最终回复，其他层默认只返回 accepted。"""

    status: str
    level: str
    task_id: str | None = None
    message: str | None = None
    event_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class InterfaceOutput:
    """流程发送给外部接口的一条输出。"""

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
    """单个注意力层的输入输出配置。"""

    level: AttentionLevel
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    wait_for_result: bool = False


@dataclass
class _FlowTask:
    id: str
    request: FlowSubmitRequest
    event: StandardEvent
    done: threading.Event = field(default_factory=threading.Event)
    result: FlowSubmitResult | None = None


_STOP = object()


class FlowRuntime:
    """L0/L1/L2/L3 分层流程运行时，每层一个有序队列和 worker 线程。"""

    def __init__(
        self,
        dialogue_service: DialogueService,
        proactive_service: L1ProactiveService,
        ingest_service: EventIngestService,
        *,
        flow_configs: dict[AttentionLevel, FlowConfig] | None = None,
        output_dispatcher: OutputDispatcher | None = None,
        interface_names: Iterable[str] = (),
    ) -> None:
        self.dialogue_service = dialogue_service
        self.proactive_service = proactive_service
        self.ingest_service = ingest_service
        self.flow_configs = flow_configs or default_flow_configs()
        self.output_dispatcher = output_dispatcher
        self.interface_names = frozenset(interface_names)
        validate_flow_adapter_config(self.flow_configs, self.interface_names)
        self._queues: dict[AttentionLevel, queue.Queue[_FlowTask | object]] = {
            level: queue.Queue() for level in AttentionLevel
        }
        self._threads: dict[AttentionLevel, threading.Thread] = {}
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
            for level in AttentionLevel:
                thread = threading.Thread(
                    target=self._run_worker,
                    args=(level,),
                    name=f"wintermute-{level.value}-worker",
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
        try:
            level = parse_level(request.level)
            config = self.flow_configs[level]
            self._validate_submit_interface(config, request)
            event = _event_from_request(request, level)
        except Exception as exc:
            return FlowSubmitResult(
                status="error",
                level=str(request.level or ""),
                task_id=task_id,
                error=str(exc),
            )

        if not self._started or self._stopped:
            return FlowSubmitResult(
                status="error",
                level=level.value,
                task_id=task_id,
                error="flow_runtime_not_running",
            )

        task = _FlowTask(id=task_id, request=request, event=event)
        self._queues[level].put(task)
        if not config.wait_for_result:
            return FlowSubmitResult(status="accepted", level=level.value, task_id=task_id)

        task.done.wait()
        if task.result is None:
            return FlowSubmitResult(
                status="error",
                level=level.value,
                task_id=task_id,
                error="flow_task_finished_without_result",
            )
        return task.result

    def _validate_submit_interface(
        self,
        config: FlowConfig,
        request: FlowSubmitRequest,
    ) -> None:
        if request.input_interface and request.input_interface not in config.inputs:
            raise RuntimeConfigError(
                f"{config.level.value} 未配置输入接口: {request.input_interface}"
            )

    def _run_worker(self, level: AttentionLevel) -> None:
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
                logger.exception("%s 流程处理失败", level.value)
                if isinstance(item, _FlowTask):
                    item.result = FlowSubmitResult(
                        status="error",
                        level=level.value,
                        task_id=item.id,
                        error=str(exc),
                    )
            finally:
                if isinstance(item, _FlowTask):
                    item.done.set()
                task_queue.task_done()

    def _handle_task(self, level: AttentionLevel, task: _FlowTask) -> FlowSubmitResult:
        config = self.flow_configs[level]
        if level is AttentionLevel.L0:
            result = self.dialogue_service.handle_event(task.event)
            self._dispatch_outputs(config, task, result.message)
            return FlowSubmitResult(
                status="ok",
                level=level.value,
                task_id=task.id,
                message=result.message,
            )
        if level is AttentionLevel.L1:
            result = self.proactive_service.handle_event(task.event)
            self._dispatch_outputs(config, task, result.message)
            return FlowSubmitResult(
                status="ok",
                level=level.value,
                task_id=task.id,
                message=result.message,
            )

        ingested = self.ingest_service.handle_event(task.event)
        return FlowSubmitResult(
            status="ok",
            level=level.value,
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
                logger.error("输出分发器未配置 name=%s level=%s", name, config.level.value)
                continue
            try:
                self.output_dispatcher.send(
                    InterfaceOutput(
                        interface=name,
                        target=target,
                        message=message,
                        metadata={
                            "level": config.level.value,
                            "task_id": task.id,
                            "input_interface": task.request.input_interface,
                        },
                    )
                )
            except Exception:  # noqa: BLE001 - 外发失败不回滚流程结果
                logger.exception("输出接口发送失败 name=%s level=%s", name, config.level.value)


def default_flow_configs() -> dict[AttentionLevel, FlowConfig]:
    """默认不绑定任何外部输入，仍可通过进程内 submit 调用。"""
    return {
        AttentionLevel.L0: FlowConfig(AttentionLevel.L0, wait_for_result=True),
        AttentionLevel.L1: FlowConfig(AttentionLevel.L1),
        AttentionLevel.L2: FlowConfig(AttentionLevel.L2),
        AttentionLevel.L3: FlowConfig(AttentionLevel.L3),
    }


def validate_flow_adapter_config(
    flow_configs: dict[AttentionLevel, FlowConfig],
    interface_names: Iterable[str],
) -> None:
    """校验流程引用的输入输出接口都已启用并注册。"""
    names = frozenset(interface_names)
    for level in AttentionLevel:
        if level not in flow_configs:
            raise RuntimeConfigError(f"缺少流程配置: {level.value}")
    for config in flow_configs.values():
        if config.level is AttentionLevel.L0 and not config.wait_for_result:
            raise RuntimeConfigError("L0 必须同步等待回复结果。")
        if config.level is not AttentionLevel.L0 and config.wait_for_result:
            raise RuntimeConfigError(f"{config.level.value} 必须异步返回 accepted。")
        for name in (*config.inputs, *config.outputs):
            if name not in names:
                raise RuntimeConfigError(
                    f"{config.level.value} 引用了未启用的接口: {name}"
                )
    input_levels_by_interface(flow_configs)


def input_levels_by_interface(
    flow_configs: dict[AttentionLevel, FlowConfig],
) -> dict[str, AttentionLevel]:
    """返回输入接口到流程层级的映射，并拒绝同一接口绑定多个层级。"""
    levels: dict[str, AttentionLevel] = {}
    for level, config in flow_configs.items():
        for name in config.inputs:
            previous = levels.get(name)
            if previous is not None and previous is not level:
                raise RuntimeConfigError(
                    f"输入接口 {name} 同时绑定到 {previous.value} 和 {level.value}"
                )
            levels[name] = level
    return levels


def flow_config_from_mapping(level: str, raw: dict[str, Any]) -> FlowConfig:
    """把配置文件中的流程配置转换成运行时配置。"""
    parsed_level = parse_level(level)
    default_wait = parsed_level is AttentionLevel.L0
    return FlowConfig(
        level=parsed_level,
        inputs=_string_tuple(raw.get("inputs")),
        outputs=_string_tuple(raw.get("outputs")),
        wait_for_result=_bool(raw.get("wait_for_result"), default=default_wait),
    )


def _event_from_request(
    request: FlowSubmitRequest,
    level: AttentionLevel,
) -> StandardEvent:
    return normalize_event(
        request.message,
        request.attachments,
        source=_clean_str(request.source) or _default_source(request),
        type=_clean_str(request.type) or default_event_type(level),
        attention_level=level.value,
        metadata=request.metadata,
    )


def default_event_type(level: AttentionLevel) -> str:
    if level is AttentionLevel.L0:
        return "user_message"
    if level is AttentionLevel.L1:
        return "l1_trigger"
    return "observation"


def _default_source(request: FlowSubmitRequest) -> str:
    return _clean_str(request.input_interface) or "user"


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, list | tuple):
        raise RuntimeConfigError("inputs/outputs 必须是字符串数组。")
    items = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return tuple(items)


def _bool(value: object, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise RuntimeConfigError(f"无法解析布尔配置: {value}")


def _clean_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
