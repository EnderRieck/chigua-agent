"""Grok（OpenAI 兼容协议）封装：支持中转站、流式输出、function calling、JSON 输出。"""
from __future__ import annotations

import json
import re
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
        """逐段产出 ("text", delta)，最后产出 ("done", {"text":..., "tool_calls":[...]}）。"""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if not settings.llm_stream:
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
