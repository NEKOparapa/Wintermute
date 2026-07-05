from __future__ import annotations

from datetime import date

from ...config.config import get_settings
from ..storage.profile_store import ProfileStore
from ..storage.storage import GlobalEventStore, MemoryStore
from .context import (
    build_prompt_with_budget,
    identity_block,
    recent_today_events,
    schedule_prompt_items,
    select_memories,
    sorted_event_memories,
)
from .types import PromptContent

_L0_SYSTEM_PROMPT = """你是一个本地运行的隐形个人家庭管理助手。

安静运行：
- 使用用户的语言回复。
- 简洁直接。
- 只报告状态，不描述过程。
- 不叙述你的处理步骤。
- 不用“还有什么需要吗？”这类泛化收尾。
- 用户的确认不需要再次确认。
"""


def build_l0_messages(
    event_date: date,
) -> PromptContent:
    """构建 L0 用户主动对话 prompt。"""
    settings = get_settings()
    event_store = GlobalEventStore(settings.data_dir)
    memory_store = MemoryStore(settings.data_dir)
    schedule_items = schedule_prompt_items(settings.data_dir, event_date)

    # 长期画像（soul/persona/user）作为固定身份上下文，始终注入且不参与 token 裁剪。
    identity = ""
    user_profile = ""
    if settings.profile_enabled:
        profile_store = ProfileStore(
            settings.data_dir,
            soul_path=settings.soul_path,
            persona_template_path=settings.persona_template_path,
            user_template_path=settings.user_template_path,
        )
        identity = identity_block(profile_store)
        user_profile = profile_store.read_user().strip()

    # 选择记忆和事件，优先保证近期对话完整，再尽可能多地带入长期记忆，最后裁剪到 token 预算内。
    selected_memories = select_memories(memory_store.load_all_memories(), today=event_date)

    # 当天的 L2 和 L3 背景事件记忆分别注入，避免不同层级互相混在同一上下文块。
    l2_event_memories = sorted_event_memories(
        memory_store.load_l2_event_memories(event_date.isoformat())
    )
    l3_event_memories = sorted_event_memories(
        memory_store.load_l3_event_memories(event_date.isoformat())
    )
    l1_context_memories = sorted_event_memories(
        memory_store.load_l1_context_memories(event_date.isoformat())
    )

    # 最近 L0 对话事件只保留当天的，并且优先保证最近几轮完整。
    raw_events = recent_today_events(
        event_store.load_events_for_date(event_date),
        today=event_date,
        recent_turns=settings.prompt_recent_turns,
    )

    # 组装成提示内容，并裁剪到 token 预算内。
    return build_prompt_with_budget(
        selected_memories,
        raw_events,
        identity=identity,
        user_profile=user_profile,
        system_prompt=_L0_SYSTEM_PROMPT,
        l2_event_memories=l2_event_memories,
        l3_event_memories=l3_event_memories,
        l1_context_memories=l1_context_memories,
        schedule_items=schedule_items,
        token_budget=settings.prompt_token_budget,
    )


def build_messages(event_date: date) -> PromptContent:
    """兼容旧调用；等价于构建 L0 prompt。"""
    return build_l0_messages(event_date)
