"""分层流程运行时。

外部接口、定时任务或进程内调用方把输入包装成 FlowSubmitRequest 后提交到这里。
运行时按注意力层 L0/L1/L2/L3 分别排队处理，保证同一层内有序，同时避免低优先级
背景事件阻塞用户主动对话。
"""

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

    # level 决定进入哪条处理链：L0 用户对话、L1 主动触发、L2/L3 背景事件。
    level: str
    # message 和 attachments 最终会被 normalize_event 标准化；两者至少应有一个有效内容。
    message: str | None = None
    attachments: list[dict[str, Any]] | None = None
    # source/type 会写入事件历史；未提供时根据接口名和层级推断默认值。
    source: str = "user"
    type: str | None = None
    # input_interface 用于校验该外部接口是否允许作为当前层级的输入。
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
    """单个注意力层的输入输出配置。"""

    level: AttentionLevel
    # inputs/outputs 存接口名；validate_flow_adapter_config 会保证它们都已启用。
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    # 只有 L0 允许同步等待最终回复，其他层必须快速返回 accepted。
    wait_for_result: bool = False


@dataclass
class _FlowTask:
    """运行时内部任务对象，用 done/result 在 submit 线程和 worker 线程之间同步。"""

    id: str
    request: FlowSubmitRequest
    event: StandardEvent
    done: threading.Event = field(default_factory=threading.Event)
    result: FlowSubmitResult | None = None


_STOP = object()


