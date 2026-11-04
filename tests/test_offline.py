"""离线单元测试：不访问网络、不需要 API Key。"""
from __future__ import annotations

import asyncio

import pytest

from chigua.agent import cited_ids
from chigua.evidence import EvidenceStore
from chigua.llm import extract_json
from chigua.tools import ToolRegistry, ToolResult, ToolSpec, parse_args
from chigua.tools.x_search import parse_posts
from chigua.tools.xhs import _parse_feeds


def test_parse_args_variants():
    assert parse_args(None) == {}
    assert parse_args("") == {}
    assert parse_args('{"query": "a"}') == {"query": "a"}
    assert parse_args({"k": 1}) == {"k": 1}
    # 中转站把两个 JSON 拼在一起时取第一个
    assert parse_args('{"query": "a"}{"query": "b"}') == {"query": "a"}
    assert parse_args("not json") == {}


def test_extract_json_with_fence_and_noise():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('好的，计划如下：{"a": [1, 2]} 请查收') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("完全没有 json")


def test_evidence_store_ids_and_dedupe():
    store = EvidenceStore()
    e1 = store.add("web", "标题一", url="https://a.com/1", text="短")
    e2 = store.add("weibo", "标题二", url="https://a.com/2")
    e3 = store.add("web", "标题一(重复)", url="https://a.com/1", text="更长的正文")
    assert (e1.id, e2.id) == ("E1", "E2")
    assert e3 is e1 and e1.text == "更长的正文"
    assert len(store) == 2
    digest = e1.digest()
    assert digest.startswith("[E1] <web> 标题一") and "https://a.com/1" in digest
    assert "[E2]" in store.sources_markdown()


def test_cited_ids_order_and_unique():
    assert cited_ids("甲 [E3] 乙 [E1] 丙 [E3]") == ["E3", "E1"]
    assert cited_ids("") == []


def test_registry_call_handles_failure_and_unknown_args():
    async def boom(keyword: str) -> ToolResult:
        raise RuntimeError("炸了")

    async def fine(keyword: str, count: int = 2) -> ToolResult:
        return ToolResult(platform="weibo", items=[{"title": f"{keyword}-{i}", "url": f"u{i}"} for i in range(count)],
                          note="ok")

    reg = ToolRegistry()
    reg.register(ToolSpec("boom", "d", {"type": "object", "properties": {"keyword": {"type": "string"}}}, boom))
    reg.register(ToolSpec("fine", "d", {"type": "object", "properties": {"keyword": {"type": "string"}}}, fine))
    store = EvidenceStore()

    text, res = asyncio.run(reg.call("boom", {"keyword": "x"}, store))
    assert not res.ok and "炸了" in text

    text, res = asyncio.run(reg.call("fine", {"keyword": "k", "count": 3, "made_up": 1}, store))
    assert res.ok and len(res.items) == 3 and "[E1]" in text and "[E3]" in text

    text, res = asyncio.run(reg.call("nope", {}, store))
    assert not res.ok and "未知工具" in text
    assert "fine(" in reg.catalog_text() and reg.schemas()[0]["type"] == "function"


def test_parse_posts_json_and_fallback():
    raw = '```json\n[{"author": "@a", "date": "2026-10-20", "text": "hello", "url": "https://x.com/a/1", "likes": 5}]\n```'
    items = parse_posts(raw, [])
    assert items[0]["url"] == "https://x.com/a/1" and items[0]["meta"]["点赞"] == 5
    # 非 JSON 输出：退化为综述 + 引用链接
    items = parse_posts("大家都在讨论……", ["https://x.com/1", "https://x.com/2"])
    assert items[0]["title"] == "X 平台讨论综述" and items[0]["url"] == "https://x.com/1"
    assert len(items) == 2


def test_parse_xhs_feeds_loose_structure():
    data = {"feeds": [{"id": "abc", "xsecToken": "tok",
                       "noteCard": {"displayTitle": "标题", "desc": "正文", "user": {"nickname": "小明"},
                                    "interactInfo": {"likedCount": "12"}}}]}
    items = _parse_feeds(data)
    assert items[0]["title"] == "标题" and "xsec_token=tok" in items[0]["url"]
    assert items[0]["meta"]["作者"] == "小明" and items[0]["meta"]["点赞"] == "12"
    assert _parse_feeds({"data": {"items": []}}) == []


def test_zhihu_signature_shape_and_determinism():
    from chigua.tools.zhihu_sign import parse_cookie, pathname_of, sign

    url = "https://www.zhihu.com/api/v4/search_v3?t=general&q=%E6%B5%8B%E8%AF%95&limit=5"
    assert pathname_of(url) == "/api/v4/search_v3?t=general&q=%E6%B5%8B%E8%AF%95&limit=5"
    assert pathname_of("/api/v4/x") == "/api/v4/x"
    s1 = sign(url, '"AbCd|1700000000"')
    s2 = sign(url, '"AbCd|1700000000"')
    assert s1 == s2 and s1.startswith("2.0_") and len(s1) == 4 + 64
    assert set(s1[4:]) <= set("6fpLRqJO8M/c3jnYxFkUVC4ZIG12SiH=5v0mXDazWBTsuw7QetbKdoPyAl+hN9rgE")
    assert sign(url, '"other|1"') != s1 and sign(url + "&offset=5", '"AbCd|1700000000"') != s1
    jar = parse_cookie('_zap=1; d_c0="AbCd|1700000000"; z_c0=2.0|xyz; _xsrf=tok')
    assert jar["d_c0"] == '"AbCd|1700000000"' and jar["_xsrf"] == "tok" and jar["z_c0"] == "2.0|xyz"
