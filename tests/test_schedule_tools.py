import asyncio
import json

import pytest
from pydantic import ValidationError

from wintermute.agent import ToolContext
from wintermute.agent.tools.conversation.schedule import create_schedule_tools
from wintermute.events.storage.sqlite import SQLiteStore


def test_schedule_tools_separate_queries_and_mutations_within_each_conversation():
    with SQLiteStore(":memory:") as store:
        for room in ("work", "home"):
            store.register_conversation(room, "user")
        read, write = create_schedule_tools(store, "work", "Asia/Shanghai")
        home_read, home_write = create_schedule_tools(store, "home", "UTC")

        def call(tool, **args):
            request = tool.parameters.model_validate(args, strict=True)
            return asyncio.run(tool.execute(request, ToolContext("schedule-test", 1)))

        def data(tool, **args):
            result = call(tool, **args)
            assert not result.is_error, result.content
            return json.loads(result.content)

        assert read.name == "schedule_read" and write.name == "schedule_write"
        assert set(read.definition.parameters["properties"]["operation"]["enum"]) == {"list", "get"}
        assert set(write.definition.parameters["properties"]["operation"]["enum"]) == {
            "create",
            "update",
            "delete",
        }
        for tool, args in [
            (read, {"operation": "delete", "id": "id"}),
            (read, {"operation": "list", "content": ""}),
            (read, {"operation": "get"}),
            (read, {"operation": "get", "id": "id", "limit": 1}),
            (write, {"operation": "list"}),
            (write, {"operation": "update", "id": "id"}),
            (write, {"operation": "delete", "id": "id", "title": "unexpected"}),
            (
                write,
                {"operation": "create", "title": "meeting", "trigger_at": "2099-01-01T10:00:00"},
            ),
        ]:
            with pytest.raises(ValidationError):
                call(tool, **args)

        event = data(
            write,
            operation="create",
            title="meeting",
            content="agenda",
            trigger_at="2099-01-01T02:00:00Z",
        )["item"]
        meeting_id = event["id"]
        assert event["trigger_at"] == "2099-01-01T10:00:00+08:00"
        due = data(write, operation="create", title="due", trigger_at="2000-01-01T08:00:00+08:00")[
            "item"
        ]
        assert store.schedules.dispatch_due_schedules() == 1
        assert data(read, operation="get", id=due["id"])["item"]["status"] == "triggered"
        assert call(write, operation="update", id=due["id"], title="too late").is_error

        page = data(read, operation="list", limit=1)
        assert page["timezone"] == "Asia/Shanghai" and page["now"]
        assert page["items"][0]["id"] == due["id"]
        assert "content" not in page["items"][0]
        next_page = data(read, operation="list", limit=1, cursor=page["next_cursor"])
        assert [item["id"] for item in next_page["items"]] == [meeting_id]
        assert data(read, operation="get", id=meeting_id)["item"]["content"] == "agenda"
        scheduled = data(read, operation="list", status="scheduled", start="2099-01-01T00:00:00Z")
        assert [item["id"] for item in scheduled["items"]] == [meeting_id]
        assert data(home_read, operation="list")["items"] == []
        assert call(home_read, operation="get", id=meeting_id).is_error
        assert call(home_write, operation="delete", id=meeting_id).is_error
        assert data(write, operation="update", id=meeting_id, content="")["item"]["content"] == ""
        assert data(write, operation="delete", id=meeting_id)["item"]["id"] == meeting_id
        assert call(read, operation="get", id=meeting_id).is_error
