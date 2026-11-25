"""Format ranked, citable web results within the current model request's budget."""

import json
from collections.abc import Callable

from purin.agent import ToolContext, ToolResult

# Also bounds history when context management is disabled.
MAX_OUTPUT_BYTES = 16 * 1024


def _fits(text: str, context: ToolContext) -> bool:
    return len(text.encode("utf-8")) <= MAX_OUTPUT_BYTES and (
        context.result_fits is None or context.result_fits(text)
    )


def _prefix_length(length: int, fits: Callable[[int], bool]) -> int:
    high = min(length, MAX_OUTPUT_BYTES)
    # A complete value can use less space than one carrying a truncation marker.
    if fits(high):
        return high
    low = 0
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return low


def _fit_fetched_page(page: dict[str, str], context: ToolContext) -> ToolResult:
    url, title, body = page["url"], page["title"], page["content"]

    def render(title_length: int, body_length: int) -> str:
        data: dict[str, object] = {
            "url": url,
            "title": title[:title_length],
            "content": body[:body_length],
        }
        if title_length < len(title) or body_length < len(body):
            data["truncated"] = True
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    if len(url) + len(title) + len(body) <= MAX_OUTPUT_BYTES:
        full = render(len(title), len(body))
        if _fits(full, context):
            return ToolResult(full)

    if len(body) <= MAX_OUTPUT_BYTES and _fits(render(0, len(body)), context):
        body_length = len(body)
    else:
        # Keep a short label on long articles only when a useful preview also fits.
        title_preview = min(32, len(title))
        if not _fits(render(title_preview, min(128, len(body))), context):
            title_preview = 0
        body_length = _prefix_length(len(body), lambda n: _fits(render(title_preview, n), context))
    if not body[:body_length].strip():
        return context.error("剩余上下文预算不足以返回网页正文。", "正文超出预算", "预算")
    title_length = _prefix_length(len(title), lambda n: _fits(render(n, body_length), context))
    return ToolResult(render(title_length, body_length))


def fit_web_output(
    sources: list[dict[str, str]], context: ToolContext, *, fetch: bool = False
) -> ToolResult:
    if fetch:
        if not sources or not sources[0]["content"].strip():
            return context.error("网页没有可供阅读的正文。", "正文为空")
        return _fit_fetched_page(sources[0], context)

    widths = {"title": 96, "summary": 64, "content": 0}

    def render(rows: list[dict[str, str]]) -> str:
        truncated = len(rows) < len(sources)
        entries = []
        for source in rows:
            entry = {"url": source["url"]}
            for key, width in (("date", 48), *widths.items()):
                if key not in source:
                    continue
                value = source[key]
                if len(value) > width:
                    truncated = True
                    value = value[: width - 1] + "…" if width else ""
                if value or key == "title":
                    entry[key] = value
            entries.append(entry)
        payload: dict[str, object] = {"results": entries}
        if truncated:
            payload["truncated"] = True
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def fits(text: str) -> bool:
        return _fits(text, context)

    # Keep links intact. An unusually long URL must not displace every later source.
    selected = [s for s in sources if len(s["url"]) <= MAX_OUTPUT_BYTES]
    if not sources and fits('{"results":[]}'):
        return ToolResult('{"results":[]}')
    if selected and not fits(render(selected)):
        candidates, selected = selected, []
        for source in candidates:
            if fits(render([*selected, source])):
                selected.append(source)
        if not selected:
            widths.update(title=16, summary=0)
            selected = next(([s] for s in candidates if fits(render([s]))), [])
    if not selected:
        return context.error("搜索结果超出上下文预算，无法保留来源链接。", "超出上下文预算")

    # Grow titles and summaries before spending any space on page bodies.
    # Use a common character limit per field so a long first page cannot crowd out peers.
    for key in widths:

        def fits_width(size: int, field: str = key) -> bool:
            widths[field] = size
            return fits(render(selected))

        widths[key] = _prefix_length(max(len(s.get(key, "")) for s in selected), fits_width)
    return ToolResult(render(selected))
