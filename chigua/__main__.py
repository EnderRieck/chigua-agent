"""命令行入口。

  python -m chigua                 交互式吃瓜对话
  python -m chigua ask "某事件"     一次性分析并输出报告
  python -m chigua hot             看今天微博热搜 + 知乎热榜
  python -m chigua doctor          自检：Key、各平台接口是否可用
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import settings

console = Console()


def _make_reader():
    """返回一个 read(prompt) 函数。优先用 prompt_toolkit：正确处理中文宽字符的退格、支持上下键翻历史；
    非交互终端或未安装时退化为 input()。"""
    if sys.stdin.isatty():
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.formatted_text import HTML

            session = PromptSession()

            def read(prompt: str) -> str:
                return session.prompt(HTML(f"\n<ansigreen><b>{prompt}</b></ansigreen> > "))

            return read
        except ImportError:
            pass

    def read_plain(prompt: str) -> str:
        return input(f"\n{prompt} > ")

    return read_plain

# ---------------------------------------------------------------- UI 事件处理
class ConsoleUI:
    """把智能体事件渲染到终端：阶段提示、检索计划、工具进度、流式正文。"""

    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self.live: Live | None = None
        self.buffer = ""

    def _ensure_live(self) -> None:
        if self.live is None:
            self.live = Live(self._render(), console=console, refresh_per_second=8, transient=True)
            self.live.start()

    def _render(self) -> Panel:
        tail = "\n".join(self.buffer.splitlines()[-14:])
        return Panel(Text(tail or "…"), title="✍️ 分析员撰写中", border_style="magenta")

    def _stop_live(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None

    def __call__(self, kind: str, payload: Any) -> None:
        if kind == "stage":
            self._stop_live()
            console.print(f"\n[bold cyan]▶ {payload}[/]")
        elif kind == "plan":
            self._print_plan(payload)
        elif kind == "tool_start":
            if self.verbose:
                console.print(f"  [dim]⏳ {payload['tool']} {_fmt_args(payload['args'])}[/]")
        elif kind == "tool_done":
            mark = "[green]✓[/]" if payload["ok"] else "[red]✗[/]"
            note = f" [dim]— {payload['note']}[/]" if payload.get("note") else ""
            console.print(f"  {mark} {payload['tool']} {_fmt_args(payload['args'])} → {payload['count']} 条{note}")
        elif kind == "gather_done":
            ok = sum(1 for t in payload if t["ok"])
            console.print(f"  [dim]采集完成：{ok}/{len(payload)} 项成功[/]")
        elif kind == "text_reset":
            self.buffer = ""
            if self.live is not None:
                self.live.update(self._render())
        elif kind == "text":
            self.buffer += payload
            self._ensure_live()
            self.live.update(self._render())  # type: ignore[union-attr]
        elif kind == "retry":
            self._stop_live()
            console.print(f"  [yellow]↻ {payload}[/]")
        elif kind == "tool_round":
            self._stop_live()
            note = (payload.get("note") or "").strip()
            if note and self.verbose:
                console.print(Panel(Text(note[:600]), title="分析员备注", border_style="dim"))
            names = ", ".join(c["name"] for c in payload["calls"])
            console.print(f"  [yellow]↻ 第 {payload['round']} 轮补充检索：{names}[/]")
        elif kind == "final":
            self._stop_live()
            console.print()
            console.print(Markdown(payload.report))
            console.print()

    def _print_plan(self, plan: dict[str, Any]) -> None:
        if not plan.get("needs_research", True) or not plan.get("queries"):
            console.print(f"  [dim]无需新检索{('：' + plan['note']) if plan.get('note') else ''}[/]")
            return
        table = Table(title=f"检索计划 · {plan.get('topic', '')}", show_lines=False, title_justify="left")
        table.add_column("工具", style="cyan", no_wrap=True)
        table.add_column("参数")
        table.add_column("理由", style="dim")
        for q in plan["queries"]:
            table.add_row(q["tool"], _fmt_args(q.get("args", {})), str(q.get("why", "")))
        console.print(table)
        if plan.get("questions"):
            console.print("  [dim]核心问题：" + "；".join(plan["questions"]) + "[/]")


def _fmt_args(args: dict[str, Any]) -> str:
    return " ".join(f"{k}={v!r}" for k, v in args.items() if v not in (None, ""))


# ---------------------------------------------------------------- 子命令
def _make_agent(ui: ConsoleUI):
    from .agent import ChiguaAgent
    try:
        return ChiguaAgent(on_event=ui)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]  （复制 .env.example 为 .env 并填写后重试）")
        sys.exit(2)


def save_report(agent, path: Path | None = None) -> Path | None:
    res = agent.last_result
    if res is None:
        console.print("[yellow]还没有可保存的报告[/]")
        return None
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    if path is None:
        slug = re.sub(r"[^\w一-鿿]+", "_", str(res.plan.get("topic") or res.question))[:40].strip("_")
        path = settings.reports_dir / f"{datetime.now():%Y%m%d-%H%M}-{slug or 'report'}.md"
    header = f"<!-- 吃瓜智能体 v{__version__} · {datetime.now():%Y-%m-%d %H:%M} · 问题：{res.question} -->\n\n"
    path.write_text(header + res.report + "\n", encoding="utf-8")
    console.print(f"[green]已保存：{path}[/]")
    return path


def cmd_ask(args: argparse.Namespace) -> None:
    ui = ConsoleUI(verbose=not args.quiet)
    agent = _make_agent(ui)
    asyncio.run(agent.run(" ".join(args.topic)))
    if args.save:
        save_report(agent)


async def _fetch_hot(limit: int):
    from .tools.weibo import weibo_hot_search
    from .tools.zhihu import zhihu_hot_list
    return await asyncio.gather(weibo_hot_search(limit), zhihu_hot_list(limit))


def _print_hot(limit: int) -> dict[str, str]:
    """打印双榜并返回 {编号: 标题}，供 /dig 使用。"""
    with console.status("拉取热榜中…"):
        wb, zh = asyncio.run(_fetch_hot(limit))
    mapping: dict[str, str] = {}
    table = Table(title=f"🍉 今日热榜 · {datetime.now():%m-%d %H:%M}", title_justify="left")
    table.add_column("微博热搜", ratio=1)
    table.add_column("知乎热榜", ratio=1)
    rows = max(len(wb.items), len(zh.items))
    for i in range(rows):
        cells = []
        for prefix, res in (("W", wb), ("Z", zh)):
            if i < len(res.items):
                title = re.sub(r"^#\d+\s*", "", res.items[i]["title"])
                heat = res.items[i].get("meta", {}).get("热度")
                mapping[f"{prefix}{i + 1}"] = title
                cells.append(f"[cyan]{prefix}{i + 1}[/] {title}" + (f" [dim]{heat}[/]" if heat else ""))
            else:
                cells.append("")
        table.add_row(*cells)
    console.print(table)
    for res in (wb, zh):
        if not res.ok:
            console.print(f"[red]{res.note}[/]")
    return mapping


def cmd_hot(args: argparse.Namespace) -> None:
    _print_hot(args.limit)


def cmd_chat(args: argparse.Namespace) -> None:
    guide = (
        f"[bold]🍉 吃瓜智能体 v{__version__}[/]  [dim]模型 {settings.xai_model}[/]\n\n"
        "直接输入想吃的瓜，例如：[italic]华晨宇新歌是怎么回事[/]\n"
        "分析完可以继续追问，例如：[italic]知乎那边怎么看[/]\n\n"
        "[cyan]/hot[/]      看微博热搜 + 知乎热榜     [cyan]/dig W3[/]   深挖榜单第 3 条（Z5 为知乎第 5 条）\n"
        "[cyan]/sources[/]  列出本轮全部证据与链接    [cyan]/save[/]     把报告保存到 reports/\n"
        "[cyan]/new[/]      清空上下文，换个瓜        [cyan]/quit[/]     退出（Ctrl-C 可中断当前分析）\n"
        "[cyan]/help[/]     再看一遍这份说明"
    )
    console.print(Panel(guide, border_style="green", padding=(0, 1)))
    ui = ConsoleUI(verbose=not args.quiet)
    agent = _make_agent(ui)
    hot_map: dict[str, str] = {}
    read = _make_reader()
    while True:
        try:
            line = read("你").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见，瓜田留步 🍉")
            break
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            console.print("再见，瓜田留步 🍉")
            break
        if line == "/help":
            console.print(Panel(guide, border_style="green", padding=(0, 1)))
            continue
        if line == "/hot":
            hot_map = _print_hot(args.limit)
            continue
        if line.startswith("/dig"):
            key = line[4:].strip().upper()
            if key not in hot_map:
                console.print("[yellow]先 /hot 看榜单，再 /dig W3 这样指定编号[/]")
                continue
            line = f"热搜「{hot_map[key]}」是怎么回事？帮我吃这个瓜"
        elif line == "/sources":
            if not agent.store:
                console.print("[yellow]还没有证据[/]")
            else:
                console.print(Markdown(agent.store.sources_markdown()))
            continue
        elif line == "/save":
            save_report(agent)
            continue
        elif line == "/new":
            agent.reset()
            console.print("[dim]上下文已清空[/]")
            continue
        elif line.startswith("/"):
            console.print("[yellow]未知命令，/help 查看[/]")
            continue
        try:
            asyncio.run(agent.run(line))
        except KeyboardInterrupt:
            ui._stop_live()
            console.print("[yellow]已中断本轮[/]")
        except Exception as exc:  # noqa: BLE001
            ui._stop_live()
            console.print(f"[red]本轮失败：{type(exc).__name__}: {exc}[/]")


def cmd_doctor(args: argparse.Namespace) -> None:
    from .tools.deepseek_search import raw_search

    rows: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, note: str) -> None:
        rows.append((name, ok, note))

    add("XAI_API_KEY", settings.has_llm, f"base_url={settings.xai_base_url} model={settings.xai_model}")
    add("DEEPSEEK_API_KEY", settings.has_deepseek, settings.deepseek_base_url)
    add("WEIBO_COOKIE", bool(settings.weibo_cookie),
        "已配置" if settings.weibo_cookie else "未配置：热搜走公开镜像，搜索走 DeepSeek 兜底")
    add("XHS_MCP_URL", bool(settings.xhs_mcp_url), settings.xhs_mcp_url or "未配置：小红书走 DeepSeek 兜底")
    add("X_SEARCH_MODE", True, settings.x_search_mode)

    with console.status("检查各接口…"):
        if settings.has_llm:
            try:
                from .llm import LLM
                add("LLM 对话", True, f"回复：{LLM().ping()!r}")
            except Exception as exc:  # noqa: BLE001
                add("LLM 对话", False, f"{type(exc).__name__}: {str(exc)[:140]}")
        if settings.has_deepseek:
            try:
                res = asyncio.run(raw_search("今日热点", count=2))
                add("DeepSeek Search", True, f"返回 {len(res)} 条")
            except Exception as exc:  # noqa: BLE001
                add("DeepSeek Search", False, f"{type(exc).__name__}: {str(exc)[:140]}")
        wb, zh = asyncio.run(_fetch_hot(5))
        add("微博热搜", wb.ok, wb.note)
        if settings.weibo_cookie:
            from .tools.weibo import weibo_search
            ws = asyncio.run(weibo_search("人工智能", count=2))
            add("微博 Cookie 搜索", ws.note.startswith("微博站内搜索"), ws.note[:140])
        add("知乎热榜", zh.ok, zh.note)
        if settings.zhihu_cookie:
            from .tools.zhihu import zhihu_search
            zs = asyncio.run(zhihu_search("人工智能", count=2))
            add("知乎 Cookie 搜索", zs.note.startswith("知乎站内搜索"), zs.note[:140])
        else:
            add("ZHIHU_COOKIE", False, "未配置：站内搜索走 DeepSeek 兜底")
        if settings.xhs_mcp_url:
            try:
                from .tools.mcp_client import open_mcp_session, text_of

                async def _xhs_status() -> str:
                    async with open_mcp_session(settings.xhs_mcp_url, settings.xhs_mcp_token) as session:
                        return text_of(await session.call_tool("check_login_status", {}))

                status = " ".join(asyncio.run(_xhs_status()).split())
                add("小红书 MCP", "已登录" in status, status[:140])
            except Exception as exc:  # noqa: BLE001
                add("小红书 MCP", False, f"{type(exc).__name__}: {str(exc)[:140]}")
        if args.full and settings.has_llm:
            from .tools.x_search import x_search
            res = asyncio.run(x_search("OpenAI", count=3, days=3))
            add("X 搜索", res.ok, res.note)

    table = Table(title="吃瓜智能体自检", title_justify="left")
    table.add_column("项目", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("说明")
    for name, ok, note in rows:
        table.add_row(name, "[green]✓[/]" if ok else "[red]✗[/]", note)
    console.print(table)
    if not args.full:
        console.print("[dim]加 --full 会额外实测 X 搜索链路（会消耗一次模型调用）[/]")


# ---------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="chigua", description="🍉 吃瓜智能体：多平台时事热点解析")
    parser.add_argument("--quiet", action="store_true", help="少打印工具过程")
    parser.add_argument("--limit", type=int, default=20, help="热榜条数（默认 20）")
    sub = parser.add_subparsers(dest="cmd")

    p_ask = sub.add_parser("ask", help="一次性分析某个事件")
    p_ask.add_argument("topic", nargs="+")
    p_ask.add_argument("--save", action="store_true", help="分析完保存到 reports/")
    p_ask.set_defaults(func=cmd_ask)

    p_hot = sub.add_parser("hot", help="看今日热榜")
    p_hot.set_defaults(func=cmd_hot)

    p_doc = sub.add_parser("doctor", help="自检配置与接口")
    p_doc.add_argument("--full", action="store_true")
    p_doc.set_defaults(func=cmd_doctor)

    p_chat = sub.add_parser("chat", help="交互式对话（默认）")
    p_chat.set_defaults(func=cmd_chat)

    args = parser.parse_args(argv)
    if args.cmd is None:
        args.func = cmd_chat
    args.func(args)


if __name__ == "__main__":
    main()
