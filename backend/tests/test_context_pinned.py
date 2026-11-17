"""上下文窗口与压缩对 pinned 消息的保护

记忆注入块以独立 system 消息附加，必须打上 pinned，
否则滑动窗口在第二轮就把它截掉，表现为「记忆时有时无」。
"""
from __future__ import annotations

from agent_core.context import ContextManager, SessionContext
from agent_core.context_compressor import ContextCompressor
from llm.base import Message


def build_ctx(n: int, memory_block: str = "[历史参考] 11434 为 ollama 端口") -> SessionContext:
    ctx = SessionContext(session_id="s-1")
    ctx.messages.append(Message(role="system", content="你是运维助手"))
    ctx.messages.append(Message(role="system", content=memory_block, pinned=True))
    for i in range(n):
        ctx.messages.append(Message(role="user", content=f"第 {i} 条提问"))
        ctx.messages.append(Message(role="assistant", content=f"第 {i} 条回答"))
    return ctx


def test_pinned_survives_slide_window():
    cm = ContextManager(max_messages=10)
    ctx = build_ctx(20)
    cm.slide_window(ctx)

    assert len(ctx.messages) <= 10
    assert any(m.pinned and "11434" in m.content for m in ctx.messages)
    assert ctx.messages[0].content == "你是运维助手"


def test_unpinned_second_system_is_dropped():
    """未打 pinned 的第二条 system 会被裁掉，证明保护是靠标记而非靠 role。"""
    cm = ContextManager(max_messages=10)
    ctx = SessionContext(session_id="s-2")
    ctx.messages.append(Message(role="system", content="主 system"))
    ctx.messages.append(Message(role="system", content="第二条 system"))
    for i in range(20):
        ctx.messages.append(Message(role="user", content=f"q{i}"))
    cm.slide_window(ctx)

    contents = [m.content for m in ctx.messages]
    assert "主 system" in contents
    assert "第二条 system" not in contents


def test_no_truncation_when_under_limit():
    cm = ContextManager(max_messages=50)
    ctx = build_ctx(3)
    before = list(ctx.messages)
    cm.slide_window(ctx)
    assert ctx.messages == before


def test_window_keeps_latest_messages():
    cm = ContextManager(max_messages=6)
    ctx = build_ctx(20)
    cm.slide_window(ctx)
    assert ctx.messages[-1].content == "第 19 条回答"


class _NoLLM:
    """摘要环节不依赖真实模型：调用失败时压缩器降级为占位摘要，
    这里要验证的是 pinned 消息是否被保留，与摘要内容无关。"""

    def chat(self, messages):
        raise RuntimeError("offline")


def test_compressor_keeps_pinned():
    comp = ContextCompressor(_NoLLM())
    ctx = build_ctx(30)
    out = comp.compress(ctx.messages)
    assert any(m.pinned for m in out)
    assert any("11434" in m.content for m in out if m.pinned)


def test_pinned_default_is_false():
    assert Message(role="user", content="x").pinned is False
