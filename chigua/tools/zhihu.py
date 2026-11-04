"""知乎。

- 热榜：公开接口，免登录（实测可用）。
- 站内搜索 / 问题下回答：需要你本人的 ZHIHU_COOKIE（含 d_c0、z_c0）并对每个请求计算 x-zse-96 签名
  （见 zhihu_sign.py）。未配置 Cookie 时搜索走 DeepSeek site:zhihu.com 兜底。
"""
from __future__ import annotations

import html
import re
from typing import Any

import httpx

from ..config import settings
from . import ToolResult, ToolSpec
from .deepseek_search import site_search
from .zhihu_sign import ZSE93, parse_cookie, sign

HOT_URL = "https://api.zhihu.com/topstory/hot-list"
BASE = "https://www.zhihu.com"
# 知乎对 UA 与 x-zse-93 版本有关联校验，用桌面 Chrome 即可
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def _strip(s: str, limit: int | None = None) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", "", s or ""))
    s = " ".join(s.split())
    return s[:limit] + "…" if limit and len(s) > limit else s


def _web_url(obj: dict[str, Any]) -> str:
    t, oid = obj.get("type", ""), obj.get("id", "")
    if t == "answer":
        qid = (obj.get("question") or {}).get("id", "")
        return f"{BASE}/question/{qid}/answer/{oid}" if qid else f"{BASE}/answer/{oid}"
    if t == "question":
        return f"{BASE}/question/{oid}"
    if t == "article":
        return f"https://zhuanlan.zhihu.com/p/{oid}"
    if t == "zvideo":
        return f"{BASE}/zvideo/{oid}"
    url = obj.get("url", "")
    return url.replace("api.zhihu.com/questions/", "www.zhihu.com/question/") if url else ""


class ZhihuWeb:
    """带签名的知乎 Web API 客户端（使用用户本人 Cookie）。"""

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie.strip().strip('"')
        jar = parse_cookie(self.cookie)
        self.d_c0 = jar.get("d_c0", "")
        self.xsrf = jar.get("_xsrf", "")
        if not self.d_c0:
            raise RuntimeError("ZHIHU_COOKIE 里没有 d_c0，请从浏览器复制完整 Cookie")

    def _headers(self, url: str, d_c0: str) -> dict[str, str]:
        h = {
            "user-agent": WEB_UA,
            "accept": "application/json, text/plain, */*",
            "referer": f"{BASE}/",
            "origin": BASE,
            "cookie": self.cookie,
            "x-requested-with": "fetch",
            "x-zse-93": ZSE93,
            "x-zse-96": sign(url, d_c0),
        }
        if self.xsrf:
            h["x-xsrf-token"] = self.xsrf
        return h

    async def get(self, path: str) -> Any:
        url = BASE + path
        # d_c0 在浏览器里带双引号，签名时两种写法都见过；先按原样，403 再去引号重试
        candidates = [self.d_c0]
        if self.d_c0.startswith('"'):
            candidates.append(self.d_c0.strip('"'))
        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=False) as client:
            for d in candidates:
                r = await client.get(url, headers=self._headers(url, d))
                if r.status_code == 200:
                    return r.json()
                try:
                    msg = r.json().get("error", {}).get("message", "")
                except Exception:  # noqa: BLE001
                    msg = r.text[:120]
                last_err = RuntimeError(f"HTTP {r.status_code} {msg}".strip())
                if r.status_code != 403:
                    break
        raise last_err or RuntimeError("未知错误")


def _client() -> ZhihuWeb | None:
    return ZhihuWeb(settings.zhihu_cookie) if settings.zhihu_cookie else None


# ---------------- 工具 ----------------
async def zhihu_hot_list(limit: int = 30) -> ToolResult:
    limit = max(1, min(int(limit), 50))
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout, headers={"User-Agent": WEB_UA}) as client:
            r = await client.get(HOT_URL, params={"limit": limit})
            r.raise_for_status()
            data = r.json().get("data", [])
    except Exception as exc:  # noqa: BLE001
        return ToolResult(platform="zhihu", items=[], ok=False, note=f"知乎热榜获取失败: {exc}")

    items = []
    for rank, entry in enumerate(data[:limit], 1):
        t = entry.get("target", {}) or {}
        url = (t.get("url") or "").replace("api.zhihu.com/questions/", "www.zhihu.com/question/")
        items.append({
            "title": f"#{rank} {t.get('title', '')}",
            "url": url,
            "text": (t.get("excerpt") or "").strip(),
            "meta": {"热度": entry.get("detail_text", ""), "回答数": t.get("answer_count"),
                     "关注数": t.get("follower_count"), "question_id": t.get("id")},
        })
    return ToolResult(platform="zhihu", items=items, note="知乎热榜（实时）")


