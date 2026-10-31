"""将配置快照转换为模型、Agent、工具和事件运行组件。"""

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from wintermute.agent import AgentRunner, Message, Model, Tool
from wintermute.agent.context import (
    ContextBudget,
    ContextManager,
    MainContextManager,
    ModelSummarizer,
    SubagentContextManager,
)
from wintermute.agent.tools import (
    BackgroundNotificationPublisher,
    BackgroundSubagentManager,
    SubagentConfig,
    create_edit_tool,
    create_exec_tool,
    create_read_tool,
    create_write_tool,
)
from wintermute.config.models import (
    AppConfig,
    ConfigError,
    ContextSettings,
    EventSettings,
    ModelSettings,
    SubagentSettings,
    ToolsSettings,
)
from wintermute.events import (
    ContextAssembler,
    EventGateway,
    OutputRouter,
    RunnerFactory,
    RunScheduler,
    SQLiteStore,
)
from wintermute.events.slices import ModelSliceCompressor, SliceProcessor
from wintermute.events.slices.audio import AudioSliceProcessor
from wintermute.logging import LoggingHandle, configure_logging


def _path(value: str, base_dir: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base_dir).expanduser().resolve() / path
    return path.resolve()


def configure_app_logging(config: AppConfig, *, base_dir: str | Path = ".") -> LoggingHandle:
    """统一应用日志设置，并将当前配置使用的凭据加入日志脱敏集合。"""
    secrets: list[str] = []
    for model in (
        config.model,
        *(item.model for item in config.subagents),
        config.events.slices.audio.summary_model,
    ):
        if model is None:
            continue
        if model.api_key is not None:
            secrets.append(model.api_key.get_secret_value())
        if model.api_key_env:
            value = os.environ.get(model.api_key_env)
            if value:
                secrets.append(value)
    credentials = [
        (terminal.token, terminal.token_env) for terminal in config.interfaces.http.terminals
    ]
    telegram = config.interfaces.telegram
    credentials.append((telegram.bot_token, telegram.bot_token_env))
    asr = config.events.slices.audio.asr
    credentials.extend(
        [
            (asr.api_key, asr.api_key_env),
            (asr.app_key, asr.app_key_env),
            (asr.access_key, asr.access_key_env),
        ]
    )
    for secret, variable in credentials:
        if secret is not None:
            secrets.append(secret.get_secret_value())
        if variable and (value := os.environ.get(variable)):
            secrets.append(value)
    return configure_logging(config.logging, base_dir=base_dir, secrets=secrets)


@asynccontextmanager
async def open_model(settings: ModelSettings) -> AsyncIterator[Model]:
    """按协议创建模型并负责客户端关闭；可选 SDK 仅在调用时加载。"""
    api_key = settings.resolve_api_key()
    try:
        from openai import AsyncOpenAI

        from wintermute.agent.providers import OpenAIChatModel, OpenAIResponsesModel
    except ImportError as exc:
        raise ConfigError("请先通过 uv sync --extra openai 安装模型适配器依赖") from exc
    async with AsyncOpenAI(
        api_key=api_key,
        base_url=settings.base_url,
        timeout=settings.timeout,
        max_retries=settings.max_retries,
    ) as client:
        adapter = OpenAIResponsesModel if settings.api == "responses" else OpenAIChatModel
        yield adapter(client, settings.model, extra_body=settings.extra_body)


@dataclass(frozen=True, slots=True)
class ModelSet:
    """主模型及按角色分配的子模型；同配置可共享支持并发的客户端。"""

    main: Model
    subagents: Mapping[str, Model]
    audio_summary: Model | None = None


@asynccontextmanager
async def open_models(config: AppConfig) -> AsyncIterator[ModelSet]:
    """打开主/子模型，子角色未指定 model 时共享主模型；退出统一关闭。"""
    async with AsyncExitStack() as stack:
        main = await stack.enter_async_context(open_model(config.model))
        subagents: dict[str, Model] = {}
        for settings in config.subagents:
            subagents[settings.name] = (
                main
                if settings.model is None
                else await stack.enter_async_context(open_model(settings.model))
            )
        audio_summary = None
        audio = config.events.slices.audio
        if audio.enabled and audio.summary_model is not None:
            audio_summary = await stack.enter_async_context(open_model(audio.summary_model))
        yield ModelSet(main, subagents, audio_summary)


