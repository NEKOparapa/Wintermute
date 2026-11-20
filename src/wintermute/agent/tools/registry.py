"""按会话绑定记忆、设备、连接器和后台任务工具。"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from wintermute.agent import Tool
from wintermute.agent.tools.calendar.calendar_query import calendar_tool
from wintermute.agent.tools.calendar.holidays import HolidayCalendar
from wintermute.agent.tools.calendar.time_query import time_tool
from wintermute.agent.tools.connectors import DeviceConnectorCalls, create_connector_tools
from wintermute.agent.tools.conversation.schedule import create_schedule_tools
from wintermute.agent.tools.devices import DeviceToolBindings, create_user_device_tools
from wintermute.agent.tools.memory import create_memory_tools
from wintermute.events.integrations.connectors import ConnectorManager
from wintermute.events.storage.sqlite import SQLiteStore
from wintermute.memory import MemoryVault


def create_conversation_tools(
    store: SQLiteStore,
    conversation_id: str,
    *,
    timezone: str,
    workspace: Path,
    vault: MemoryVault | None,
    allow_character_write: Callable[[], bool],
    reply_device_id: Callable[[], str | None],
    devices: DeviceToolBindings | None,
    connectors: ConnectorManager,
    holidays: HolidayCalendar | None = None,
    web_tools: Sequence[Tool[Any]] = (),
    background_tools: Sequence[Tool[Any]] = (),
) -> tuple[Tool[Any], ...]:
    tools: list[Tool[Any]] = list(create_schedule_tools(store, conversation_id, timezone))
    tools.extend(
        (
            time_tool(store.now_utc, ZoneInfo(timezone)),
            calendar_tool(store.now_utc, holidays or HolidayCalendar(store.holiday_years)),
        )
    )
    if vault is not None:
        tools.extend(
            create_memory_tools(
                vault, actor="main_agent", allow_character_write=allow_character_write
            )
        )
    device_calls = None
    if devices is not None:
        tools.extend(
            create_user_device_tools(store, conversation_id, devices, workspace, reply_device_id)
        )
        device_calls = DeviceConnectorCalls(
            store, conversation_id, devices.current, devices.capabilities, reply_device_id
        )
    tools.extend(
        create_connector_tools(
            store,
            conversation_id,
            devices.current if devices is not None else (),
            devices.capabilities if devices is not None else None,
            connectors,
            device_calls,
        )
    )
    tools.extend(web_tools)
    tools.extend(background_tools)
    return tuple(tools)
