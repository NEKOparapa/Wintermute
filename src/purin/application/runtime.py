"""组装事件存储、媒体处理、记忆后台任务和会话调度器。"""

from pathlib import Path
from typing import Literal

from purin.agent import Model
from purin.application.paths import _memory_root, _path
from purin.config.api_keys import APIKeyRegistry
from purin.config.models import (
    ConfigError,
    EventSettings,
    SliceSettings,
)
from purin.events.core.timeline_compression import TimelinePolicy
from purin.events.ingress import EventGateway
from purin.events.runtime import OutputRouter, RunnerFactory, RunScheduler
from purin.events.runtime.memory_worker import MemoryWorker
from purin.events.runtime.timeline_worker import TimelineWorker
from purin.events.slices.audio import AudioSliceProcessor
from purin.events.slices.image import ImageSliceProcessor
from purin.events.slices.processor import SliceProcessor
from purin.events.storage.sqlite import SQLiteStore
from purin.memory import MemoryVault


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
    return SQLiteStore(
        path,
        max_event_bytes=settings.max_event_bytes,
        timeline_policy=policy,
        records_storage_path=_path(settings.memory.records_storage_path, base_dir),
    )


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
    main_model: Model | None = None,
    image_model: Model | None = None,
    image_processor: ImageSliceProcessor | None = None,
    timeline_summary_model: Model | None = None,
    memory_model: Model | None = None,
    api_keys: APIKeyRegistry | None = None,
    base_dir: str | Path = ".",
) -> RunScheduler:
    """将存储之外的事件运行参数集中应用到调度、资料组装和输出组件。"""
    slices = settings.slices
    processor: SliceProcessor | Literal[False] = False
    if slices.enabled:
        audio_processor = None
        if slices.audio.enabled:
            from purin.events.slices.asr import DeepSeekASR

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
            )
        if image_processor is None:
            image_processor = create_image_processor(slices, image_model, base_dir=base_dir)
        processor = SliceProcessor(
            gateway.store,
            timezone=slices.timezone,
            batch_size=slices.batch_size,
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
    timeline_model = timeline_summary_model or main_model
    memory = settings.memory
    vaults: dict[str, MemoryVault] = {}
    if memory.enabled:
        roots = {user: _memory_root(path, base_dir) for user, path in memory.user_dirs.items()}
        paths = list(roots.values())
        if any(
            left.is_relative_to(right) or right.is_relative_to(left)
            for index, left in enumerate(paths)
            for right in paths[index + 1 :]
        ):
            raise ConfigError("Memory directories must be separate and cannot overlap")
        for user, root in roots.items():
            vault = MemoryVault(
                root, timezone=slices.timezone, git_snapshot_enabled=memory.git_snapshot_enabled
            )
            vault.initialize()
            vault.retire_generated_activity()
            vault.initialize_templates()
            vaults[user] = vault
    memory_worker = None
    if vaults:
        if memory.model is not None and memory_model is None:
            raise ConfigError("Configured memory model requires memory_model")
        model = memory_model or main_model
        if model is None:
            raise ConfigError("Enabled memory directories require a memory or main model")
        memory_worker = MemoryWorker(gateway.store, vaults, model, memory, timezone=slices.timezone)
    return RunScheduler(
        gateway,
        runner_factory,
        memory_vaults=vaults,
        memory_worker=memory_worker,
        memory_prompt_budgets=memory.prompt_budgets,
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
