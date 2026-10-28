"""将外部任务结果接入事件系统的适配器。"""

from wintermute.events.integrations.subagent import BackgroundEventPublisher

__all__ = ["BackgroundEventPublisher"]
