import asyncio
import json

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from pydantic import ValidationError
from test_chat_media import media_service

from wintermute.agent import ToolContext
from wintermute.agent.tools.web import WebFetchInput, WebSearchInput, open_web_tools
from wintermute.config import AppConfig, DeepSeekWebSettings
from wintermute.config.api_keys import APIKeyRegistry


def test_main_agent_searches_then_reads_a_source(tmp_path):
    url = "https://example.org/story?id=12"
    source = {"title": "报道", "url": url, "summary": "报道摘要", "content": ""}
    markdown = "# 报道\n\n正文中才有的细节。"
    requests = []
    results = []
    final_turn = asyncio.Event()

    async def upstream(request):
        assert request.headers["Authorization"] == "Bearer test-web-key"
        body = await request.json()
        requests.append((request.path, body))
        if request.path == "/v1/tools/search":
            return web.json_response({"results": [source]})
        return web.json_response({"title": "报道", "content": markdown})

    def model_reply(data):
        last = data["messages"][-1]
        if last["role"] == "tool":
            results.append((last["tool_call_id"], json.loads(last["content"])))
        if not results:
            assert {"web_search", "web_fetch"} <= {t["function"]["name"] for t in data["tools"]}
            name, args = "web_search", {"query": "报道 site:example.org", "count": 3}
        elif len(results) == 1:
            name, args = "web_fetch", {"url": results[0][1]["results"][0]["url"]}
        else:
            final_turn.set()
            return {"delta": {"content": markdown}, "finish_reason": "stop"}
        return {
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": name,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
            },
            "finish_reason": "tool_calls",
        }

    async def run():
        app = web.Application()
        app.router.add_post("/v1/tools/search", upstream)
        app.router.add_post("/v1/tools/fetch", upstream)
        async with TestServer(app) as server:
            settings = {"base_url": str(server.make_url("/v1/")), "api_key": "test-web-key"}
            async with (
                media_service(tmp_path, model_reply=model_reply, web_settings=settings) as (
                    backend,
                    _,
                    _,
                ),
                ClientSession(
                    backend, headers={"X-Device-ID": "desktop", "Authorization": "Bearer desktop"}
                ) as client,
            ):
                response = await client.post(
                    "/v1/messages", json={"message_id": "web-test", "text": "查找报道并读原文"}
                )
                assert response.status == 202
                job_id = (await response.json())["job_id"]
                await asyncio.wait_for(final_turn.wait(), 10)
                async with asyncio.timeout(5):
                    while True:
                        job = await (await client.get(f"/v1/jobs/{job_id}")).json()
                        if job["status"] not in {"running", "queued"}:
                            assert job["status"] == "completed"
                            break
                        await asyncio.sleep(0.02)

        assert requests == [
            (
                "/v1/tools/search",
                {"query": "报道 site:example.org", "count": 3, "need_content": False},
            ),
            ("/v1/tools/fetch", {"url": url}),
        ]
        assert results == [
            ("web_search", {"results": [source]}),
            ("web_fetch", {"url": url, "title": "报道", "content": markdown}),
        ]

    asyncio.run(run())


def test_web_arguments_credentials_and_provider_errors(monkeypatch):
    for fields in ({"query": "   "}, {"query": "字" * 129}, {"query": "字", "count": 51}):
        with pytest.raises(ValidationError):
            WebSearchInput.model_validate(fields, strict=True)
    for url in ("file:///path", "https://user:pass@example.org", "https://", "http://a:99999"):
        with pytest.raises(ValidationError):
            WebFetchInput.model_validate({"url": url}, strict=True)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    async def run():
        replies = [
            web.json_response({"results": []}),
            web.json_response({"error": {"message": "sensitive upstream detail"}}, status=429),
            web.json_response({"results": {}}),
            web.Response(text="not JSON"),
            web.json_response({"content": ""}),
        ]
        sent = []

        async def upstream(request):
            sent.append((request.headers["Authorization"], await request.json()))
            return replies.pop(0)

        app = web.Application()
        app.router.add_post("/v1/tools/{tool}", upstream)
        async with TestServer(app) as server:
            config = AppConfig.model_validate({"events": {"database_path": ":memory:"}})
            registry = APIKeyRegistry(config)
            settings = DeepSeekWebSettings(base_url=str(server.make_url("/v1")))
            context = ToolContext("web", 1)
            async with open_web_tools(settings, registry.provider("web")) as (search, fetch):
                args = WebSearchInput(
                    query="示例",
                    count=1,
                    need_content=True,
                    ori_question="原问题",
                    localization="1.2.3.4",
                )
                missing = await search.execute(args, context)
                assert missing.is_error and not sent
                registry.update("env:DEEPSEEK_API_KEY", "first-token")
                empty = await search.execute(args, context)
                assert not empty.is_error and json.loads(empty.content) == {"results": []}
                assert sent[0] == ("Bearer first-token", args.model_dump(mode="json"))
                registry.update("env:DEEPSEEK_API_KEY", "second-token")
                limited = await search.execute(args, context)
                assert limited.is_error and "429" in limited.content
                assert "sensitive" not in limited.content and "second-token" not in limited.content
                assert sent[-1][0] == "Bearer second-token"
                assert (await search.execute(args, context)).is_error
                page = WebFetchInput(url="https://example.org")
                assert (await fetch.execute(page, context)).is_error
                assert (await fetch.execute(page, context)).is_error
                assert not replies

    asyncio.run(run())