class FlowRuntime:
    """L0/L1/L2/L3 分层流程运行时，每层一个有序队列和 worker 线程。

    设计上只让 worker 线程执行实际业务服务，submit 线程负责校验、标准化和排队。
    这样外部接口不需要知道 L0/L1/L2/L3 的具体处理细节，也不会直接调用业务服务。
    """

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
        # 三个服务分别承载不同层级的业务逻辑，运行时只负责路由和生命周期。
        self.dialogue_service = dialogue_service
        self.proactive_service = proactive_service
        self.ingest_service = ingest_service
        self.flow_configs = flow_configs or default_flow_configs()
        self.output_dispatcher = output_dispatcher
        self.interface_names = frozenset(interface_names)

        # 启动前即校验配置，尽早暴露“接口名写错”或“层级返回语义错误”等问题。
        validate_flow_adapter_config(self.flow_configs, self.interface_names)

        # 每个 AttentionLevel 一个队列，保证同层事件 FIFO；不同层之间并行处理。
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
            # start/stop 由锁保护，避免接口或测试环境里重复启动造成多个 worker 消费同队列。
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
            # 用哨兵对象唤醒阻塞在 get() 的 worker；每个队列放一个即可停止对应线程。
            for task_queue in self._queues.values():
                task_queue.put(_STOP)
        for thread in self._threads.values():
            thread.join(timeout=timeout)

    def submit(self, request: FlowSubmitRequest) -> FlowSubmitResult:
        """提交输入事件。L0 同步等待回复，L1/L2/L3 只确认已接收。"""
        task_id = str(uuid.uuid4())
        try:
            # 先解析层级和校验接口，再构造标准事件；任何输入错误都以 error 结果返回。
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
            # 运行时未就绪时不入队，避免任务永远没有 worker 消费。
            return FlowSubmitResult(
                status="error",
                level=level.value,
                task_id=task_id,
                error="flow_runtime_not_running",
            )

        task = _FlowTask(id=task_id, request=request, event=event)
        self._queues[level].put(task)
        if not config.wait_for_result:
            # L1/L2/L3 的调用方只需要知道任务已接收；实际结果由后台 worker 记录日志/落库。
            return FlowSubmitResult(status="accepted", level=level.value, task_id=task_id)

        # L0 需要把模型回复同步返回给接口，所以 submit 线程在这里等待 worker 填充 result。
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
        # 只有声明了 input_interface 的外部输入才检查白名单；进程内调用可不绑定接口。
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
                # worker 是唯一执行层级业务服务的位置，保证 submit 侧不直接承担耗时逻辑。
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
                    # 无论成功还是异常，都唤醒可能正在等待同步结果的 submit 线程。
                    item.done.set()
                task_queue.task_done()

    def _handle_task(self, level: AttentionLevel, task: _FlowTask) -> FlowSubmitResult:
        config = self.flow_configs[level]
        if level is AttentionLevel.L0:
            # L0 是用户主动对话：需要生成可见回复，并按 outputs 配置回发到外部接口。
            result = self.dialogue_service.handle_event(task.event)
            self._dispatch_outputs(config, task, result.message)
            return FlowSubmitResult(
                status="ok",
                level=level.value,
                task_id=task.id,
                message=result.message,
            )
        if level is AttentionLevel.L1:
            # L1 是主动唤醒：后台处理完成后可按配置外发，但 submit 默认已经返回 accepted。
            result = self.proactive_service.handle_event(task.event)
            self._dispatch_outputs(config, task, result.message)
            return FlowSubmitResult(
                status="ok",
                level=level.value,
                task_id=task.id,
                message=result.message,
            )

        # L2/L3 是背景观察事件：只落库并压缩，不产生面向用户的直接回复。
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
        # 同一条回复可分发给多个输出接口；target 由输入接口提供，运行时只透传。
        target = dict(task.request.reply_target or {})
        for name in config.outputs:
            if self.output_dispatcher is None:
                logger.error("输出分发器未配置 name=%s level=%s", name, config.level.value)
                continue
            try:
                # 外发 metadata 便于适配器或日志追踪“哪一层、哪次任务、来自哪个输入接口”。
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
                # 模型处理和事件落库已经完成，单个输出失败只记录日志，不让整条流程失败。
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
    # 四个层级必须都有配置，避免运行时 submit 某个层级时才 KeyError。
    for level in AttentionLevel:
        if level not in flow_configs:
            raise RuntimeConfigError(f"缺少流程配置: {level.value}")
    for config in flow_configs.values():
        # 返回语义是对外契约：L0 同步回复；L1/L2/L3 异步 accepted。
        if config.level is AttentionLevel.L0 and not config.wait_for_result:
            raise RuntimeConfigError("L0 必须同步等待回复结果。")
        if config.level is not AttentionLevel.L0 and config.wait_for_result:
            raise RuntimeConfigError(f"{config.level.value} 必须异步返回 accepted。")
        # inputs/outputs 都只能引用当前已启用的接口，避免消息进入后才发现无法收发。
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
            # 一个输入接口只能对应一个注意力层，否则适配器收到消息后无法判断投递目标。
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
    # 配置缺省时仍保持运行时契约：L0 同步，其余层异步。
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
    # normalize_event 统一校验文本/附件，并形成后续事件存储与模型输入都能理解的结构。
    return normalize_event(
        request.message,
        request.attachments,
        source=_clean_str(request.source) or _default_source(request),
        type=_clean_str(request.type) or default_event_type(level),
        attention_level=level.value,
        metadata=request.metadata,
    )


def default_event_type(level: AttentionLevel) -> str:
    """按层级推断默认事件类型，减少外部接口必须填写的字段。"""
    if level is AttentionLevel.L0:
        return "user_message"
    if level is AttentionLevel.L1:
        return "l1_trigger"
    return "observation"


def _default_source(request: FlowSubmitRequest) -> str:
    """优先使用输入接口名作为事件来源，方便回溯消息来自哪个外部通道。"""
    return _clean_str(request.input_interface) or "user"


def _string_tuple(value: object) -> tuple[str, ...]:
    """把配置中的 inputs/outputs 规范成字符串元组。"""
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
    """解析配置里的布尔值，兼容 JSON bool 和常见字符串写法。"""
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
    """把可选字符串规整为非空文本或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
