"""微博。

- 热搜：默认走开源镜像（vikiboss/60s，实测可用，免登录）；若配置了 WEIBO_COOKIE（你自己浏览器的登录态），
  则直连 weibo.com/ajax/side/hotSearch 获取更完整的榜单字段。
- 关键词搜索：配置 WEIBO_COOKIE（桌面端 weibo.com 的 Cookie）时直连 s.weibo.com 搜索页并解析 HTML；
  否则走 DeepSeek site:weibo.com 兜底。（m.weibo.cn 的接口要求手机端登录态，桌面 Cookie 会被跳转到 passport）
微博对无登录态的访问做了限制（返回 432/302 到 passport），本项目不做绕过，只使用用户本人的 Cookie 或公开镜像。
"""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from ..config import USER_AGENT, settings
from . import ToolResult, ToolSpec
from .deepseek_search import site_search

HOT_MIRROR = os.getenv("WEIBO_HOT_MIRROR", "https://60s.viki.moe/v2/weibo").strip()
HOT_DIRECT = "https://weibo.com/ajax/side/hotSearch"


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").replace("​", "").strip()


def _topic_url(word: str) -> str:
    return f"https://s.weibo.com/weibo?q={quote(word)}"


async def _hot_via_mirror(limit: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=settings.request_timeout, headers={"User-Agent": USER_AGENT}) as client:
        r = await client.get(HOT_MIRROR)
        r.raise_for_status()
        data = r.json().get("data", [])
    items = []
    for rank, it in enumerate(data[:limit], 1):
        title = it.get("title", "")
        items.append({"title": f"#{rank} {title}", "url": it.get("link") or _topic_url(title),
                      "meta": {"热度": it.get("hot_value")}})
    return items


async def _hot_via_cookie(limit: int) -> list[dict[str, Any]]:
    headers = {"User-Agent": USER_AGENT, "Cookie": settings.weibo_cookie, "Referer": "https://weibo.com/"}
    async with httpx.AsyncClient(timeout=settings.request_timeout, headers=headers) as client:
        r = await client.get(HOT_DIRECT)
        r.raise_for_status()
        data = r.json().get("data", {})
    items = []
    for it in (data.get("realtime") or [])[:limit]:
        word = it.get("word", "")
        items.append({"title": f"#{it.get('rank', '?')} {word}", "url": _topic_url(word),
                      "meta": {"热度": it.get("num"), "标签": it.get("label_name") or "", "分类": it.get("category") or ""}})
    return items


async def weibo_hot_search(limit: int = 30) -> ToolResult:
    limit = max(1, min(int(limit), 50))
    errors = []
    if settings.weibo_cookie:
        try:
            return ToolResult(platform="weibo", items=await _hot_via_cookie(limit), note="微博热搜（直连，实时）")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"直连失败: {exc}")
    try:
        items = await _hot_via_mirror(limit)
        note = "微博热搜（公开镜像 60s API，实时）" + (f"；{errors[0]}" if errors else "")
        return ToolResult(platform="weibo", items=items, note=note)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"镜像失败: {exc}")
    return ToolResult(platform="weibo", items=[], ok=False, note="微博热搜获取失败: " + "；".join(errors))


SEARCH_PAGE = "https://s.weibo.com/weibo"
REALTIME_PAGE = "https://s.weibo.com/realtime"


