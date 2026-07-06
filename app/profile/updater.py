from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from ..config.config import Settings
from ..infrastructure.llm.llm import OpenAICompatibleLLM
from ..infrastructure.storage.profile_store import ProfileStore
from ..infrastructure.storage.storage import MemoryStore

logger = logging.getLogger(__name__)

_NO_CHANGE = "NO_CHANGE"

_USER_SYSTEM = """你负责维护一份长期、稳定的"用户画像"Markdown 文档，供 AI 在未来对话中了解用户。

要求：
- 输出更新后的完整 Markdown 全文，保留既有的小节标题结构。
- 合并新的稳定事实（称呼、长期偏好、进行中的事项、关系、约束等），并修正过时信息。
- 用户最近、明确的陈述优先于旧的推断。
- 只记录长期有用的稳定信息，忽略一次性的琐碎细节与寒暄。
- 不编造证据中没有的信息。
- 控制篇幅，超出预算时删除最不重要的内容。
- 如果新证据没有带来任何需要写入的稳定信息，只输出 NO_CHANGE。
"""

_SOUL_SYSTEM = """你负责维护 AI 的"统一人格（Soul）"Markdown 文档，记录 AI 的身份、价值观、边界、语气与长期沟通习惯。

要求：
- 输出更新后的完整 Markdown 全文，保留既有的小节标题结构。
- 保留身份、价值观和底线等核心边界，不因短期证据剧烈改变。
- 人格应缓慢演化：只在出现明确且反复的信号时才调整。
- 优先采纳用户对 AI 风格的显式反馈。
- 不编造证据中没有的信息。
- 控制篇幅，超出预算时删除最不重要的内容。
- 如果本周证据不足以稳定地调整 Soul，只输出 NO_CHANGE。
"""


@dataclass(frozen=True)
class UpdateResult:
    """一次画像刷新的结果。"""

    updated: bool
    reason: str = ""


class ProfileUpdater:
    """基于已压缩的分层记忆，用 LLM 合并刷新 user/soul 画像。"""

    def __init__(
        self,
        memory_store: MemoryStore,
        settings: Settings,
        *,
        max_tokens: int = 800,
    ) -> None:
        self.memory_store = memory_store
        self.settings = settings
        self.max_tokens = max_tokens
        self.enabled = settings.profile_enabled
        self.profile_store: ProfileStore | None = None
        self.llm: OpenAICompatibleLLM | None = None
        if not self.enabled:
            return

        self.profile_store = ProfileStore(
            settings.data_dir,
            soul_path=settings.soul_path,
            user_template_path=settings.user_template_path,
        )
        self.llm = OpenAICompatibleLLM(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        )

    def update_user(self, target_date: date) -> UpdateResult:
        """用目标自然日的 daily 记忆刷新用户画像。"""
        profile_store = self.profile_store
        if not self.enabled or profile_store is None:
            return UpdateResult(False, reason="disabled")

        label = target_date.isoformat()
        daily = self.memory_store.load_memory("daily", label)
        if daily is None:
            return UpdateResult(False, reason=f"missing_daily:{label}")
        evidence = str(daily.get("content", "")).strip()
        if not evidence:
            return UpdateResult(False, reason="empty_daily")

        updated = self._merge(
            _USER_SYSTEM,
            current=profile_store.read_user(),
            evidence=evidence,
            scope=f"用户画像（依据 {label} 的日记忆）",
        )
        if updated is None:
            return UpdateResult(False, reason="no_change")
        profile_store.write_user(updated)
        return UpdateResult(True, reason="updated")

    def update_soul(self, week_start: date) -> UpdateResult:
        """用某个 ISO 周的周记忆（缺失时回退到日记忆）刷新 AI 统一人格。"""
        profile_store = self.profile_store
        if not self.enabled or profile_store is None:
            return UpdateResult(False, reason="disabled")

        week_start = week_start - timedelta(days=week_start.weekday())
        label = _week_label(week_start)
        evidence = self._week_evidence(week_start)
        if not evidence:
            return UpdateResult(False, reason=f"missing_week:{label}")

        updated = self._merge(
            _SOUL_SYSTEM,
            current=profile_store.read_soul(),
            evidence=evidence,
            scope=f"AI 统一人格 Soul（依据 {label} 的周记忆）",
        )
        if updated is None:
            return UpdateResult(False, reason="no_change")
        profile_store.write_soul(updated)
        return UpdateResult(True, reason="updated")

    def _week_evidence(self, week_start: date) -> str:
        """优先取该周的 weekly 记忆，缺失时拼接该周已有的 daily 记忆。"""
        weekly = self.memory_store.load_memory("weekly", _week_label(week_start))
        if weekly is not None:
            content = str(weekly.get("content", "")).strip()
            if content:
                return content

        chunks: list[str] = []
        for offset in range(7):
            label = (week_start + timedelta(days=offset)).isoformat()
            daily = self.memory_store.load_memory("daily", label)
            if daily is None:
                continue
            content = str(daily.get("content", "")).strip()
            if content:
                chunks.append(f"[{label}] {content}")
        return "\n".join(chunks)

    def _merge(
        self,
        system: str,
        *,
        current: str,
        evidence: str,
        scope: str,
    ) -> str | None:
        """调用 LLM 合并当前画像与新证据；返回新全文或 None（无需更新）。"""
        sections = [
            f"# 当前{scope}\n{current.strip() or '（暂无内容，请基于证据建立初始画像）'}"
        ]
        sections.append(f"# 新的观察证据\n{evidence}")
        sections.append(
            f"请据此输出更新后的完整 Markdown 全文，控制在 {self.max_tokens} token 以内；"
            f"若无需更新则只输出 {_NO_CHANGE}。"
        )

        if self.llm is None:
            return None

        response = self.llm.complete(
            system=system,
            messages=[{"role": "user", "content": "\n\n".join(sections)}],
        )
        text = response.content.strip()
        if not text or text.upper() == _NO_CHANGE:
            return None
        return text


def _week_label(week_start: date) -> str:
    iso_year, iso_week, _ = week_start.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"
