"""小红书：优先通过本地运行的 xpzouying/xiaohongshu-mcp（Streamable HTTP MCP，需用户自己扫码登录）；
未配置时走 DeepSeek site:xiaohongshu.com 兜底。"""
from __future__ import annotations

import json
import re
from typing import Any

from ..config import settings
from . import ToolResult, ToolSpec
from .deepseek_search import site_search


def _extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                return None
    return None


async def _mcp_call(tool: str, args: dict[str, Any]) -> Any:
    from .mcp_client import open_mcp_session, text_of

    async with open_mcp_session(settings.xhs_mcp_url, settings.xhs_mcp_token,
                                timeout=settings.request_timeout * 3) as session:
        result = await session.call_tool(tool, args)
    joined = text_of(result)
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        raise RuntimeError(joined or "MCP 返回错误")
    return _extract_json(joined) if joined else None


def _parse_feeds(data: Any) -> list[dict[str, Any]]:
    """xiaohongshu-mcp 的 search_feeds 返回结构在版本间略有差异，这里做宽松解析。"""
    feeds: list[Any] = []
    if isinstance(data, dict):
        for key in ("feeds", "data", "items", "notes"):
            if isinstance(data.get(key), list):
                feeds = data[key]
                break
        if not feeds and isinstance(data.get("data"), dict):
            inner = data["data"]
            for key in ("feeds", "items", "notes"):
                if isinstance(inner.get(key), list):
                    feeds = inner[key]
                    break
    elif isinstance(data, list):
        feeds = data

    items = []
    for f in feeds:
        if not isinstance(f, dict):
            continue
        card = f.get("noteCard") or f.get("note_card") or f
        user = card.get("user") or {}
        interact = card.get("interactInfo") or card.get("interact_info") or {}
        fid = f.get("id") or f.get("noteId") or f.get("note_id") or ""
        token = f.get("xsecToken") or f.get("xsec_token") or ""
        url = f"https://www.xiaohongshu.com/explore/{fid}" + (f"?xsec_token={token}" if token else "") if fid else ""
        items.append({
            "title": card.get("displayTitle") or card.get("display_title") or card.get("title") or "(无标题)",
            "url": url,
            "text": card.get("desc") or "",
            "meta": {"作者": user.get("nickname") or user.get("nickName") or "",
                     "点赞": interact.get("likedCount") or interact.get("liked_count"),
                     "评论": interact.get("commentCount") or interact.get("comment_count"),
                     "类型": card.get("type", ""),
                     "note_id": fid, "xsec_token": token},
        })
    return items


async def xiaohongshu_search(keyword: str, count: int = 10, sort_by: str = "综合",
                             publish_time: str = "不限") -> ToolResult:
    if not settings.xhs_mcp_url:
        return await site_search("xiaohongshu.com", keyword, platform="xiaohongshu", count=count,
                                 reason="未配置 XHS_MCP_URL（小红书 MCP 未运行）")
    # 实测：关键词含空格时 xiaohongshu-mcp 的搜索页会等待超时；中文关键词去掉空格直接拼接即可
    if re.search(r"[\u4e00-\u9fff]", keyword):
        keyword = "".join(keyword.split())
    sort_map = {"general": "综合", "latest": "最新", "newest": "最新", "most_liked": "最多点赞",
                "likes": "最多点赞", "most_commented": "最多评论", "comments": "最多评论"}
    sort_by = sort_map.get(str(sort_by).lower(), sort_by)
    if sort_by not in ("综合", "最新", "最多点赞", "最多评论"):
        sort_by = "综合"
    time_map = {"day": "一天内", "week": "一周内", "half_year": "半年内"}
    publish_time = time_map.get(str(publish_time).lower(), publish_time)

    # xiaohongshu-mcp 等待搜索页数据偶发超时（服务端 60s panic），带筛选失败后去掉筛选重试一次
    attempts: list[dict[str, str]] = [{"sort_by": sort_by}]
    if publish_time and publish_time != "不限":
        attempts.insert(0, {"sort_by": sort_by, "publish_time": publish_time})
    errors: list[str] = []
    for filters in attempts:
        try:
            data = await _mcp_call("search_feeds", {"keyword": keyword, "filters": filters})
            items = _parse_feeds(data)[: max(1, int(count))]
            if not items:
                errors.append(f"{filters} 无结果")
                continue
            note = "来自 xiaohongshu-mcp 实时搜索"
            if "publish_time" not in filters and publish_time not in ("", "不限"):
                note += f"（带 {publish_time} 筛选时失败，已改为不限时间，注意甄别旧帖）"
            return ToolResult(platform="xiaohongshu", items=items, note=note)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{filters} 失败: {str(exc)[:60]}")
    return await site_search("xiaohongshu.com", keyword, platform="xiaohongshu", count=count,
                             reason="小红书站内搜索无结果或超时（" + "；".join(errors) +
                                    "；时政类敏感话题可能被平台限流/屏蔽，报告中应如实说明而非当作没人讨论）")


async def xiaohongshu_note_detail(note_id: str, xsec_token: str = "", with_comments: bool = True) -> ToolResult:
    if not settings.xhs_mcp_url:
        return ToolResult(platform="xiaohongshu", items=[], ok=False, note="未配置 XHS_MCP_URL，无法读取笔记详情")
    try:
        data = await _mcp_call("get_feed_detail", {"feed_id": note_id, "xsec_token": xsec_token,
                                                   "load_all_comments": bool(with_comments), "limit": 20})
    except Exception as exc:  # noqa: BLE001
        return ToolResult(platform="xiaohongshu", items=[], ok=False, note=f"读取笔记失败: {exc}")
    text = json.dumps(data, ensure_ascii=False)[:6000] if data else ""
    return ToolResult(platform="xiaohongshu",
                      items=[{"title": f"小红书笔记 {note_id}", "url": f"https://www.xiaohongshu.com/explore/{note_id}",
                              "text": text}])


TOOLS = [
    ToolSpec(
        name="xiaohongshu_search",
        description="搜索小红书笔记（配置了本地 xiaohongshu-mcp 时为实时搜索，否则为搜索引擎索引）。"
                    "适合看普通网友/当事人视角、生活化讨论、图文爆料。实时搜索只返回标题与互动数，"
                    "要看正文和评论请对感兴趣的 note_id 调 xiaohongshu_note_detail。",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "count": {"type": "integer", "description": "默认 10"},
                "sort_by": {"type": "string", "enum": ["综合", "最新", "最多点赞", "最多评论"]},
                "publish_time": {"type": "string", "enum": ["不限", "一天内", "一周内", "半年内"],
                                 "description": "时事热点建议用 一周内，避免混入旧帖"},
            },
            "required": ["keyword"],
        },
        func=xiaohongshu_search,
    ),
    ToolSpec(
        name="xiaohongshu_note_detail",
        description="读取某条小红书笔记的正文与评论（需本地 xiaohongshu-mcp）。note_id 与 xsec_token 来自搜索结果。",
        parameters={
            "type": "object",
            "properties": {"note_id": {"type": "string"}, "xsec_token": {"type": "string"},
                           "with_comments": {"type": "boolean"}},
            "required": ["note_id"],
        },
        func=xiaohongshu_note_detail,
    ),
]
