"""LLM 客户端抽象接口

仿照 Claude 的 tool_use 设计：
- chat: 同步调用，返回完整响应
- stream_chat: 流式调用，便于实时推送思维链
- LLMResponse 统一格式：content / tool_calls / usage
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional


@dataclass
class Message:
    """对话消息，支持 tool 角色（OpenAI 风格）。"""
    role: str  # system | user | assistant | tool
    content: str = ""
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List["ToolCall"]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role}
        if self.content:
            d["content"] = self.content
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        return d


@dataclass
class ToolCall:
    """工具调用。"""
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self._json_dump(self.arguments),
            },
        }

    @staticmethod
    def _json_dump(obj: Any) -> str:
        import json
        return json.dumps(obj, ensure_ascii=False)


@dataclass
class LLMResponse:
    """LLM 响应。"""
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Dict[str, int] = field(default_factory=dict)
    raw: Any = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


@dataclass
class LLMChunk:
    """流式响应中的一个 chunk。"""
    delta_content: str = ""
    delta_tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None


class LLMClient(abc.ABC):
    """LLM 客户端抽象基类。"""

    def __init__(self, model: str = "", **kwargs: Any) -> None:
        self.model = model
        self.kwargs = kwargs

    @abc.abstractmethod
    def chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """同步对话。"""

    @abc.abstractmethod
    async def stream_chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMChunk]:
        """流式对话。"""
        if False:
            yield  # 让 async generator 语法合法

    def to_provider_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """转换为各家 provider 的 messages 格式，默认 OpenAI 兼容。"""
        return [m.to_dict() for m in messages]

    def to_provider_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """转换为 provider 的 tools 格式。"""
        return tools