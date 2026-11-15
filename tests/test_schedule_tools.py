"""Exercise the schedule tool with the JSON arguments accepted by the main Runner."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from wintermute.agent import ToolContext
from wintermute.events import EventGateway, SQLiteStore
from wintermute.events.integrations.schedule import ScheduleRequest, schedule_tool


async def test_agent_can_move_an_arrangement_across_local_midnight_and_delete_it():
    with SQLiteStore(clock=lambda: datetime(2026, 11, 16, tzinfo=UTC)) as store:
        gateway = EventGateway(store)
        for room in ("work", "private"):
            gateway.register_conversation(room, "owner")
        tool = schedule_tool(store, "work", "Asia/Shanghai")
        other = schedule_tool(store, "private", "Asia/Shanghai")
        ctx = ToolContext("call", 1)

        async def call(operation, **kwargs):
            params = ScheduleRequest.model_validate({"operation": operation, **kwargs}, strict=True)
            result = await tool.execute(params, ctx)
            assert not result.is_error, result.content
            return json.loads(result.content)

        # Use a JSON string (the provider cannot send a native Python datetime).
        created = await call(
            "create",
            title="发布评审",
            content="核实验收清单",
            trigger_at="2026-11-20T23:00:00+08:00",
        )
        identifier = created["item"]["id"]
        await call("create", title="跟进会议", trigger_at="2026-11-20T18:00:00Z")
        assert created["item"]["action"] == "notify_agent"
        assert created["timezone"] == "Asia/Shanghai"
        assert datetime.fromisoformat(created["now"]).utcoffset() == timedelta(hours=8)
        updated = await call("update", id=identifier, trigger_at="2026-11-20T16:30:00Z")
        assert updated["item"]["content"] == "核实验收清单"
        assert updated["item"]["trigger_at"] == "2026-11-21T00:30:00+08:00"
        params = {
            "start": "2026-11-21T00:00:00+08:00",
            "end": "2026-11-22T00:00:00+08:00",
            "limit": 1,
        }
        first = await call("list", **params)
        assert [i["id"] for i in first["items"]] == [identifier]
        second = await call("list", **params, cursor=first["next_cursor"])
        assert second["next_cursor"] is None and second["items"][0]["title"] == "跟进会议"
        inaccessible = await other.execute(ScheduleRequest(operation="delete", id=identifier), ctx)
        assert (
            inaccessible.is_error and (await call("get", id=identifier))["item"]["id"] == identifier
        )
        assert (await call("update", id=identifier, content=""))["item"]["content"] == ""
        await call("delete", id=identifier)
        assert (await tool.execute(ScheduleRequest(operation="get", id=identifier), ctx)).is_error


@pytest.mark.parametrize(
    "parameters",
    [
        {"operation": "create", "title": "周会", "trigger_at": "2026-11-20T09:00:00"},
        {
            "operation": "create",
            "title": "周会",
            "trigger_at": "2026-11-20T09:00Z",
            "action": "notify_desktop",
        },
        {"operation": "create", "title": "缺少时间"},
        {"operation": "update", "id": "item"},
        {"operation": "delete", "title": "按标题误删"},
        {"operation": "delete", "id": "item", "conversation_id": "another"},
        {"operation": "list", "trigger_at": "2026-11-20T01:00Z"},
        {"operation": "list", "start": "2026-11-20T01:00Z", "end": "2026-11-20T00:00Z"},
    ],
)
def test_invalid_schedule_arguments_are_rejected_before_mutating(parameters):
    with pytest.raises(ValidationError):
        ScheduleRequest.model_validate(parameters, strict=True)
