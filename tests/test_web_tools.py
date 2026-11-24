import asyncio
import json
from collections.abc import AsyncIterator

from aiohttp import web
from aiohttp.test_utils import TestServer

from wintermute.agent import AgentContext, AgentRunner, AssistantMessage, ToolCall, ToolContext
from wintermute.agent.context import (
    ContextBudget,
    EstimatedTokenCounter,
    MainContextManager,
    SummaryRequest,
)
from wintermute.agent.core.types import ToolResultMessage, UserMessage
from wintermute.agent.tools.web import WebFetchInput, WebSearchInput, open_web_tools
from wintermute.agent.tools.web.output import MAX_OUTPUT_BYTES, fit_web_output
from wintermute.config import ConfigError, DeepSeekWebSettings
from wintermute.events.core.types import Device, OutputRecord
from wintermute.events.ingress import EventGateway
from wintermute.events.runtime.scheduler import RunScheduler
from wintermute.events.storage.sqlite import SQLiteStore


def test_fetch_404_fits_last_24_bytes_and_allows_the_next_model_turn() -> None:
    async def exercise() -> None:
        async def missing(_: web.Request) -> web.Response:
            return web.Response(status=404)

        app = web.Application()
        app.router.add_post("/tools/fetch", missing)
        async with (
            TestServer(app) as server,
            open_web_tools(
                DeepSeekWebSettings(base_url=str(server.make_url("/"))), lambda: "fixture"
            ) as (_, fetch),
        ):
            call = ToolCall("read", "web_fetch", {"url": "https://example.com/gone"})
            declaration = AssistantMessage(tool_calls=(call,))
            prompt = UserMessage("读。")
            refs = (UserMessage("参考资料" * 300),)
            placeholder = ToolResultMessage(call.id, call.name, "")
            counter = EstimatedTokenCounter()
            base = counter.count(
                AgentContext("", (prompt, declaration, placeholder), (fetch.definition,), refs)
            )
            budget = ContextBudget(
                base + 24 + 256, reserve_tokens=256, keep_recent_tokens=0, max_summary_tokens=1
            )

            class Model:
                async def stream(self, context: AgentContext) -> AsyncIterator[AssistantMessage]:
                    assert counter.count(context) <= budget.input_limit
                    last = context.messages[-1]
                    if isinstance(last, ToolResultMessage):
                        assert last.is_error and "HTTP 404" in last.content
                        assert len(last.content.encode()) <= 24
                        yield AssistantMessage("该网页不存在。")
                    else:
                        yield declaration

            class Summarizer:
                async def summarize(self, _: SummaryRequest) -> str:
                    raise AssertionError("The fetch error should fit without compaction")

            runner = AgentRunner(
                Model(), tools=(fetch,), context_manager=MainContextManager(budget, Summarizer())
            )
            result = await runner.run(prompt, references=refs)
            assert result.turns == 2 and result.response.text == "该网页不存在。"

    asyncio.run(exercise())


def test_fetch_prioritizes_body_when_a_long_title_competes_for_space() -> None:
    async def exercise() -> None:
        url = "https://example.com/long-title"
        title = "产品与工程团队月度进展报告：" * 7
        body = '本次发布的版本是 3.2。\n支持 "中文" 和 🌍。'

        async def page(_: web.Request) -> web.Response:
            return web.json_response({"title": title, "content": body})

        app = web.Application()
        app.router.add_post("/tools/fetch", page)
        async with (
            TestServer(app) as server,
            open_web_tools(
                DeepSeekWebSettings(base_url=str(server.make_url("/"))), lambda: "fixture"
            ) as (_, fetch),
        ):
            for limit in (4096, 256, 128, 64, 0):
                context = ToolContext(
                    "read", 1, result_fits=lambda s, n=limit: len(s.encode()) <= n
                )
                receipt = await fetch.execute(WebFetchInput(url=url), context)
                assert len(receipt.content.encode()) <= limit
                if limit <= 64:
                    assert receipt.is_error
                    if limit:
                        assert "预算" in receipt.content
                    continue
                assert not receipt.is_error
                data = json.loads(receipt.content)
                assert data["url"] == url
                if limit >= 256:
                    assert data["content"] == body
                else:
                    assert data["content"].startswith("本次发布的版本是 3.2。")
                if limit == 4096:
                    assert data == {"url": url, "content": body, "title": title}
                else:
                    assert data["truncated"] and data["title"] != title

    asyncio.run(exercise())


def test_fetch_failures_use_the_remaining_receipt_budget() -> None:
    async def exercise() -> None:
        async def upstream(request: web.Request) -> web.Response:
            case = (await request.json())["url"].rsplit("/", 1)[-1]
            if case == "rate":
                return web.Response(status=429)
            if case == "timeout":
                await asyncio.sleep(0.2)
            if case == "bad-json":
                return web.Response(text="{", content_type="application/json")
            if case == "bad-title":
                return web.json_response({"title": None, "content": "ok"})
            if case == "blank":
                return web.json_response({"content": " \n "})
            return web.json_response({})

        key_configured = True

        def key() -> str:
            if not key_configured:
                raise ConfigError("missing")
            return "fixture"

        app = web.Application()
        app.router.add_post("/tools/fetch", upstream)
        async with (
            TestServer(app) as server,
            open_web_tools(
                DeepSeekWebSettings(base_url=str(server.make_url("/")), timeout=0.1), key
            ) as (_, fetch),
        ):
            for case, expected in (
                ("rate", "HTTP 429"),
                ("timeout", "超时"),
                ("bad-json", "响应无效"),
                ("bad-title", "标题无效"),
                ("blank", "正文为空"),
                ("missing", "缺少正文"),
                ("no-key", "缺少密钥"),
            ):
                key_configured = case != "no-key"
                args = WebFetchInput(url=f"https://example.com/{case}")
                for limit in (24, 0):
                    context = ToolContext(
                        "read", 1, result_fits=lambda s, n=limit: len(s.encode()) <= n
                    )
                    result = await fetch.execute(args, context)
                    assert result.is_error and len(result.content.encode()) <= limit
                    assert result.content == (expected if limit else "")

    asyncio.run(exercise())