def create_context_manager(
    settings: ContextSettings,
    model: Model,
    *,
    role: Literal["main", "subagent"] = "main",
) -> ContextManager | None:
    if not settings.enabled:
        return None
    manager = MainContextManager if role == "main" else SubagentContextManager
    return manager(
        ContextBudget(
            context_window=settings.context_window,
            reserve_tokens=settings.reserve_tokens,
            keep_recent_tokens=settings.keep_recent_tokens,
            max_summary_tokens=settings.max_summary_tokens,
        ),
        ModelSummarizer(
            model,
            input_limit=settings.summary_input_limit,
            timeout=settings.summary_timeout,
        ),
    )


def create_tools(settings: ToolsSettings, *, base_dir: str | Path = ".") -> tuple[Tool[Any], ...]:
    """仅构造显式启用的工具，工作目录以配置文件目录为基准。"""
    cwd = _path(settings.cwd, base_dir)
    tools: list[Tool[Any]] = []
    for name in settings.enabled:
        match name:
            case "read":
                tools.append(
                    create_read_tool(
                        cwd,
                        max_lines=settings.max_output_lines,
                        max_bytes=settings.max_output_bytes,
                    )
                )
            case "write":
                tools.append(create_write_tool(cwd))
            case "edit":
                tools.append(
                    create_edit_tool(
                        cwd,
                        max_lines=settings.max_output_lines,
                        max_bytes=settings.max_output_bytes,
                    )
                )
            case "exec":
                tools.append(
                    create_exec_tool(
                        cwd,
                        shell=settings.shell,
                        timeout=settings.command_timeout,
                        max_output_bytes=settings.max_output_bytes,
                        max_output_lines=settings.max_output_lines,
                    )
                )
            case _:
                raise ConfigError("工具名称不在内置工具列表中")
    return tuple(tools)


def create_runner(
    config: AppConfig,
    model: Model,
    *,
    base_dir: str | Path = ".",
    additional_tools: Sequence[Tool[Any]] = (),
    messages: Sequence[Message] = (),
) -> AgentRunner:
    """从快照创建独立主 Runner，自定义工具仍由应用显式注入。"""
    return AgentRunner(
        model,
        system_prompt=config.agent.system_prompt,
        tools=(*create_tools(config.tools, base_dir=base_dir), *additional_tools),
        messages=messages,
        max_turns=config.agent.max_turns,
        tool_timeout=config.agent.tool_timeout,
        model_timeout=config.agent.model_timeout,
        context_manager=create_context_manager(config.context, model),
    )


def create_subagent(
    settings: SubagentSettings, model: Model, *, base_dir: str | Path = "."
) -> SubagentConfig:
    """每次子任务创建独立压缩器，不共享主会话或其他子任务的摘要。"""

    def context_factory() -> ContextManager:
        manager = create_context_manager(settings.context, model, role="subagent")
        assert manager is not None
        return manager

    return SubagentConfig(
        settings.name,
        model,
        description=settings.description,
        system_prompt=settings.agent.system_prompt,
        tools=create_tools(settings.tools, base_dir=base_dir),
        max_turns=settings.agent.max_turns,
        tool_timeout=settings.agent.tool_timeout,
        model_timeout=settings.agent.model_timeout,
        timeout=settings.timeout,
        context_manager_factory=context_factory if settings.context.enabled else None,
    )


def create_background_manager(
    config: AppConfig,
    models: ModelSet,
    *,
    base_dir: str | Path = ".",
    notification_publisher: BackgroundNotificationPublisher | None = None,
) -> BackgroundSubagentManager:
    if not config.subagents:
        raise ConfigError("创建后台管理器需要至少一个子 Agent 配置")
    if any(settings.name not in models.subagents for settings in config.subagents):
        raise ConfigError("子 Agent 缺少对应模型实例")
    return BackgroundSubagentManager(
        [
            create_subagent(settings, models.subagents[settings.name], base_dir=base_dir)
            for settings in config.subagents
        ],
        max_concurrency=config.background.max_concurrency,
        max_tasks=config.background.max_tasks,
        max_output_bytes=config.background.max_output_bytes,
        notification_publisher=notification_publisher,
    )


