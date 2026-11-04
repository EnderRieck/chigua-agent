"""证据库：所有工具返回的条目统一编号为 [E1]、[E2]…，供分析员引用、供报告列来源。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    id: str
    platform: str          # web / weibo / zhihu / xiaohongshu / x
    title: str
    url: str = ""
    date: str = ""
    text: str = ""         # 摘要 / 正文片段
    meta: dict[str, Any] = field(default_factory=dict)  # 点赞数、作者等

    def digest(self, max_text: int = 320) -> str:
        bits = [f"[{self.id}] <{self.platform}> {self.title.strip()}"]
        if self.date:
            bits.append(f"  日期: {self.date}")
        if self.meta:
            kv = " ".join(f"{k}={v}" for k, v in self.meta.items() if v not in (None, "", 0))
            if kv:
                bits.append(f"  数据: {kv}")
        if self.text:
            t = " ".join(self.text.split())
            bits.append(f"  内容: {t[:max_text]}{'…' if len(t) > max_text else ''}")
        if self.url:
            bits.append(f"  链接: {self.url}")
        return "\n".join(bits)


class EvidenceStore:
    def __init__(self) -> None:
        self._items: list[Evidence] = []
        self._by_url: dict[str, Evidence] = {}

    def __len__(self) -> int:
        return len(self._items)

    def all(self) -> list[Evidence]:
        return list(self._items)

    def get(self, eid: str) -> Evidence | None:
        for e in self._items:
            if e.id == eid:
                return e
        return None

    def add(self, platform: str, title: str, url: str = "", date: str = "",
            text: str = "", meta: dict[str, Any] | None = None) -> Evidence:
        key = url.strip()
        if key and key in self._by_url:
            ev = self._by_url[key]
            # 同一链接再次出现时合并更长的正文
            if len(text or "") > len(ev.text):
                ev.text = text
            return ev
        ev = Evidence(id=f"E{len(self._items) + 1}", platform=platform, title=title or "(无标题)",
                      url=key, date=date or "", text=text or "", meta=meta or {})
        self._items.append(ev)
        if key:
            self._by_url[key] = ev
        return ev

    def sources_markdown(self) -> str:
        lines = []
        for e in self._items:
            link = f"（{e.url}）" if e.url else ""
            lines.append(f"- [{e.id}] <{e.platform}> {e.title}{link}")
        return "\n".join(lines)
