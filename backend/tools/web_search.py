"""web_search tool — DuckDuckGo HTML search with SSRF allowlist.

SSRF 防护：仅允许白名单域名（搜索引擎），拒绝回环/私网 IP，
即使通过重定向跳板也无法绕过（手动 build_url 校验，不跟随重定向到非白名单域名）。
"""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlparse, quote_plus

import httpx

from .base import RiskLevel, Tool, ToolResult


# 白名单：仅允许这些域名（用于查询/抓取）
ALLOWED_DOMAINS = {
    "www.bing.com",
    "cn.bing.com",
    "www.baidu.com",
    "duckduckgo.com",
    "html.duckduckgo.com",
    "www.google.com",
    "scholar.google.com",
}

DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/"

MAX_SNIPPET_CHARS = 4000
TIMEOUT_SECONDS = 10
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _is_private_or_loopback(host: str) -> bool:
    """检查 host 是否为回环地址 / 私网 IP（IP 字面量；域名解析不在此做）。"""
    if not host:
        return False
    # 去掉方括号（IPv6 字面量）
    bare = host.strip("[]")
    # 是 IP 字面量？
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


class WebSearchTool(Tool):
    """通过 DuckDuckGo HTML 接口做轻量网页搜索（无需 API key）。"""

    name = "web_search"
    description = (
        "使用 DuckDuckGo HTML 搜索公网信息（无需 API key）。"
        "输入 query 关键词，返回首个结果片段（截断至 4000 字符）。"
        "仅允许白名单域名；私网/回环目标一律拒绝。"
    )
    risk_level = RiskLevel.MEDIUM
    required_capability = "network"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "url": {
                "type": "string",
                "description": (
                    "可选：自定义目标 URL（必须是白名单域名）。"
                    "缺省时使用 DuckDuckGo HTML 搜索。"
                ),
            },
        },
        "required": ["query"],
    }

    def build_url(self, target_url: str) -> str:
        """校验并规范化 URL，拒绝白名单之外的域名 / 私网 / 回环。"""
        if not target_url:
            raise ValueError("URL must be non-empty")
        parsed = urlparse(target_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"URL scheme not allowed: {parsed.scheme!r}")
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError("URL has no host")
        # IP 字面量：直接拒绝（无论是否白名单"看起来"）
        if _is_private_or_loopback(host):
            raise ValueError(f"Loopback / private IP rejected: {host}")
        # 域名必须命中白名单
        if host not in ALLOWED_DOMAINS:
            raise ValueError(f"Domain not in allowlist: {host}")
        return target_url

    async def run(self, *args: Any, **kwargs: Any) -> ToolResult:
        # 兼容两种调用方式：
        #   1. 传统: t.run(query="x")        （基类抽象签名）
        #   2. dict:  t.run({"query": "x"})   （便于测试 & MCP 风格调用）
        if args and isinstance(args[0], dict) and not kwargs:
            kwargs = args[0]
        elif args:
            return ToolResult(
                success=False,
                error=f"Unexpected positional args: {args!r}",
            )

        query = kwargs.get("query")
        if not query or not isinstance(query, str):
            return ToolResult(
                success=False,
                error="Missing required field: query",
            )

        # 1. 决定目标 URL
        explicit_url = kwargs.get("url")
        if explicit_url:
            try:
                url = self.build_url(explicit_url)
            except ValueError as e:
                return ToolResult(success=False, error=str(e))
        else:
            url = f"{DEFAULT_SEARCH_URL}?q={quote_plus(query)}"

        # 2. 抓取（按白名单域名，不跟随重定向到非白名单）
        #    简化的 SSRF 防护：直接抓；不使用 follow_redirects=True 跳到非白名单
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS,
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                resp = await client.get(url)
        except httpx.RequestError as e:
            return ToolResult(success=False, error=f"Network error: {e}")
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, error=f"Fetch failed: {e}")

        # 处理 redirect：手动跳一次，仍校验白名单
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location") or resp.headers.get("Location")
            if not location:
                return ToolResult(
                    success=False,
                    error=f"Redirect with no Location header (status {resp.status_code})",
                )
            try:
                url = self.build_url(location)
            except ValueError as e:
                return ToolResult(success=False, error=str(e))
            try:
                async with httpx.AsyncClient(
                    timeout=TIMEOUT_SECONDS,
                    follow_redirects=False,
                    headers={"User-Agent": USER_AGENT},
                ) as client2:
                    resp = await client2.get(url)
            except Exception as e:  # noqa: BLE001
                return ToolResult(success=False, error=f"Fetch redirect failed: {e}")

        if resp.status_code >= 400:
            return ToolResult(
                success=False,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )

        # 3. 解析：去 HTML 标签，合并空白
        text = _HTML_TAG_RE.sub(" ", resp.text)
        text = _WS_RE.sub(" ", text).strip()

        # 4. 截断
        truncated = len(text) > MAX_SNIPPET_CHARS
        snippet = text[:MAX_SNIPPET_CHARS]

        return ToolResult(
            success=True,
            output={
                "url": url,
                "snippet": snippet,
                "length": len(snippet),
                "truncated": truncated,
            },
        )