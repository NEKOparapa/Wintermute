"""将配置快照转换为模型、Agent、工具和事件运行组件。"""

import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
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
from wintermute.config.api_keys import APIKeyRegistry, subagent_slot
from wintermute.config.models import (
    AppConfig,
    ConfigError,
    ContextSettings,
    EventSettings,
    ModelSettings,
    SliceSettings,
    SubagentSettings,
    ToolsSettings,
)
from wintermute.events import (
    EventGateway,
    OutputRouter,
    RunnerFactory,
    RunScheduler,
    SQLiteStore,
)
from wintermute.events.core.timeline_compression import TimelinePolicy
from wintermute.events.runtime.profile_worker import ProfileWorker
from wintermute.events.runtime.timeline_worker import TimelineWorker
from wintermute.events.slices import SliceProcessor
from wintermute.events.slices.audio import AudioSliceProcessor
from wintermute.events.slices.image import ImageSliceProcessor
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
        config.events.slices.image.model,
        config.events.timeline_compression.summary_model,
        config.events.profile_dreaming.summary_model,
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
    credentials.append((asr.api_key, asr.api_key_env))
    for secret, variable in credentials:
        if secret is not None:
            secrets.append(secret.get_secret_value())
        if variable and (value := os.environ.get(variable)):
            secrets.append(value)
    return configure_logging(config.logging, base_dir=base_dir, secrets=secrets)


@asynccontextmanager
async def open_model(
    settings: ModelSettings,
    *,
    api_key: Callable[[], str] | None = None,
    allow_missing: bool = False,
) -> AsyncIterator[Model]:
    """按协议创建模型并负责客户端关闭；可选 SDK 仅在调用时加载。"""
    try:
        from openai import AsyncOpenAI

        from wintermute.agent.providers import OpenAIChatModel, OpenAIResponsesModel
    except ImportError as exc:
        raise ConfigError("请先通过 uv sync --extra openai 安装模型适配器依赖") from exc
    async with AsyncOpenAI(
        # The managed client always supplies Authorization on the individual request.
        # Do not resolve here: a missing credential can be provisioned after startup.
        api_key="per-request"
        if api_key and allow_missing
        else (api_key or settings.resolve_api_key)(),
        base_url=settings.base_url,
        timeout=settings.timeout,
        max_retries=settings.max_retries,
    ) as client:
        adapter = OpenAIResponsesModel if settings.api == "responses" else OpenAIChatModel
        yield adapter(client, settings.model, extra_body=settings.extra_body, api_key=api_key)


@dataclass(frozen=True, slots=True)
class ModelSet:
    """主模型、各子 Agent 模型，以及可选的媒体和时间线模型。"""

    main: Model
    subagents: Mapping[str, Model]
    audio_summary: Model | None = None
    image_model: Model | None = None
    timeline_summary: Model | None = None
    profile_summary: Model | None = None
    api_keys: APIKeyRegistry | None = None


@asynccontextmanager
async def open_models(config: AppConfig, *, base_dir: str | Path = ".") -> AsyncIterator[ModelSet]:
    """打开主模型、子模型及已启用的媒体处理模型，退出时统一关闭。"""
    async with AsyncExitStack() as stack:
        keys = APIKeyRegistry(config, base_dir=base_dir)

        async def model_for(settings: ModelSettings, slot: str) -> Model:
            return await stack.enter_async_context(
                open_model(settings, api_key=keys.provider(slot), allow_missing=keys.can_provision)
            )

        main = await model_for(config.model, "main")
        subagents: dict[str, Model] = {}
        for settings in config.subagents:
            subagents[settings.name] = (
                main
                if settings.model is None
                else await model_for(settings.model, subagent_slot(settings.name))
            )
        audio_summary = None
        audio = config.events.slices.audio
        if audio.enabled and audio.summary_model is not None:
            audio_summary = await model_for(audio.summary_model, "audio-summary")
        image_settings = config.events.slices.image
        image_model = None
        if image_settings.enabled and image_settings.model is not None:
            image_model = await model_for(image_settings.model, "vision")
        timeline = config.events.timeline_compression
        timeline_summary = None
        if timeline.ai_enabled and timeline.summary_model is not None:
            timeline_summary = await model_for(timeline.summary_model, "timeline")
        dreaming = config.events.profile_dreaming
        profile_summary = None
        if config.events.profile_files and dreaming.enabled and dreaming.summary_model is not None:
            profile_summary = await model_for(dreaming.summary_model, "profile")
        yield ModelSet(
            main, subagents, audio_summary, image_model, timeline_summary, profile_summary, keys
        )


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
    timeline = settings.timeline_compression
    policy = TimelinePolicy(
        raw_tokens=timeline.raw_tokens,
        ai_threshold=timeline.ai_threshold,
        summary_tokens=timeline.summary_tokens,
        delivery_tokens=timeline.delivery_tokens,
        idle_seconds=timeline.idle_seconds,
        timezone=settings.slices.timezone,
    )
    return SQLiteStore(path, max_event_bytes=settings.max_event_bytes, timeline_policy=policy)


