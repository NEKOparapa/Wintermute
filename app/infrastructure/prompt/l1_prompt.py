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
from .messages import build_event_input_message
from .types import PromptContent

_L1_SYSTEM_PROMPT = """你是一个本地运行的隐形个人家庭管理助手，当前正在处理 L1 主动唤醒事件。

L1 处理原则：
- 使用用户的语言。
- 只判断当前主动事件对用户是否有用，并给出可直接通知用户的简洁结果。
- 可以参考最近 L0 对话了解用户正在做什么，但不要延续 L0 对话语气。
- 不把当前事件当作用户提问。
- 不描述你的处理步骤。
"""


def build_l1_messages(
    event_date: date,
    active_event: dict[str, object],
) -> PromptContent:
    """构建 L1 主动唤醒 prompt；读取 L0 最近对话，但不复用 L0 对话流程。"""
    settings = get_settings()
    event_store = GlobalEventStore(settings.data_dir)
    memory_store = MemoryStore(settings.data_dir)
    schedule_items = schedule_prompt_items(settings.data_dir, event_date)

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

    selected_memories = select_memories(memory_store.load_all_memories(), today=event_date)
    l2_event_memories = sorted_event_memories(
        memory_store.load_l2_event_memories(event_date.isoformat())
    )
    l3_event_memories = sorted_event_memories(
        memory_store.load_l3_event_memories(event_date.isoformat())
    )
    l1_context_memories = sorted_event_memories(
        memory_store.load_l1_context_memories(event_date.isoformat())
    )
    l0_recent_events = recent_today_events(
        event_store.load_events_for_date(event_date),
        today=event_date,
        recent_turns=settings.prompt_recent_turns,
    )

    prompt = build_prompt_with_budget(
        selected_memories,
        l0_recent_events,
        identity=identity,
        user_profile=user_profile,
        system_prompt=_L1_SYSTEM_PROMPT,
        l2_event_memories=l2_event_memories,
        l3_event_memories=l3_event_memories,
        l1_context_memories=l1_context_memories,
        schedule_items=schedule_items,
        active_l1_event=active_event,
        token_budget=settings.prompt_token_budget,
    )
    return PromptContent(
        system=prompt.system,
        messages=[
            *prompt.messages,
            build_event_input_message(
                "请处理系统提示中的当前 L1 主动触发事件。",
                active_event,
            ),
        ],
    )
