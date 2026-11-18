"""MCP 协议数据类型定义（JSON-RPC 2.0 子集）"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional


class MCPErrorCode(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # MCP 自定义
    TOOL_NOT_FOUND = -32001
    TOOL_EXECUTION_ERROR = -32002
    PERMISSION_DENIED = -32003
    RISK_DENIED = -32004


@dataclass
class JsonRpcRequest:
    """JSON-RPC 2.0 请求。"""
    method: str
    params: Optional[Dict[str, Any]] = None
    id: Optional[Any] = 1
    jsonrpc: str = "2.0"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"jsonrpc": self.jsonrpc, "method": self.method, "id": self.id}
        if self.params is not None:
            d["params"] = self.params
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JsonRpcRequest":
        return cls(
            method=data["method"],
            params=data.get("params"),
            id=data.get("id"),
        )


@dataclass
class JsonRpcError(Exception):
    code: int
    message: str
    data: Any = None

    def to_dict(self) -> Dict[str, Any]:
        d = {"code": self.code, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d


@dataclass
class JsonRpcResponse:
    """JSON-RPC 2.0 响应。"""
    id: Any = 1
    result: Any = None
    error: Optional[JsonRpcError] = None
    jsonrpc: str = "2.0"

    def to_dict(self) -> Dict[str, Any]:
        d = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            d["error"] = self.error.to_dict()
        else:
            d["result"] = self.result
        return d


@dataclass
class ToolDefinition:
    """工具定义（JSON Schema）。"""
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    required_capability: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "riskLevel": self.risk_level,
            "requiredCapability": self.required_capability,
        }


@dataclass
class ToolsListResult:
    tools: List[ToolDefinition] = field(default_factory=list)
    next_cursor: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {"tools": [t.to_dict() for t in self.tools]}
        if self.next_cursor:
            d["nextCursor"] = self.next_cursor
        return d


@dataclass
class ToolCallParams:
    """tools/call 参数。"""
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolCallParams":
        return cls(
            name=data["name"],
            arguments=data.get("arguments", {}) or {},
        )


@dataclass
class ToolCallResult:
    """工具调用结果（MCP 格式）。"""
    content: List[Dict[str, Any]] = field(default_factory=list)
    is_error: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"content": self.content, "isError": self.is_error}