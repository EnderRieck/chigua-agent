"""MCP Streamable HTTP 客户端的小封装，兼容 mcp SDK 1.x（streamablehttp_client）与 2.x（streamable_http_client）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator


@asynccontextmanager
async def open_mcp_session(url: str, token: str = "", timeout: float = 120.0) -> AsyncIterator["ClientSession"]:
    from mcp import ClientSession

    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        from mcp.client.streamable_http import streamable_http_client as _open  # mcp >= 2.0
        try:
            import httpx2 as hx  # mcp 2.x 自带的 httpx 分支
        except ImportError:  # pragma: no cover
            import httpx as hx
        http_client = hx.AsyncClient(headers=headers, timeout=hx.Timeout(timeout, read=timeout * 3)) if headers else None
        cm = _open(url, http_client=http_client) if http_client else _open(url)
    except ImportError:
        from mcp.client.streamable_http import streamablehttp_client as _open  # mcp 1.x
        cm = _open(url, headers=headers)

    async with cm as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def text_of(result) -> str:
    return "\n".join(c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text")
