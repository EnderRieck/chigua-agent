"""读取 .env / 环境变量，集中管理配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _normalize_base_url(url: str) -> str:
    """中转站地址常被填成完整的 /chat/completions 路径；SDK 会自己拼接，这里去掉多余后缀。"""
    url = url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    # LLM（Grok，OpenAI 兼容协议，可为中转站）
    xai_api_key: str = field(default_factory=lambda: _env("XAI_API_KEY"))
    xai_base_url: str = field(default_factory=lambda: _normalize_base_url(_env("XAI_BASE_URL", "https://api.x.ai/v1")))
    xai_model: str = field(default_factory=lambda: _env("XAI_MODEL", "grok-4.3"))
    llm_stream: bool = field(default_factory=lambda: _env("LLM_STREAM", "1") != "0")
    x_search_mode: str = field(default_factory=lambda: _env("X_SEARCH_MODE", "auto").lower())
    x_search_timeout: int = field(default_factory=lambda: _env_int("X_SEARCH_TIMEOUT", 120))

    # DeepSeek 搜索
    deepseek_api_key: str = field(default_factory=lambda: _env("DEEPSEEK_API_KEY"))
    deepseek_base_url: str = field(default_factory=lambda: _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))

    # 社媒
    weibo_cookie: str = field(default_factory=lambda: _env("WEIBO_COOKIE"))
    zhihu_cookie: str = field(default_factory=lambda: _env("ZHIHU_COOKIE"))
    xhs_mcp_url: str = field(default_factory=lambda: _env("XHS_MCP_URL"))
    xhs_mcp_token: str = field(default_factory=lambda: _env("XHS_MCP_TOKEN"))

    # 运行参数
    max_tool_rounds: int = field(default_factory=lambda: _env_int("MAX_TOOL_ROUNDS", 4))
    request_timeout: int = field(default_factory=lambda: _env_int("REQUEST_TIMEOUT", 40))
    reports_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "reports")

    @staticmethod
    def _real_key(key: str) -> bool:
        return bool(key) and not key.lower().startswith(("sk-xxx", "your", "<"))

    @property
    def has_llm(self) -> bool:
        return self._real_key(self.xai_api_key)

    @property
    def has_deepseek(self) -> bool:
        return self._real_key(self.deepseek_api_key)


settings = Settings()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
