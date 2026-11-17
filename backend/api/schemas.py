"""Pydantic 请求/响应模型"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    user_id: Optional[str] = ""
    metadata: Optional[Dict[str, Any]] = None


class SessionResponse(BaseModel):
    session_id: str
    auth_token: Optional[str] = None  # C1: WS 连接需带此 token
    updated_at: Optional[str] = None  # Concern #2: .recent 高亮用（ISO 字符串）


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1)


class MessageItem(BaseModel):
    role: str
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    ts: Optional[str] = None


class SendMessageResponse(BaseModel):
    content: str
    tool_calls: List[Dict[str, Any]] = []


# WebSocket 流式事件 — 与前端 handleStreamEvent type switch 对齐
class ToolCallEvent(BaseModel):
    """WS 事件 type='tool_call' data 结构。

    前端 app.js appendToolCall({name, arguments: args}) 期望这两个字段。
    """
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(BaseModel):
    """WS 事件 type='tool_result' data 结构。

    前端 appendToolResult({name, success, duration_ms, decision, output, error})
    """
    name: str
    success: bool = True
    duration_ms: int = 0
    decision: Optional[str] = None
    output: Any = None
    error: Optional[str] = None


class ConfirmRequestEvent(BaseModel):
    """WS 事件 type='confirm_request' data 结构。

    前端 showConfirmDialog({request_id, tool, arguments: args, rationale})
    """
    request_id: str
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    rationale: Optional[str] = None


class ConfirmAckEvent(BaseModel):
    """WS 事件 type='confirm_ack' data 结构。"""
    request_id: str
    ok: bool = True


class ToolInfo(BaseModel):
    """工具元数据（前端展示用）。

    用户已精简：不再有 risk_level 字段（无 confirm 弹窗）。
    """
    name: str
    description: str
    required_capability: str = ""
    input_schema: Dict[str, Any] = Field(default_factory=dict)


class ToolListResponse(BaseModel):
    tools: List[ToolInfo]


class TraceItem(BaseModel):
    trace_id: str
    step: str
    actor: str
    ts: str
    input: Dict[str, Any] = {}
    output: Dict[str, Any] = {}
    decision_rationale: Optional[str] = None
    user_confirmed: Optional[bool] = None


class TracesResponse(BaseModel):
    session_id: str
    traces: List[TraceItem]


class WSMessage(BaseModel):
    type: str
    data: Dict[str, Any] = {}


class ConfirmRequest(BaseModel):
    request_id: str
    approved: bool
    reason: Optional[str] = ""


# ============================================================
# 受信任工作目录（Trusted Workspace）
# ============================================================

class SetWorkspaceRequest(BaseModel):
    path: str = Field(..., min_length=1, description="工作目录绝对路径")


class WorkspaceResponse(BaseModel):
    session_id: str
    raw: Optional[str] = None
    root: Optional[str] = None
    previous_root: Optional[str] = None
    set_at: str
    ok: bool
    reason: Optional[str] = None