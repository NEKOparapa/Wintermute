"""Main model -> selected client -> result notification -> main model and audit API."""

import asyncio
import json

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from model_text import event_fields

from wintermute.agent import AssistantMessage, ToolCall, ToolResultMessage
from wintermute.config import AppConfig, HTTPSettings
from wintermute.config.factory import ModelSet
from wintermute.events import EventGateway, SQLiteStore
from wintermute.interfaces.http import HTTPInterface
from wintermute.interfaces.service import ConversationService


class RemoteModel:
    def __init__(self, target, tool):
        self.target, self.tool = target, tool
        self.submitted = None
        self.result = None

    async def stream(self, context):
        last = context.messages[-1]
        if isinstance(last, ToolResultMessage):
            assert last.name == "terminal_tool" and not last.is_error, last.content
            payload = json.loads(last.content)
            if "targets" in payload:
                devices = {t["terminal_id"]: t["tools"] for t in payload["targets"]}
                assert self.tool in devices[self.target]
                assert "private" not in devices
                args = {"command": "hostname"} if self.tool == "powershell" else {}
                yield AssistantMessage(
                    tool_calls=(
                        ToolCall(
                            "execute",
                            "terminal_tool",
                            {
                                "operation": "run",
                                "terminal_id": self.target,
                                "tool": self.tool,
                                "arguments": args,
                            },
                        ),
                    )
                )
            else:
                self.submitted = payload
                yield AssistantMessage("已提交，等待设备回传。")
        elif event_fields(last.text)["来源"] == "后台通知 · 终端工具":
            self.result = event_fields(last.text)["正文"]
            yield AssistantMessage("设备已返回结果。")
        else:
            assert "terminal_tool" in {tool.name for tool in context.tools}
            yield AssistantMessage(
                tool_calls=(ToolCall("discover", "terminal_tool", {"operation": "list_targets"}),)
            )


@pytest.mark.parametrize(
    "target,tool",
    [
        ("desktop", "powershell"),
        ("android", "device_status"),
        ("esp32", "device_status"),
    ],
)
async def test_remote_execution_and_receipt(target, tool):
    terminals = ["desktop", "android", "esp32", "private"]
    config = AppConfig.model_validate(
        {
            "events": {"poll_interval": 0.01},
            "interfaces": {
                "http": {
                    "enabled": True,
                    "port": 0,
                    "terminals": [
                        {
                            "terminal_id": name,
                            "user_id": "owner",
                            "token": name,
                            "conversation_id": "other" if name == "private" else "work",
                        }
                        for name in terminals
                    ],
                }
            },
        }
    )
    model = RemoteModel(target, tool)
    with SQLiteStore(":memory:") as store:
        async with (
            asyncio.timeout(10),
            ConversationService(config, ModelSet(model, {}), store) as service,
            ClientSession() as client,
        ):
            host, port = service.http.addresses[0]
            base = f"http://{host}:{port}/v1"

            def auth(name):
                return {"Authorization": "Bearer " + name, "X-Terminal-ID": name}

            async def claim(name, tools):
                response = await client.post(
                    base + "/terminal-tools/claim", headers=auth(name), json={"tools": tools}
                )
                assert response.status == 200
                return (await response.json())["call"]

            async def audit(name):
                response = await client.get(base + "/terminal-tools/history", headers=auth(name))
                assert response.status == 200
                return (await response.json())["items"]

            assert await claim(target, [tool]) is None
            assert (
                await client.post(
                    base + "/messages",
                    headers=auth("android"),
                    json={"message_id": "request", "text": "请检查设备"},
                )
            ).status == 202
            await service.scheduler.wait_until_idle()
            assert model.submitted["status"] == "queued"
            other = "android" if target != "android" else "desktop"
            assert await claim(other, [tool]) is None
            assert await claim(target, ["unrelated"]) is None
            call = await claim(target, [tool])
            assert call["id"] == model.submitted["id"] and call["tool"] == tool
            assert await claim(target, [tool]) is None
            (running,) = await audit(other)
            assert running["status"] == "running" and "claim_token" not in running
            assert await audit("private") == []
            route = base + "/terminal-tools/" + call["id"] + "/result"
            result = {
                "status": "succeeded",
                "claim_token": call["claim_token"],
                "exit_code": 0,
                "stdout": "terminal-result-42",
                "stderr": "",
                "truncated": False,
            }
            assert (await client.post(route, headers=auth(other), json=result)).status == 404
            for _ in range(2):
                assert (await client.post(route, headers=auth(target), json=result)).status == 200
            assert (
                await client.post(
                    route, headers=auth(target), json={**result, "stdout": "different"}
                )
            ).status == 409
            await service.scheduler.wait_until_idle()
            assert "terminal-result-42" in model.result and target in model.result
            (finished,) = await audit("desktop")
            assert finished["id"] == call["id"] and finished["status"] == "succeeded"
            assert finished["stdout"] == result["stdout"] and finished["exit_code"] == 0
            (event,) = store.list_events("notification", user_id="owner")
            assert event.reply_terminal_id == "android"


async def test_capability_discovery_requires_device_identity_and_secure_transport(monkeypatch):
    bindings = [
        {"terminal_id": name, "token": token, "user_id": "owner", "conversation_id": "work"}
        for name, token in (("one", "shared"), ("two", "shared"), ("desktop", "separate"))
    ]
    settings = HTTPSettings.model_validate({"enabled": True, "terminals": bindings})
    now = 10
    monkeypatch.setattr("wintermute.events.integrations.terminal_tool.monotonic", lambda: now)
    route = "/v1/terminal-tools/claim"
    with SQLiteStore(":memory:") as store:
        http = HTTPInterface(settings, EventGateway(store))
        async with TestClient(TestServer(http.create_app())) as client:
            desktop = {"X-Terminal-ID": "desktop", "Authorization": "Bearer separate"}
            for headers, status in (
                ({}, 401),
                ({"X-Terminal-ID": "one", "Authorization": "Bearer shared"}, 403),
                ({**desktop, "Host": "public.example"}, 403),
            ):
                response = await client.post(route, headers=headers, json={"tools": ["powershell"]})
                assert response.status == status and "no-store" in response.headers["Cache-Control"]
            assert not http.terminal_capabilities.tools("one")
            assert not http.terminal_capabilities.tools("desktop")
            for names in (["cmd"], [], ["powershell"]):
                response = await client.post(route, headers=desktop, json={"tools": names})
                assert response.status == 200
                assert list(http.terminal_capabilities.tools("desktop")) == names
            now += 16
            assert not http.terminal_capabilities.tools("desktop")