def _norm_date(raw: str) -> str:
    """把 `今天13:00`、`10月26日 21:30`、`2025-10-26` 统一成 YYYY-MM-DD HH:MM。"""
    from datetime import datetime
    raw = " ".join(raw.split())
    now = datetime.now()
    m = re.match(r"(\d+)\s*(秒|分钟|小时)前", raw)
    if m:
        from datetime import timedelta
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(seconds=n) if unit == "秒" else timedelta(minutes=n) if unit == "分钟" else timedelta(hours=n)
        return f"{now - delta:%Y-%m-%d %H:%M}"
    m = re.match(r"今天\s*(\d{1,2}:\d{2})", raw)
    if m:
        return f"{now:%Y-%m-%d} {m.group(1)}"
    m = re.match(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}:\d{2})?", raw)
    if m:
        return f"{now.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" + (f" {m.group(3)}" if m.group(3) else "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    return raw if m else raw


def parse_search_page(html_text: str, limit: int) -> list[dict[str, Any]]:
    """解析 s.weibo.com 搜索结果页（桌面端 Cookie 可直接访问）。"""
    import html as _html
    items: list[dict[str, Any]] = []
    cards = re.split(r'<div class="card-wrap"[^>]*action-type="feed_list_item"', html_text)[1:]
    for card in cards:
        mid = re.search(r'mid="(\d+)"', card)
        text_m = (re.search(r'<p class="txt"[^>]*node-type="feed_list_content_full"[^>]*>(.*?)</p>', card, re.S)
                  or re.search(r'<p class="txt"[^>]*node-type="feed_list_content"[^>]*>(.*?)</p>', card, re.S))
        if not mid or not text_m:
            continue
        text = _html.unescape(_strip_html(text_m.group(1))).replace("收起d", "").replace("展开c", "").strip()
        nick = re.search(r'nick-name="([^"]*)"', card)
        from_m = re.search(r'<div class="from"\s*>\s*<a[^>]*href="([^"]*)"[^>]*>\s*([^<]*?)\s*</a>', card, re.S)
        href = from_m.group(1) if from_m else ""
        if href.startswith("//"):
            href = "https:" + href
        href = href.split("?")[0]
        act = card.split('class="card-act"', 1)[1] if 'class="card-act"' in card else ""
        act = act.split("</ul>", 1)[0]
        # 操作栏里三个数字依次是 转发、评论、赞（标签是图标，去标签后只剩数字）
        act_text = re.sub(r"<!--.*?-->", "", act, flags=re.S)
        nums = [int(x) for x in re.findall(r"\d+", " ".join(_strip_html(act_text).split()))]
        nums += [0] * (3 - len(nums))
        reposts, comments, likes = nums[:3]
        items.append({
            "title": text[:40] + ("…" if len(text) > 40 else ""),
            "url": href or f"https://weibo.com/detail/{mid.group(1)}",
            "date": _norm_date(from_m.group(2)) if from_m else "",
            "text": text,
            "meta": {"作者": _html.unescape(nick.group(1)) if nick else "", "转发": reposts,
                     "评论": comments, "点赞": likes},
        })
        if len(items) >= limit:
            break
    return items


async def _search_via_cookie(keyword: str, limit: int, sort: str = "综合") -> list[dict[str, Any]]:
    headers = {"User-Agent": USER_AGENT, "Cookie": settings.weibo_cookie.strip().strip('"'),
               "Referer": "https://weibo.com/"}
    if sort == "实时":
        url, params = REALTIME_PAGE, {"q": keyword}
    elif sort == "热门":
        url, params = SEARCH_PAGE, {"q": keyword, "xsort": "hot"}
    else:
        url, params = SEARCH_PAGE, {"q": keyword}
    async with httpx.AsyncClient(timeout=settings.request_timeout, headers=headers, follow_redirects=False) as client:
        r = await client.get(url, params=params)
        if r.status_code in (301, 302):
            raise RuntimeError("Cookie 未登录或已过期（被跳转到 passport）")
        r.raise_for_status()
        return parse_search_page(r.text, limit)


async def weibo_search(keyword: str, count: int = 10, sort: str = "综合") -> ToolResult:
    count = max(1, min(int(count), 20))
    sort = {"hot": "热门", "realtime": "实时", "latest": "实时", "general": "综合"}.get(str(sort).lower(), sort)
    if settings.weibo_cookie:
        try:
            items = await _search_via_cookie(keyword, count, sort)
            if items:
                return ToolResult(platform="weibo", items=items, note=f"微博站内搜索（直连，{sort}）")
            reason = "微博直连搜索无结果"
        except Exception as exc:  # noqa: BLE001
            reason = f"微博直连搜索失败（{exc}）"
    else:
        reason = "未配置 WEIBO_COOKIE，未直连微博搜索"
    return await site_search("weibo.com", keyword, platform="weibo", count=count, reason=reason)


TOOLS = [
    ToolSpec(
        name="weibo_hot_search",
        description="获取微博热搜榜（实时），返回话题词、热度值、话题链接。用于发现当下全网最热的瓜。",
        parameters={"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认 30"}}},
        func=weibo_hot_search,
    ),
    ToolSpec(
        name="weibo_search",
        description="按关键词搜索微博内容，看大众舆论、当事人/官方账号发声、传播热度（转评赞）。"
                    "sort=热门 看高互动帖，sort=实时 看最新动态。",
        parameters={
            "type": "object",
            "properties": {"keyword": {"type": "string"}, "count": {"type": "integer", "description": "默认 10"},
                           "sort": {"type": "string", "enum": ["综合", "热门", "实时"]}},
            "required": ["keyword"],
        },
        func=weibo_search,
    ),
]
