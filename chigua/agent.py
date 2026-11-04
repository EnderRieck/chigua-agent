"""吃瓜智能体的编排核心：规划员（Planner）→ 采集（Gather）→ 分析员（Analyst，带工具循环）→ 报告。

与 C3 作业 v5 的“感知-决策-行动”骨架一脉相承：
- 感知：读取用户问题 + 对话历史 + 已有证据索引
- 决策：规划员产出 JSON 检索计划；分析员在写作中按需追加工具调用
- 行动：工具注册表并发执行各平台适配器，结果统一进证据库
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import settings
from .evidence import EvidenceStore
from .llm import LLM
from .prompts import ANALYST_SYSTEM, FOLLOWUP_HINT, PLANNER_SYSTEM, today
from .tools import ToolRegistry, build_registry, parse_args

EventHandler = Callable[[str, Any], None]
_CITE_RE = re.compile(r"\[E(\d+)\]")


def cited_ids(text: str) -> list[str]:
    seen: list[str] = []
    for m in _CITE_RE.finditer(text or ""):
        eid = f"E{m.group(1)}"
        if eid not in seen:
            seen.append(eid)
    return seen


@dataclass
class RunResult:
    question: str
    plan: dict[str, Any]
    answer: str                      # 分析员正文
    report: str                      # 正文 + 自动附加的信息来源
    evidence_ids: list[str] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)


class ChiguaAgent:
    def __init__(self, llm: LLM | None = None, registry: ToolRegistry | None = None,
                 on_event: EventHandler | None = None, max_rounds: int | None = None) -> None:
        self.llm = llm or LLM()
        self.registry = registry or build_registry()
        self.on_event = on_event or (lambda kind, payload: None)
        self.max_rounds = settings.max_tool_rounds if max_rounds is None else max_rounds
        self.store = EvidenceStore()
        self.history: list[dict[str, str]] = []   # 仅保留 user/assistant 文本，跨轮追问用
        self.last_result: RunResult | None = None

    # ---------------- 对外入口 ----------------
    def reset(self) -> None:
        self.store = EvidenceStore()
        self.history.clear()
        self.last_result = None

    async def run(self, question: str) -> RunResult:
        emit = self.on_event
        followup = bool(self.history)

        emit("stage", "规划检索")
        plan = await asyncio.to_thread(self.plan, question, followup)
        emit("plan", plan)

        gathered: list[str] = []
        tool_log: list[dict[str, Any]] = []
        queries = plan.get("queries") or []
        if plan.get("needs_research", True) and queries:
            emit("stage", f"多平台采集（{len(queries)} 项并发）")
            gathered, tool_log = await self.gather(queries)
            emit("gather_done", tool_log)

        emit("stage", "分析撰写")
        answer, extra_log = await self.analyze(question, plan, gathered, followup)
        tool_log.extend(extra_log)

        ids = cited_ids(answer)
        if not ids:  # 模型没标引用时，列出本轮用到的全部证据（最多 30 条）
            ids = [e.id for e in self.store.all()][-30:]
        src_lines = []
        for eid in ids:
            ev = self.store.get(eid)
            if ev:
                link = f"（{ev.url}）" if ev.url else ""
                src_lines.append(f"- [{ev.id}] <{ev.platform}> {ev.title}{link}")
        report = answer.rstrip() + ("\n\n## 信息来源\n" + "\n".join(src_lines) if src_lines else "")

        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        self.history = self.history[-8:]
        result = RunResult(question=question, plan=plan, answer=answer, report=report,
                           evidence_ids=ids, tool_log=tool_log)
        self.last_result = result
        emit("final", result)
        return result

    # ---------------- 规划员 ----------------
    def plan(self, question: str, followup: bool) -> dict[str, Any]:
        system = PLANNER_SYSTEM.format(catalog=self.registry.catalog_text(), today=today())
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        if followup:
            messages.extend(self.history[-4:])
            if self.store:
                index = "\n".join(f"[{e.id}] <{e.platform}> {e.title}" for e in self.store.all()[-40:])
                messages.append({"role": "system", "content": f"已有证据索引（供判断是否需要新检索）：\n{index}"})
        messages.append({"role": "user", "content": question})
        try:
            plan = self.llm.complete_json(messages)
            if not isinstance(plan, dict):
                raise ValueError("计划不是 JSON 对象")
        except Exception as exc:  # noqa: BLE001 — 规划失败就退化为一条通用搜索
            plan = {"needs_research": True, "topic": question[:40], "aliases": [],
                    "queries": [{"tool": "web_search", "args": {"query": question[:100]}, "why": "规划失败兜底"}],
                    "questions": [], "note": f"规划员输出解析失败（{exc}），使用兜底计划"}
        # 过滤掉不存在的工具
        valid = []
        for q in plan.get("queries") or []:
            if isinstance(q, dict) and self.registry.get(str(q.get("tool", ""))):
                q["args"] = q.get("args") if isinstance(q.get("args"), dict) else {}
                valid.append(q)
        plan["queries"] = valid[:8]
        plan.setdefault("topic", question[:40])
        plan.setdefault("questions", [])
        return plan

    # ---------------- 并发采集 ----------------
    async def gather(self, queries: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
        async def one(q: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            name, args = str(q["tool"]), dict(q.get("args") or {})
            self.on_event("tool_start", {"tool": name, "args": args})
            text, res = await self.registry.call(name, args, self.store, timeout=settings.request_timeout * 2)
            entry = {"tool": name, "args": args, "ok": res.ok, "count": len(res.items), "note": res.note}
            self.on_event("tool_done", entry)
            header = f"### 工具 {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"
            return f"{header}\n{text}", entry

        pairs = await asyncio.gather(*(one(q) for q in queries))
        return [p[0] for p in pairs], [p[1] for p in pairs]

    # ---------------- 分析员（带工具循环） ----------------
    def _user_prompt(self, question: str, plan: dict[str, Any], gathered: list[str], followup: bool) -> str:
        parts = [f"用户问题：{question}"]
        if followup:
            parts.append(FOLLOWUP_HINT)
        parts.append(f"规划员判断的事件：{plan.get('topic', '')}")
        if plan.get("questions"):
            parts.append("核心问题：\n" + "\n".join(f"- {q}" for q in plan["questions"]))
        if gathered:
            parts.append("=== 本轮采集到的证据 ===\n" + "\n\n".join(gathered))
        elif followup and self.store:
            index = "\n".join(e.digest(max_text=160) for e in self.store.all()[-40:])
            parts.append("=== 已有证据（上轮采集）===\n" + index)
        else:
            parts.append("（本轮未采集到新证据）")
        return "\n\n".join(parts)

    async def analyze(self, question: str, plan: dict[str, Any], gathered: list[str],
                      followup: bool) -> tuple[str, list[dict[str, Any]]]:
        emit = self.on_event
        system = ANALYST_SYSTEM.format(today=today(), max_rounds=self.max_rounds)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(self.history[-6:])
        messages.append({"role": "user", "content": self._user_prompt(question, plan, gathered, followup)})
        schemas = self.registry.schemas()
        log: list[dict[str, Any]] = []
        text = ""

        for rnd in range(self.max_rounds + 1):
            tools = schemas if rnd < self.max_rounds else None
            emit("text_reset", None)
            text, calls = "", []
            for kind, payload in self.llm.stream(messages, tools=tools):
                if kind == "text":
                    emit("text", payload)
                else:
                    text, calls = payload["text"], payload["tool_calls"]
            if not calls:
                return text, log

            messages.append({
                "role": "assistant", "content": text or None,
                "tool_calls": [{"id": c["id"], "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}} for c in calls],
            })
            emit("tool_round", {"round": rnd + 1, "calls": calls, "note": text})
            results = await asyncio.gather(
                *(self.registry.call(c["name"], parse_args(c["arguments"]), self.store,
                                     timeout=settings.request_timeout * 2) for c in calls))
            for c, (txt, res) in zip(calls, results):
                entry = {"tool": c["name"], "args": parse_args(c["arguments"]), "ok": res.ok,
                         "count": len(res.items), "note": res.note, "round": rnd + 1}
                log.append(entry)
                emit("tool_done", entry)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": txt})
        return text, log
