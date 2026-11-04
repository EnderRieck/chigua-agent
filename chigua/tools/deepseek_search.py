"""DeepSeek Search / Fetch API（https://api-docs.deepseek.com/guides/search）。

- POST {base}/v1/tools/search  {query, count, need_content}
- POST {base}/v1/tools/fetch   {url}
query 支持 site:、lang:、after:、before: 操作符，是其他平台“兜底搜索”的基础。
"""
from __future__ import annotations

from typing import Any

import httpx

from ..config import settings
from . import ToolResult, ToolSpec


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"}


async def raw_search(query: str, count: int = 8, need_content: bool = False) -> list[dict[str, Any]]:
    if not settings.has_deepseek:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    query = query.strip()[:128]
    count = max(1, min(int(count), 50))
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        r = await client.post(f"{settings.deepseek_base_url.rstrip('/')}/v1/tools/search",
                              headers=_headers(),
                              json={"query": query, "count": count, "need_content": need_content})
        r.raise_for_status()
        return r.json().get("results", [])


async def raw_fetch(url: str) -> dict[str, Any]:
    if not settings.has_deepseek:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        r = await client.post(f"{settings.deepseek_base_url.rstrip('/')}/v1/tools/fetch",
                              headers=_headers(), json={"url": url})
        r.raise_for_status()
        return r.json()


def to_items(results: list[dict[str, Any]], platform_hint: str | None = None) -> list[dict[str, Any]]:
    items = []
    for res in results:
        text = res.get("summary") or res.get("snippet") or ""
        if res.get("content"):
            text = res["content"]
        items.append({
            "title": res.get("title") or res.get("url") or "",
            "url": res.get("url", ""),
            "date": (res.get("date") or "")[:10],
            "text": text,
            "meta": {"站点": res.get("site_name") or "", "相关度": round(float(res.get("rerank_score") or 0), 2)},
        })
    return items


async def site_search(site: str, keyword: str, platform: str, count: int = 8,
                      reason: str = "") -> ToolResult:
    """其他平台无法直连时的通用兜底：DeepSeek 搜索 site:xxx。"""
    try:
        results = await raw_search(f"site:{site} {keyword}", count=count)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(platform=platform, items=[], ok=False,
                          note=f"{reason}；DeepSeek 兜底搜索也失败: {exc}")
    note = f"{reason}；已改用 DeepSeek 搜索 site:{site}（结果为搜索引擎索引到的公开页面，覆盖面有限）"
    return ToolResult(platform=platform, items=to_items(results), note=note.strip("；"))


# ---------------- 暴露给模型的工具 ----------------
async def web_search(query: str, count: int = 8, need_content: bool = False) -> ToolResult:
    try:
        results = await raw_search(query, count=count, need_content=need_content)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(platform="web", items=[], ok=False, note=f"DeepSeek 搜索失败: {exc}")
    return ToolResult(platform="web", items=to_items(results))


async def fetch_page(url: str, max_chars: int = 6000) -> ToolResult:
    try:
        data = await raw_fetch(url)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        hint = {403: "目标站点拒绝抓取", 404: "页面无可用正文"}.get(code, "")
        return ToolResult(platform="web", items=[], ok=False, note=f"抓取失败 HTTP {code} {hint}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(platform="web", items=[], ok=False, note=f"抓取失败: {exc}")
    content = (data.get("content") or "").strip()
    max_chars = max(500, min(int(max_chars), 20000))
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n…（全文 {len(content)} 字，已截断）"
    return ToolResult(platform="web", items=[{"title": data.get("title") or url, "url": url, "text": content}])


TOOLS = [
    ToolSpec(
        name="web_search",
        description="通用联网搜索（DeepSeek 自研搜索引擎）。适合查新闻报道、官方通报、事件时间线。"
                    "query 支持 site:域名、after:YYYY-MM-DD、before:YYYY-MM-DD 等操作符，长度≤128 字。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词，建议包含事件关键词+主体名"},
                "count": {"type": "integer", "description": "返回条数 1-50，默认 8"},
                "need_content": {"type": "boolean", "description": "是否附带网页正文（更耗 token），默认 false"},
            },
            "required": ["query"],
        },
        func=web_search,
    ),
    ToolSpec(
        name="fetch_page",
        description="抓取指定 URL 的网页正文（Markdown）。用于深入阅读某条搜索结果、官方声明、长文。",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http/https 链接"},
                "max_chars": {"type": "integer", "description": "最多返回的字符数，默认 6000"},
            },
            "required": ["url"],
        },
        func=fetch_page,
    ),
]