def create_image_processor(
    settings: SliceSettings,
    image_model: Model | None,
    *,
    base_dir: str | Path = ".",
) -> ImageSliceProcessor | None:
    image = settings.image
    if not image.enabled:
        return None
    if image_model is None or image.model is None:
        raise ConfigError("Screenshot description requires a separate image_model")
    return ImageSliceProcessor(
        image_model,
        _path(image.storage_path, base_dir),
        timeout=image.model.timeout,
        max_chars=settings.max_output_chars,
    )


def create_scheduler(
    settings: EventSettings,
    gateway: EventGateway,
    runner_factory: RunnerFactory,
    *,
    slice_model: Model | None = None,
    audio_summary_model: Model | None = None,
    image_model: Model | None = None,
    image_processor: ImageSliceProcessor | None = None,
    timeline_summary_model: Model | None = None,
    profile_model: Model | None = None,
    api_keys: APIKeyRegistry | None = None,
    base_dir: str | Path = ".",
) -> RunScheduler:
    """将存储之外的事件运行参数集中应用到调度、资料组装和输出组件。"""
    slices = settings.slices
    processor: SliceProcessor | Literal[False] = False
    if slices.enabled:
        audio_processor = None
        if slices.audio.enabled:
            from wintermute.events.slices.asr import DeepSeekASR

            audio = slices.audio
            asr_key = api_keys.provider("asr") if api_keys else audio.asr.resolve_api_key
            audio_processor = AudioSliceProcessor(
                gateway.store,
                DeepSeekASR(
                    endpoint=audio.asr.endpoint,
                    api_key=asr_key if api_keys and api_keys.can_provision else asr_key(),
                    timeout=audio.asr.timeout,
                    max_audio_bytes=audio.max_segment_bytes,
                ),
                storage_path=_path(audio.storage_path, base_dir),
                max_segment_bytes=audio.max_segment_bytes,
                max_input_chars=slices.max_input_chars,
                max_output_chars=slices.max_output_chars,
                timezone=slices.timezone,
            )
        if image_processor is None:
            image_processor = create_image_processor(slices, image_model, base_dir=base_dir)
        processor = SliceProcessor(
            gateway.store,
            timezone=slices.timezone,
            batch_size=slices.batch_size,
            max_input_chars=slices.max_input_chars,
            max_output_chars=slices.max_output_chars,
            retry_delay=slices.retry_delay,
            audio_processor=audio_processor,
            image_processor=image_processor,
        )
    timeline = settings.timeline_compression
    if (
        timeline.ai_enabled
        and timeline.summary_model is not None
        and timeline_summary_model is None
    ):
        raise ConfigError("Configured timeline compression model requires timeline_summary_model")
    timeline_model = timeline_summary_model or audio_summary_model or slice_model
    files = {user: _path(path, base_dir) for user, path in settings.profile_files.items()}
    if len(set(files.values())) != len(files):
        raise ConfigError("Profiles require a separate file per user")
    profile_worker = None
    if settings.profile_dreaming.enabled and files:
        if settings.profile_dreaming.summary_model is not None and profile_model is None:
            raise ConfigError("Configured profile dreaming model requires profile_model")
        model = profile_model or slice_model
        if model is not None:
            profile_worker = ProfileWorker(
                gateway.store, files, model, settings.profile_dreaming, timezone=slices.timezone
            )
    return RunScheduler(
        gateway,
        runner_factory,
        profile_files=files,
        profile_worker=profile_worker,
        slice_processor=processor,
        timeline_worker=TimelineWorker(
            gateway.store,
            timeline_model if timeline.ai_enabled else None,
            model_timeout=timeline.model_timeout,
        ),
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
    "create_image_processor",
    "create_runner",
    "create_scheduler",
    "create_store",
    "create_subagent",
    "create_tools",
    "open_model",
    "open_models",
]
