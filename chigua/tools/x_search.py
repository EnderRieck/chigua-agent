"""X（Twitter）。不需要 X 开发者账号：

1. responses —— xAI Responses API 的服务端工具 x_search（官方直连可用；中转站多半未实现 /responses）
2. live      —— 旧版 chat.completions 透传 search_parameters（部分中转站可用）
3. deepseek  —— DeepSeek 搜索 site:x.com 兜底

前两种都要求返回引用（citations / 工具调用痕迹），否则视为“中转站忽略了搜索参数”，拒绝采用模型凭空生成的帖子。
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta
from typing import Any

from openai import OpenAI

from ..config import settings
from . import ToolResult, ToolSpec
from .deepseek_search import site_search

_PROMPT = """请在 X 上搜索与「{query}」相关、{since} 之后的讨论，挑出最多 {count} 条有代表性的帖子
（优先：当事人/官方账号、主流媒体、高互动帖、不同立场各取一些）。
只输出一个 JSON 数组，不要任何其他文字，每个元素格式：
{{"author": "@handle 或显示名", "date": "YYYY-MM-DD", "text": "帖子内容要点（非中文请翻译为中文，保留关键原文词）",
  "url": "帖子链接", "likes": 数字或null, "reposts": 数字或null, "stance": "支持/反对/中立/爆料/辟谣/其他"}}"""


def _client() -> OpenAI:
    if not settings.has_llm:
        raise RuntimeError("未配置 XAI_API_KEY")
    return OpenAI(api_key=settings.xai_api_key, base_url=settings.xai_base_url,
                  timeout=settings.x_search_timeout, max_retries=0)


def parse_posts(text: str, citations: list[str]) -> list[dict[str, Any]]:
    text = text.strip()
    data: Any = None
    for candidate in (text, re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()):
        try:
            data = json.loads(candidate)
            break
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", candidate, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                    break
                except json.JSONDecodeError:
                    continue
    items: list[dict[str, Any]] = []
    if isinstance(data, list):
        for i, p in enumerate(data):
            if not isinstance(p, dict):
                continue
            url = p.get("url") or (citations[i] if i < len(citations) else "")
            body = str(p.get("text", ""))
            items.append({
                "title": f"{p.get('author', '?')}: {body[:40]}{'…' if len(body) > 40 else ''}",
                "url": url, "date": str(p.get("date", "")), "text": body,
                "meta": {"点赞": p.get("likes"), "转发": p.get("reposts"), "立场": p.get("stance", "")},
            })
    if not items and text:
        # 模型没按 JSON 输出，退化为一条综述 + 引用链接
        items.append({"title": "X 平台讨论综述", "text": text[:2000], "url": citations[0] if citations else "",
                      "meta": {"引用数": len(citations)}})
        for u in citations[1:8]:
            items.append({"title": "X 引用链接", "url": u})
    return items


def _since(days: int) -> str:
    return (date.today() - timedelta(days=max(1, days))).isoformat()


def _via_responses(query: str, count: int, days: int) -> ToolResult:
    client = _client()
    resp = client.responses.create(
        model=settings.xai_model,
        input=[{"role": "user", "content": _PROMPT.format(query=query, count=count, since=_since(days))}],
        tools=[{"type": "x_search"}],
    )
    citations: list[str] = []
    used_tool = False
    for item in getattr(resp, "output", []) or []:
        itype = getattr(item, "type", "")
        if "x_search" in itype or "search" in itype:
            used_tool = True
        for part in getattr(item, "content", []) or []:
            for ann in getattr(part, "annotations", []) or []:
                url = getattr(ann, "url", None)
                if url:
                    citations.append(url)
    if not used_tool and not citations:
        raise RuntimeError("接口未执行 x_search（无工具调用与引用）")
    return ToolResult(platform="x", items=parse_posts(resp.output_text or "", citations)[:count],
                      note="来自 xAI x_search（Responses API）")


def _via_live(query: str, count: int, days: int) -> ToolResult:
    client = _client()
    resp = client.chat.completions.create(
        model=settings.xai_model,
        messages=[{"role": "user", "content": _PROMPT.format(query=query, count=count, since=_since(days))}],
        extra_body={"search_parameters": {"mode": "on", "sources": [{"type": "x"}], "return_citations": True,
                                          "max_search_results": max(5, min(count * 2, 30)), "from_date": _since(days)}},
    )
    citations = getattr(resp, "citations", None) or (resp.model_extra or {}).get("citations") or []
    if not citations:
        raise RuntimeError("接口未返回 citations，疑似不支持 live search")
    text = resp.choices[0].message.content or ""
    return ToolResult(platform="x", items=parse_posts(text, list(citations))[:count],
                      note="来自 Grok Live Search（chat.completions search_parameters）")


async def x_search(query: str, count: int = 10, days: int = 7) -> ToolResult:
    count = max(1, min(int(count), 30))
    mode = settings.x_search_mode
    order = ["responses", "live", "deepseek"] if mode == "auto" else [mode]
    errors: list[str] = []
    budget = settings.x_search_timeout
    for m in order:
        try:
            if m == "responses":
                return await asyncio.wait_for(asyncio.to_thread(_via_responses, query, count, days), timeout=budget)
            if m == "live":
                return await asyncio.wait_for(asyncio.to_thread(_via_live, query, count, days), timeout=min(budget, 30))
            if m == "deepseek":
                reason = "；".join(errors) or "X_SEARCH_MODE=deepseek"
                res = await site_search("x.com", query, platform="x", count=count, reason=reason)
                if res.items:
                    return res
                # 搜索引擎对 x.com 索引很少，再退一步：找转述 X/推特舆论的新闻与长文
                from .deepseek_search import raw_search, to_items
                suffix = " 推特 海外网友 反应" if re.search(r"[\u4e00-\u9fff]", query) else " Twitter reaction"
                results = [r for r in await raw_search(f"{query}{suffix}", count=count * 2)
                           if float(r.get("rerank_score") or 0) >= 0.5][:count]
                return ToolResult(platform="x", items=to_items(results), ok=bool(results),
                                  note=f"{reason}；DeepSeek 对 x.com 无索引，改为搜索转述 X/推特舆论的报道"
                                       "（二手来源，不是帖子原文，报告中需注明）")
        except asyncio.TimeoutError:
            errors.append(f"{m} 超时（>{budget}s）")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{m} 不可用（{type(exc).__name__}: {str(exc)[:120]}）")
    return ToolResult(platform="x", items=[], ok=False, note="X 搜索失败: " + "；".join(errors))


TOOLS = [
    ToolSpec(
        name="x_search",
        description="搜索 X（Twitter）上的相关帖子，看海外/英文舆论、当事人海外账号、外媒记者爆料。"
                    "query 可用中英文；days 为回溯天数。",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}, "count": {"type": "integer", "description": "默认 10"},
                           "days": {"type": "integer", "description": "回溯天数，默认 7"}},
            "required": ["query"],
        },
        func=x_search,
        timeout=settings.x_search_timeout + 60,   # responses 预算 + live/deepseek 兜底的余量
    ),
]
