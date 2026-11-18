import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wintermute.agent.core.types import ToolCall
from wintermute.config.factory import ModelSet
from wintermute.config.models import AppConfig
from wintermute.events import SQLiteStore
from wintermute.interfaces.http import HTTPInterface
from wintermute.interfaces.service import ConversationService


async def test_main_agent_reads_only_current_bound_terminal_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr("wintermute.events.integrations.terminal_tool.monotonic", lambda: now)
    bindings = [
        {"terminal_id": device, "conversation_id": chat, "user_id": user, "token": device}
        for device, chat, user in [
            ("phone", "primary", "owner"),
            ("desk", "primary", "owner"),
            ("tablet", "other-chat", "owner"),
            ("guest", "guest-chat", "guest-user"),
        ]
    ]
    config = AppConfig.model_validate(
        {
            "interfaces": {
                "http": {
                    "enabled": True,
                    "terminals": bindings,
                }
            }
        }
    )
    with SQLiteStore() as store:
        service = ConversationService(config, ModelSet(Mock(), {}), store, base_dir=tmp_path)
        http = HTTPInterface(config.interfaces.http, service.gateway, base_dir=tmp_path)
        service.http = http
        runner = service._make_runner("primary")
        query = ToolCall(id="read", name="list_terminal_capabilities", arguments={})

        async def inventory():
            result = await runner._execute_tool(query)
            assert not result.is_error
            return {t["terminal_id"]: t for t in json.loads(result.content)["targets"]}

        headers = {"X-Terminal-ID": "phone", "Authorization": "Bearer phone"}
        async with TestClient(TestServer(http.create_app())) as client:
            assert (await client.get("/health")).status == 200
            assert (
                await client.get(
                    "/v1/outputs",
                    headers={
                        **headers,
                        "Authorization": "Bearer invalid",
                    },
                )
            ).status == 401
            targets = await inventory()
            assert set(targets) == {"phone", "desk"}
            assert targets["phone"]["status"] == "unknown"
            assert targets["phone"]["last_seen_seconds_ago"] is None

            assert (await client.get("/v1/outputs", headers=headers)).status == 200
            targets = await inventory()
            assert targets["phone"]["status"] == "online"
            assert targets["phone"]["tools"] == []

            async def advertise(tools: list[str]) -> None:
                response = await client.post(
                    "/v1/terminal-tools/claim", headers=headers, json={"tools": tools}
                )
                assert response.status == 200
                assert await response.json() == {"call": None}

            await advertise(["device_status"])
            assert (await inventory())["phone"]["tools"] == ["device_status"]
            legacy = await runner._execute_tool(
                ToolCall(id="list", name="terminal_tool", arguments={"operation": "list_targets"})
            )
            assert legacy == await runner._execute_tool(query)
            await advertise([])
            assert (await inventory())["phone"]["tools"] == []
            await advertise(["device_status"])

            now += 12
            assert (await client.get("/v1/outputs", headers=headers)).status == 200
            now += 4
            phone = (await inventory())["phone"]
            assert (phone["status"], phone["last_seen_seconds_ago"], phone["tools"]) == (
                "online",
                4,
                [],
            )
            attempt = await runner._execute_tool(
                ToolCall(
                    id="expired",
                    name="terminal_tool",
                    arguments={
                        "operation": "run",
                        "terminal_id": "phone",
                        "tool": "device_status",
                        "arguments": {},
                    },
                ),
                run_id="local-test",
            )
            assert attempt.is_error
            assert "当前未提供该工具" in attempt.content
            now += 12
            assert (await inventory())["phone"]["status"] == "offline"
            await advertise(["device_status"])
            assert (await inventory())["phone"]["status"] == "online"
