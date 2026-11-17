"""MCP Server

支持两种传输：
- stdio: 通过 stdin/stdout 处理 JSON-RPC（默认）
- http: 通过 FastAPI 暴露 /mcp 端点（复用 api/app.py 的路由）

消息格式：JSON-RPC 2.0
支持方法：
- initialize
- tools/list
- tools/call
- ping
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Dict, Optional

from utils.logger import get_logger
from .protocol import (
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    MCPErrorCode,
)
from .registry import ToolRegistry, get_default_registry

logger = get_logger("mcp.server")


class MCPServer:
    """MCP 服务器，处理 JSON-RPC 请求并路由到工具。"""

    def __init__(self, registry: Optional[ToolRegistry] = None) -> None:
        self.registry = registry or get_default_registry()

    async def handle_request(self, raw: str) -> str:
        """处理单个 JSON-RPC 请求，返回响应 JSON 字符串。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            err = JsonRpcResponse(
                id=None,
                error=JsonRpcError(MCPErrorCode.PARSE_ERROR, f"Parse error: {e}"),
            )
            return json.dumps(err.to_dict(), ensure_ascii=False)

        try:
            req = JsonRpcRequest.from_dict(data)
        except (KeyError, TypeError) as e:
            err = JsonRpcResponse(
                id=data.get("id"),
                error=JsonRpcError(MCPErrorCode.INVALID_REQUEST, f"Invalid request: {e}"),
            )
            return json.dumps(err.to_dict(), ensure_ascii=False)

        try:
            result = await self._dispatch(req)
            resp = JsonRpcResponse(id=req.id, result=result)
        except JsonRpcError as e:
            resp = JsonRpcResponse(id=req.id, error=e)
        except Exception as e:
            logger.exception("MCP dispatch error")
            resp = JsonRpcResponse(
                id=req.id,
                error=JsonRpcError(MCPErrorCode.INTERNAL_ERROR, f"Internal error: {e}"),
            )

        return json.dumps(resp.to_dict(), ensure_ascii=False)

    async def _dispatch(self, req: JsonRpcRequest) -> Any:
        method = req.method
        params = req.params or {}

        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "opsagent-mcp", "version": "1.0.0"},
                "capabilities": {"tools": {"listChanged": False}},
            }

        if method == "ping":
            return {"pong": True}

        if method == "tools/list":
            return self.registry.to_tools_list_result().to_dict()

        if method == "tools/call":
            from .protocol import ToolCallParams
            call_params = ToolCallParams.from_dict(params)
            result = await self.registry.call(call_params.name, call_params.arguments)
            return result.to_dict()

        raise JsonRpcError(MCPErrorCode.METHOD_NOT_FOUND, f"Unknown method: {method}")

    async def run_stdio(self) -> None:
        """以 stdio 模式运行：逐行读取 JSON 请求并写回响应。"""
        logger.info("MCP server starting in stdio mode")
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        writer_transport, writer_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, loop)

        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.decode("utf-8").strip()
            if not line:
                continue
            response = await self.handle_request(line)
            writer.write((response + "\n").encode("utf-8"))
            await writer.drain()