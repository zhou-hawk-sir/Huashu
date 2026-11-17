"""MCP Client

用于将来支持远端 MCP Server（HTTP 传输）。当前系统以本地进程内调用为主，
但保留 client 接口以便将来扩展。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

import httpx

from .protocol import JsonRpcRequest, JsonRpcResponse


class MCPClient:
    """MCP 客户端，支持 HTTP 传输。"""

    def __init__(self, endpoint: str, timeout: int = 30) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "MCPClient":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if self._client is None:
            raise RuntimeError("MCPClient must be used as async context manager")

        req = JsonRpcRequest(method=method, params=params or {})
        resp = await self._client.post(self.endpoint, json=req.to_dict())
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result")

    async def list_tools(self) -> Any:
        return await self.request("tools/list")

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        return await self.request("tools/call", {"name": name, "arguments": arguments})