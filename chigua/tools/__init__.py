"""工具注册表：把各平台适配器统一成 OpenAI function-calling 的 schema，并负责分发执行。"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..evidence import EvidenceStore


@dataclass
class ToolResult:
    platform: str
    items: list[dict[str, Any]]          # 每项至少含 title；可含 url/date/text/meta
    note: str = ""                       # 给模型看的说明（例如“已降级为 DeepSeek 搜索”）
    ok: bool = True


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Awaitable[ToolResult]]
    timeout: float | None = None   # 工具自己的总时限（覆盖调用方给的默认值）

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def catalog_text(self) -> str:
        lines = []
        for t in self._tools.values():
            props = t.parameters.get("properties", {})
            args = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items())
            lines.append(f"- {t.name}({args}) — {t.description}")
        return "\n".join(lines)

    async def call(self, name: str, args: dict[str, Any], store: EvidenceStore,
                   timeout: float = 60.0) -> tuple[str, ToolResult]:
        """执行工具，把结果写入证据库，并返回给模型看的文本。"""
        spec = self._tools.get(name)
        if spec is None:
            res = ToolResult(platform="system", items=[], note=f"未知工具 {name}", ok=False)
            return res.note, res
        # 丢弃模型瞎编的参数
        sig = inspect.signature(spec.func)
        clean = {k: v for k, v in args.items() if k in sig.parameters}
        t0 = time.time()
        budget = spec.timeout or timeout
        try:
            res = await asyncio.wait_for(spec.func(**clean), timeout=budget)
        except asyncio.TimeoutError:
            res = ToolResult(platform="system", items=[], note=f"{name} 超时（>{budget:.0f}s）", ok=False)
        except Exception as exc:  # noqa: BLE001 — 工具失败不应打断智能体
            res = ToolResult(platform="system", items=[], note=f"{name} 执行失败: {type(exc).__name__}: {exc}", ok=False)
        elapsed = time.time() - t0

        lines = []
        if res.note:
            lines.append(f"[说明] {res.note}")
        if not res.items:
            lines.append("（无结果）")
        for it in res.items:
            ev = store.add(platform=res.platform, title=str(it.get("title", "")), url=str(it.get("url", "") or ""),
                           date=str(it.get("date", "") or ""), text=str(it.get("text", "") or ""),
                           meta=it.get("meta") or {})
            lines.append(ev.digest())
        lines.append(f"（{name} 用时 {elapsed:.1f}s，共 {len(res.items)} 条）")
        return "\n".join(lines), res


def build_registry() -> ToolRegistry:
    from . import deepseek_search, weibo, zhihu, xhs, x_search

    reg = ToolRegistry()
    for mod in (deepseek_search, weibo, zhihu, xhs, x_search):
        for spec in mod.TOOLS:
            reg.register(spec)
    return reg


def parse_args(raw: str | dict | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    raw = raw.strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        # 有的中转站会把两个 JSON 拼在一起，取第一个
        depth, start = 0, None
        for i, ch in enumerate(raw):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(raw[start:i + 1])
                    except json.JSONDecodeError:
                        return {}
        return {}
