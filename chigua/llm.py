"""Grok（OpenAI 兼容协议）封装：支持中转站、流式输出、function calling、JSON 输出。"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

from openai import OpenAI

from .config import settings


def extract_json(text: str) -> Any:
    """从模型输出里尽量抠出 JSON（兼容 ```json 包裹、前后废话）。"""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("模型输出不是合法 JSON: " + text[:200])


class LLM:
    def __init__(self, model: str | None = None) -> None:
        if not settings.has_llm:
            raise RuntimeError("未配置 XAI_API_KEY，请先在 .env 中填写")
        self.model = model or settings.xai_model
        self.client = OpenAI(api_key=settings.xai_api_key, base_url=settings.xai_base_url,
                             timeout=settings.request_timeout * 3, max_retries=2)
        self._stream_ok = True   # 流式连接一旦被中转站掐断，本会话后续走非流式

    # ---------- 普通对话 ----------
    def complete(self, messages: list[dict[str, Any]], temperature: float = 0.3, **kw: Any) -> str:
        resp = self.client.chat.completions.create(model=self.model, messages=messages,
                                                   temperature=temperature, **kw)
        return resp.choices[0].message.content or ""

    def complete_json(self, messages: list[dict[str, Any]], temperature: float = 0.1) -> Any:
        try:
            text = self.complete(messages, temperature=temperature, response_format={"type": "json_object"})
        except Exception:  # noqa: BLE001 — 中转站可能不支持 response_format
            text = self.complete(messages, temperature=temperature)
        return extract_json(text)

    # ---------- 流式 + 工具调用 ----------
    def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
               temperature: float = 0.3) -> Iterator[tuple[str, Any]]:
        """逐段产出 ("text", delta)，最后产出 ("done", {"text":..., "tool_calls":[...]}）。

        连接类错误（中转站断流、超时）自动重试：前两次仍用流式，第三次改用非流式一次性拿结果。
        已经吐出去的文本片段无法撤回，重试前会先产出 ("retry", 说明) 让界面清屏。
        """
        from openai import APIConnectionError, APITimeoutError, InternalServerError

        attempts = 3
        for attempt in range(1, attempts + 1):
            # 流式连接在本会话里断过一次之后就不再尝试流式：中转站对长时间无输出的流式连接会掐断，
            # 非流式一次性返回反而稳定（实测 9~13s）
            use_stream = settings.llm_stream and self._stream_ok and attempt < attempts
            try:
                yield from self._stream_once(messages, tools, temperature, use_stream)
                return
            except (APIConnectionError, APITimeoutError, InternalServerError) as exc:
                if use_stream and self._stream_ok:
                    self._stream_ok = False
                    yield ("retry", f"流式连接被中断（{type(exc).__name__}），本会话后续改用非流式输出")
                elif attempt == attempts:
                    raise
                else:
                    wait = 3 * attempt
                    yield ("retry", f"模型连接中断（{type(exc).__name__}），{wait}s 后第 {attempt + 1} 次重试")
                    time.sleep(wait)

    def _stream_once(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                     temperature: float, use_stream: bool) -> Iterator[tuple[str, Any]]:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if not use_stream:
            resp = self.client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            text = msg.content or ""
            if text:
                yield ("text", text)
            calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or ""}
                     for tc in (msg.tool_calls or [])]
            yield ("done", {"text": text, "tool_calls": calls})
            return

        stream = self.client.chat.completions.create(stream=True, **kwargs)
        parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                parts.append(delta.content)
                yield ("text", delta.content)
            for tc in delta.tool_calls or []:
                idx = tc.index if tc.index is not None else len(calls)
                slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name and not slot["name"].startswith(tc.function.name):
                        slot["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
        ordered = [calls[i] for i in sorted(calls)]
        for i, c in enumerate(ordered):
            c["id"] = c["id"] or f"call_{i}"
        yield ("done", {"text": "".join(parts), "tool_calls": ordered})

    def ping(self) -> str:
        return self.complete([{"role": "user", "content": "回复“ok”两个字母即可。"}], temperature=0)[:50]