def _parse_search(data: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in data.get("data", []):
        if entry.get("type") != "search_result":
            continue
        obj = entry.get("object") or {}
        t = obj.get("type", "")
        if t not in ("answer", "article", "question", "zvideo"):
            continue  # 跳过人物、圆桌等非内容结果
        title = _strip(obj.get("title") or (obj.get("question") or {}).get("name") or "")
        if t == "answer" and title:
            title = f"回答｜{title}"
        elif t == "article":
            title = f"专栏｜{title}"
        elif t == "question":
            title = f"问题｜{title}"
        author = (obj.get("author") or {}).get("name", "")
        created = obj.get("created_time") or obj.get("created") or 0
        date = ""
        if isinstance(created, (int, float)) and created > 0:
            from datetime import datetime
            date = datetime.fromtimestamp(created).strftime("%Y-%m-%d")
        items.append({
            "title": title or "(无标题)",
            "url": _web_url(obj),
            "date": date,
            "text": _strip(obj.get("excerpt") or obj.get("content") or "", 400),
            "meta": {"类型": t, "作者": author, "赞同": obj.get("voteup_count"),
                     "评论": obj.get("comment_count"),
                     "回答数": obj.get("answer_count") if t == "question" else None,
                     "question_id": (obj.get("question") or {}).get("id") or (obj.get("id") if t == "question" else None)},
        })
        if len(items) >= limit:
            break
    return items


async def zhihu_search(keyword: str, count: int = 8) -> ToolResult:
    count = max(1, min(int(count), 20))
    client = _client()
    if client is None:
        return await site_search("zhihu.com", keyword, platform="zhihu", count=count,
                                 reason="未配置 ZHIHU_COOKIE，未直连知乎搜索")
    try:
        from urllib.parse import quote
        # 返回列表里混着广告、热点卡片、相关搜索等非内容条目，多取一倍再过滤
        fetch_n = min(max(count * 2, 10), 20)
        data = await client.get(f"/api/v4/search_v3?t=general&q={quote(keyword)}&correction=1&offset=0&limit={fetch_n}")
        items = _parse_search(data, count)
        if not items:
            raise RuntimeError("站内搜索无结果")
        return ToolResult(platform="zhihu", items=items, note="知乎站内搜索（直连，实时）")
    except Exception as exc:  # noqa: BLE001
        return await site_search("zhihu.com", keyword, platform="zhihu", count=count,
                                 reason=f"知乎直连搜索失败（{str(exc)[:80]}）")


async def zhihu_question_answers(question_id: str, count: int = 5, order: str = "normal") -> ToolResult:
    m = re.search(r"(\d{6,})", str(question_id))
    if not m:
        return ToolResult(platform="zhihu", items=[], ok=False, note="question_id 无效，需要数字 ID 或问题链接")
    qid = m.group(1)
    count = max(1, min(int(count), 10))
    client = _client()
    if client is None:
        return ToolResult(platform="zhihu", items=[], ok=False, note="未配置 ZHIHU_COOKIE，无法读取问题下的回答")
    try:
        order = order if order in ("normal", "updated") else "normal"
        data = await client.get(f"/api/v4/questions/{qid}/answers?include=content,voteup_count,comment_count,"
                                f"author,created_time,question&limit={count}&offset=0&order={order}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(platform="zhihu", items=[], ok=False, note=f"读取回答失败: {str(exc)[:120]}")
    items = []
    for a in data.get("data", []):
        q = a.get("question") or {}
        from datetime import datetime
        created = a.get("created_time") or 0
        items.append({
            "title": f"回答｜{_strip(q.get('title') or '')}",
            "url": f"{BASE}/question/{q.get('id', qid)}/answer/{a.get('id', '')}",
            "date": datetime.fromtimestamp(created).strftime("%Y-%m-%d") if created else "",
            "text": _strip(a.get("content") or "", 1500),
            "meta": {"作者": (a.get("author") or {}).get("name", ""), "赞同": a.get("voteup_count"),
                     "评论": a.get("comment_count")},
        })
    return ToolResult(platform="zhihu", items=items, note=f"知乎问题 {qid} 下的回答（直连）")


TOOLS = [
    ToolSpec(
        name="zhihu_hot_list",
        description="获取知乎热榜（实时、免登录），返回标题、热度、回答数、问题链接。用于发现今天大家在讨论什么。",
        parameters={"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认 30"}}},
        func=zhihu_hot_list,
    ),
    ToolSpec(
        name="zhihu_search",
        description="知乎站内搜索问题/回答/专栏（配置 Cookie 时实时直连，否则为搜索引擎索引）。"
                    "适合看理性分析、专业视角、长文梳理。结果带 question_id，可用 zhihu_question_answers 深入。",
        parameters={
            "type": "object",
            "properties": {"keyword": {"type": "string"}, "count": {"type": "integer", "description": "默认 8"}},
            "required": ["keyword"],
        },
        func=zhihu_search,
    ),
    ToolSpec(
        name="zhihu_question_answers",
        description="读取某个知乎问题下的高赞回答正文（需 Cookie）。question_id 为数字 ID 或问题链接。",
        parameters={
            "type": "object",
            "properties": {"question_id": {"type": "string"}, "count": {"type": "integer", "description": "默认 5"},
                           "order": {"type": "string", "enum": ["normal", "updated"]}},
            "required": ["question_id"],
        },
        func=zhihu_question_answers,
    ),
]