def test_output_prioritizes_summaries_and_never_splits_source_links() -> None:
    url = "https://example.com/资料?q=a%20b"
    sources = [
        {"title": "巨大链接", "url": url + "x" * MAX_OUTPUT_BYTES},
        {"title": "第一篇", "url": url, "summary": "第一条简报", "content": "全文" * 20000},
        {"title": "第二篇", "url": url + "2", "summary": "第二条简报", "content": "全文" * 40000},
    ]
    for limit in (None, 380):

        def fits(text: str, size: int | None = limit) -> bool:
            return size is None or len(text.encode()) <= size

        receipt = fit_web_output(
            sources, ToolContext("one", 1, result_fits=fits if limit else None)
        )
        data = json.loads(receipt.content)
        assert not receipt.is_error and data["truncated"]
        assert len(receipt.content.encode()) <= (limit or MAX_OUTPUT_BYTES)
        assert [item["url"] for item in data["results"]] == [url, url + "2"]
        assert [item["summary"] for item in data["results"]] == ["第一条简报", "第二条简报"]
        assert all(item["content"].endswith("…") for item in data["results"])

    tight = ToolContext("two", 1, result_fits=lambda s: len(s.encode()) <= 60)
    error = fit_web_output(sources, tight)
    assert error.is_error and "超出上下文预算" in error.content
    assert json.loads(fit_web_output([], tight).content) == {"results": []}


def test_large_web_batch_delivers_an_assistant_reply_within_the_remaining_context() -> None:
    async def exercise() -> None:
        body = '大段网页正文🌍"\\\n' * 20000
        urls = [f"https://example.com/news/{i}" for i in range(5)]
        hits = [
            {
                "title": f"来源 {i}",
                "url": url,
                "snippet": f"短摘要 {i}",
                "content": body,
                "site_icon": "x" * 50000,
            }
            for i, url in enumerate(urls)
        ]
        hits[0]["summary"] = "优先使用长摘要"
        budget = ContextBudget(
            12500, reserve_tokens=3500, keep_recent_tokens=500, max_summary_tokens=200
        )

        async def search(_: web.Request) -> web.Response:
            return web.json_response({"results": hits})

        async def fetch(_: web.Request) -> web.Response:
            return web.json_response({"title": "正文标题", "content": body})

        class Summarizer:
            async def summarize(self, _: SummaryRequest) -> str:
                raise AssertionError("Search receipts should fit without compacting this history")

        class Model:
            async def stream(self, context: AgentContext) -> AsyncIterator[AssistantMessage]:
                assert EstimatedTokenCounter().count(context) <= budget.input_limit
                results = [m for m in context.messages if isinstance(m, ToolResultMessage)]
                if not results:
                    yield AssistantMessage(
                        tool_calls=(
                            ToolCall(
                                "search", "web_search", {"query": "新闻", "need_content": True}
                            ),
                            ToolCall("page", "web_fetch", {"url": urls[0]}),
                        ),
                        provider_data={"reasoning": "推理" * 150},
                    )
                    return
                assert [m.tool_call_id for m in results] == ["search", "page"]
                search_data, page = (json.loads(m.content) for m in results)
                assert search_data["truncated"] and page["truncated"]
                assert [r["url"] for r in search_data["results"]] == urls
                assert search_data["results"][0]["summary"] == "优先使用长摘要"
                assert search_data["results"][1]["summary"] == "短摘要 1"
                assert all("site_icon" not in r for r in search_data["results"])
                assert page["url"] == urls[0] and page["content"].startswith("大段网页正文")
                yield AssistantMessage("核对结果：" + page["url"])

        app = web.Application()
        app.router.add_post("/tools/search", search)
        app.router.add_post("/tools/fetch", fetch)
        async with (
            TestServer(app) as server,
            open_web_tools(
                DeepSeekWebSettings(base_url=str(server.make_url("/"))), lambda: "fixture"
            ) as tools,
        ):
            summary_only = await tools[0].execute(
                WebSearchInput(query="新闻", count=1), ToolContext("short", 1)
            )
            assert json.loads(summary_only.content) == {
                "results": [{"url": urls[0], "title": "来源 0", "summary": "优先使用长摘要"}]
            }
            runner = AgentRunner(
                Model(), tools=tools, context_manager=MainContextManager(budget, Summarizer())
            )
            delivered: list[str] = []

            def deliver(output: OutputRecord) -> None:
                delivered.append(output.text)

            with SQLiteStore() as store:
                gateway = EventGateway(store)
                gateway.register_device(Device(user_id="user", device_id="desktop"))
                gateway.register_conversation("room", "user")
                async with RunScheduler(gateway, lambda _: runner) as scheduler:
                    scheduler.output_router.connect("desktop", deliver)
                    client = gateway.conversation.connect("desktop", "room")
                    await client.send("请搜索网页。" + "已有上下文" * 60)
                    await scheduler.wait_until_idle()
                    assert delivered == ["核对结果：" + urls[0]]
                    history, _ = store.history.browse_history(
                        user_id="user", conversation_id="room", view="dialogue"
                    )
                    assert history[0].text == delivered[0]

    asyncio.run(exercise())
