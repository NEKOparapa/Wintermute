import asyncio
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from wintermute.agent import ToolContext
from wintermute.agent.tools.devices import DeviceToolBindings
from wintermute.agent.tools.registry import create_conversation_tools
from wintermute.config import HTTPDeviceSettings
from wintermute.events.core.types import Device
from wintermute.events.integrations.connectors import ConnectorManager
from wintermute.events.integrations.devices import DeviceCapabilities
from wintermute.events.storage.sqlite import SQLiteStore


def test_device_tools_list_connected_devices_across_the_account(tmp_path):
    devices = DeviceToolBindings(
        [
            HTTPDeviceSettings(
                user_id="user", conversation_id=room, device_id=device, token_env="TEST_TOKEN"
            )
            for room, device in [("work", "desktop"), ("home", "mobile")]
        ],
        DeviceCapabilities(),
        Mock(),
    )
    with SQLiteStore(":memory:") as store:

        def tools_for(room, bindings):
            tools = create_conversation_tools(
                store,
                room,
                timezone="Asia/Shanghai",
                workspace=tmp_path,
                vault=None,
                allow_character_write=lambda: False,
                reply_device_id=lambda: None,
                devices=bindings,
                connectors=ConnectorManager(),
            )
            return {tool.name: tool for tool in tools}

        for room, device in [("work", "desktop"), ("home", "mobile")]:
            store.register_conversation(room, "user")
            store.register_device(Device(device_id=device, user_id="user"))
            store.devices.observe(device, device * 3, "My " + device)
        for room in ("work", "home"):
            without_devices = tools_for(room, None)
            with_devices = tools_for(room, devices)
            assert with_devices.keys() - without_devices.keys() == {
                "device_statuses",
                "device_messaging",
            }
            assert without_devices.keys() <= with_devices.keys()
            assert "Mcp" in without_devices and "call_device_connector" not in with_devices
            statuses = with_devices["device_statuses"]
            with pytest.raises(ValidationError):
                statuses.parameters.model_validate({"action": "notify"}, strict=True)
            args = statuses.parameters.model_validate({}, strict=True)
            result = asyncio.run(statuses.execute(args, ToolContext("list", 1)))
            assert not result.is_error
            assert json.loads(result.content)["devices"] == [
                {
                    "device_id": device,
                    "name": "My " + device,
                    "platform": None,
                    "status": "unknown",
                    "can_notify": device_room == room,
                }
                for device, device_room in [("desktop", "work"), ("mobile", "home")]
            ]
            messaging = with_devices["device_messaging"]
            schema = messaging.definition.parameters
            assert set(schema["properties"]["action"]["enum"]) == {"ask", "notify"}
            assert set(schema["required"]) == {"action", "title"}
            for invalid in [
                {"action": "list"},
                {"action": "list_targets"},
                {"operation": "send"},
                {"action": "ask"},
                {"action": "ask", "question": "  "},
                {"action": "ask", "question": "?", "targets": ["desktop"]},
                {"action": "ask", "question": "?", "options": ["a", "a"]},
                {"action": "ask", "question": "?", "allow_custom": False},
                {"action": "notify", "body": "message"},
                {"action": "notify", "targets": ["desktop"]},
                {"action": "notify", "targets": ["desktop", "desktop"], "body": "message"},
                {"action": "notify", "targets": ["desktop"], "body": "message", "options": []},
            ]:
                with pytest.raises(ValidationError):
                    messaging.parameters.model_validate({"title": "Title", **invalid}, strict=True)
            args = messaging.parameters.model_validate(
                {
                    "action": "notify",
                    "title": "Title",
                    "body": "hello",
                    "targets": ["desktop", "mobile"],
                }
            )
            denied = asyncio.run(messaging.execute(args, ToolContext("send", 1, run_id="test")))
            assert denied.is_error and "device_statuses" in denied.content
