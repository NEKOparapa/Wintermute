from __future__ import annotations

from ..infrastructure.storage.schedule_store import ScheduleStore

__all__ = ["ScheduleStore", "ScheduleTriggerService"]


def __getattr__(name: str):
    if name == "ScheduleTriggerService":
        from .service import ScheduleTriggerService

        return ScheduleTriggerService
    raise AttributeError(name)
