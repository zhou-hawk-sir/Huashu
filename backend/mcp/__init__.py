"""MCP (Model Context Protocol) 协议层

参考 https://modelcontextprotocol.io/ 实现核心子集：
- JSON-RPC 2.0 消息
- tools/list, tools/call 方法
- stdio 与 http 传输
"""
from .protocol import (
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcError,
    MCPErrorCode,
    ToolDefinition,
    ToolsListResult,
    ToolCallParams,
    ToolCallResult,
)
from .registry import ToolRegistry, get_default_registry
from .manifest import build_manifest_from_tools, load_tools_from_yaml

__all__ = [
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonRpcError",
    "MCPErrorCode",
    "ToolDefinition",
    "ToolsListResult",
    "ToolCallParams",
    "ToolCallResult",
    "ToolRegistry",
    "get_default_registry",
    "build_manifest_from_tools",
    "load_tools_from_yaml",
]