"""上下文管理"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from llm.base import Message, ToolCall


@dataclass
class SessionContext:
    """单个会话的上下文。"""
    session_id: str
    messages: List[Message] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_user(self, content: str) -> None:
        self.messages.append(Message(role="user", content=content))

    def add_assistant(self, content: str, tool_calls: Optional[List[ToolCall]] = None) -> None:
        self.messages.append(
            Message(role="assistant", content=content, tool_calls=tool_calls or [])
        )

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        self.messages.append(
            Message(
                role="tool",
                content=content,
                name=name,
                tool_call_id=tool_call_id,
            )
        )


class ContextManager:
    """上下文管理器：滑动窗口 / Token 计数。"""

    def __init__(self, max_messages: int = 40, max_tokens: int = 8000) -> None:
        self.max_messages = max_messages
        self.max_tokens = max_tokens
        self._sessions: Dict[str, SessionContext] = {}

    def create_session(self, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> SessionContext:
        ctx = SessionContext(session_id=session_id, metadata=metadata or {})
        self._sessions[session_id] = ctx
        return ctx

    def get(self, session_id: str) -> Optional[SessionContext]:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str) -> SessionContext:
        return self._sessions.setdefault(
            session_id, SessionContext(session_id=session_id)
        )

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def slide_window(self, ctx: SessionContext) -> None:
        """滑动窗口：保留首条 system + 所有 pinned + 后 N 条。

        pinned 消息（如记忆注入块）必须始终保留：它承担「历史参考」的作用，
        一旦被窗口截掉，模型在后续轮次就看不到它，表现为记忆随机失效。
        """
        if len(ctx.messages) <= self.max_messages:
            return

        head: List[Message] = []
        if ctx.messages and ctx.messages[0].role == "system":
            head.append(ctx.messages[0])

        kept_ids = {id(m) for m in head}
        pinned = [m for m in ctx.messages if m.pinned and id(m) not in kept_ids]
        kept_ids |= {id(m) for m in pinned}

        need = max(0, self.max_messages - len(head) - len(pinned))
        tail: List[Message] = []
        for m in reversed(ctx.messages):
            if id(m) in kept_ids:
                continue
            tail.append(m)
            if len(tail) >= need:
                break
        tail.reverse()

        ctx.messages = head + pinned + tail

    def estimate_tokens(self, ctx: SessionContext) -> int:
        """粗略估算 token 数（按字符 / 1.5）。"""
        total = 0
        for m in ctx.messages:
            total += len(m.content) // 2
            if m.tool_calls:
                for tc in m.tool_calls:
                    total += len(tc.name) * 2
                    for v in tc.arguments.values():
                        total += len(str(v)) // 2
        return total