def create_store(settings: EventSettings, *, base_dir: str | Path = ".") -> SQLiteStore:
    """创建事件存储；调用方使用 with 或 close 管理生命周期。"""
    path: str | Path = ":memory:"
    if settings.database_path != ":memory:":
        path = _path(settings.database_path, base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStore(path, max_event_bytes=settings.max_event_bytes)


def create_scheduler(
    settings: EventSettings,
    gateway: EventGateway,
    runner_factory: RunnerFactory,
    *,
    slice_model: Model | None = None,
    audio_summary_model: Model | None = None,
    base_dir: str | Path = ".",
) -> RunScheduler:
    """将存储之外的事件运行参数集中应用到调度、资料组装和输出组件。"""
    slices = settings.slices
    processor: SliceProcessor | Literal[False] = False
    if slices.enabled:
        if slices.strategy == "model" and slice_model is None:
            raise ConfigError("Model slice compression requires slice_model")
        audio_processor = None
        if slices.audio.enabled:
            if audio_summary_model is None:
                raise ConfigError("Audio slices require a separate audio_summary_model")
            from wintermute.events.slices.asr import VolcengineASR

            audio = slices.audio
            asr_credentials = audio.asr.resolve_credentials()
            audio_processor = AudioSliceProcessor(
                gateway.store,
                VolcengineASR(
                    endpoint=audio.asr.endpoint,
                    resource_id=audio.asr.resource_id,
                    timeout=audio.asr.timeout,
                    max_audio_bytes=audio.max_segment_bytes,
                    api_key=asr_credentials.get("api_key"),
                    app_key=asr_credentials.get("app_key"),
                    access_key=asr_credentials.get("access_key"),
                ),
                ModelSliceCompressor(
                    audio_summary_model,
                    timeout=slices.model_timeout,
                    input_limit_chars=slices.max_input_chars,
                    timezone=slices.timezone,
                ),
                storage_path=_path(audio.storage_path, base_dir),
                max_segment_bytes=audio.max_segment_bytes,
                max_input_chars=slices.max_input_chars,
                max_output_chars=slices.max_output_chars,
                timezone=slices.timezone,
            )
        processor = SliceProcessor(
            gateway.store,
            timezone=slices.timezone,
            batch_size=slices.batch_size,
            max_input_chars=slices.max_input_chars,
            max_output_chars=slices.max_output_chars,
            retry_delay=slices.retry_delay,
            audio_processor=audio_processor,
            compressor=(
                ModelSliceCompressor(
                    slice_model,
                    timeout=slices.model_timeout,
                    input_limit_chars=slices.max_input_chars,
                    timezone=slices.timezone,
                )
                if slices.strategy == "model" and slice_model is not None
                else None
            ),
        )
    return RunScheduler(
        gateway,
        runner_factory,
        assembler=ContextAssembler(
            gateway.store,
            max_reference_bytes=settings.max_reference_bytes,
            max_notifications=settings.max_notifications,
            max_states=settings.max_states,
            max_slice_items=settings.max_slice_items if slices.enabled else 0,
            slice_timezone=slices.timezone,
        ),
        slice_processor=processor,
        output_router=OutputRouter(gateway.store, delivery_timeout=settings.delivery_timeout),
        max_concurrency=settings.max_concurrency,
        poll_interval=settings.poll_interval,
        lease_seconds=settings.lease_seconds,
        delivery_retry_interval=settings.delivery_retry_interval,
    )


__all__ = [
    "ModelSet",
    "configure_app_logging",
    "create_background_manager",
    "create_context_manager",
    "create_runner",
    "create_scheduler",
    "create_store",
    "create_subagent",
    "create_tools",
    "open_model",
    "open_models",
]
