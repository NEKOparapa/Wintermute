"""按配置打开模型客户端，并统一管理凭据与关闭顺序。"""

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from wintermute.agent import Model
from wintermute.config.api_keys import APIKeyRegistry, subagent_slot
from wintermute.config.models import (
    AppConfig,
    ConfigError,
    ModelSettings,
)


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
    image_model: Model | None = None
    timeline_summary: Model | None = None
    api_keys: APIKeyRegistry | None = None
    memory_model: Model | None = None


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
        image_settings = config.events.slices.image
        image_model = None
        if image_settings.enabled and image_settings.model is not None:
            image_model = await model_for(image_settings.model, "vision")
        timeline = config.events.timeline_compression
        timeline_summary = None
        if timeline.ai_enabled and timeline.summary_model is not None:
            timeline_summary = await model_for(timeline.summary_model, "timeline")
        memory_model = None
        memory = config.events.memory
        if memory.enabled and memory.user_dirs and memory.model is not None:
            memory_model = await model_for(memory.model, "memory")
        yield ModelSet(
            main=main,
            subagents=subagents,
            image_model=image_model,
            timeline_summary=timeline_summary,
            api_keys=keys,
            memory_model=memory_model,
        )
