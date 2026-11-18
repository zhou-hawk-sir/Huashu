"""记忆层两项增强的单元测试

1. 压缩前抢救：上下文压缩时记忆块降级为「编号 + 结论」精简版，
   pinned 属性与 name 保持不变，内容长度显著下降；
2. 记忆 ↔ 追踪双向链接：写入时 provenance 记下 trace_id / node_id，
   可按节点主键回放到产生该记忆的决策链。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_core.context_compressor import ContextCompressor
from api.memory_router import build_memory_router
from audit.audit_db import AuditDB
from audit.tracer import Tracer
from llm.base import Message
from memory.schema import connect
from memory.service import MemoryConfig, MemoryService
from memory.writer import WriteRequest


class _NoLLM:
    def chat(self, messages):
        raise RuntimeError("offline")


def _svc() -> MemoryService:
    return MemoryService(config=MemoryConfig(db_path=":memory:"), conn=connect(":memory:"))


def _seed(svc: MemoryService, title: str = "定位端口占用后重启 ollama") -> str:
    result = svc.write(
        WriteRequest(
            kind="episodic",
            title=title,
            symptom="ollama 起不来，11434 端口被占用",
            diagnosis="端口被残留进程占用",
            solution="ss -lntp 找占用进程后 kill -9，再 systemctl restart ollama 恢复服务",
        )
    )
    assert result is not None and result.memory_id
    return result.memory_id


# ── 一、压缩前抢救 ────────────────────────────────────────────────────
def test_compact_block_is_shorter_and_keeps_id():
    svc = _svc()
    mid = _seed(svc)
    full = svc.retrieve_for_prompt("ollama 11434 端口占用", session_id="s1")
    slim = svc.compact_block("s1")

    assert full and slim
    assert len(slim) < len(full)
    # 记忆编号前缀保留，模型与人都还能按编号引用
    assert mid[:8] in slim
    assert "定位端口占用后重启 ollama" in slim
    assert slim.startswith("【历史运维经验")
    assert slim.rstrip().endswith("【历史经验结束】")


def test_compact_block_empty_without_retrieval():
    svc = _svc()
    assert svc.compact_block("never-retrieved") == ""


def test_compress_swaps_memory_block_for_slim_version():
    svc = _svc()
    mid = _seed(svc)
    full = svc.retrieve_for_prompt("ollama 11434 端口占用", session_id="s1")
    slim = svc.compact_block("s1")

    messages = [Message(role="system", content="系统提示")]
    messages.append(Message(role="system", content=full, name="memory", pinned=True))
    for i in range(12):
        messages.append(Message(role="user", content=f"第 {i} 条提问"))
        messages.append(Message(role="assistant", content=f"第 {i} 条回答"))

    comp = ContextCompressor(_NoLLM(), retain_recent=4)
    out = comp.compress(messages, memory_rewriter=lambda: slim)

    mem = [m for m in out if m.name == "memory"]
    assert len(mem) == 1
    assert mem[0].pinned is True
    assert mem[0].role == "system"
    assert len(mem[0].content) < len(full)
    assert mid[:8] in mem[0].content


def test_compress_keeps_original_when_rewriter_empty():
    svc = _svc()
    full = svc.retrieve_for_prompt("ollama 11434 端口占用", session_id="s1")

    messages = [Message(role="system", content="系统提示")]
    messages.append(Message(role="system", content=full, name="memory", pinned=True))
    for i in range(12):
        messages.append(Message(role="user", content=f"第 {i} 条提问"))
        messages.append(Message(role="assistant", content=f"第 {i} 条回答"))

    comp = ContextCompressor(_NoLLM(), retain_recent=4)
    out = comp.compress(messages, memory_rewriter=lambda: "")

    mem = [m for m in out if m.name == "memory"]
    assert mem and mem[0].content == full


def test_compress_without_rewriter_is_unchanged():
    """不传 rewriter 时行为与改动前一致：pinned 消息原样保留。"""
    messages = [Message(role="system", content="系统提示")]
    messages.append(Message(role="system", content="记忆块", name="memory", pinned=True))
    for i in range(10):
        messages.append(Message(role="user", content=f"q{i}"))
        messages.append(Message(role="assistant", content=f"a{i}"))

    comp = ContextCompressor(_NoLLM(), retain_recent=4)
    out = comp.compress(messages)
    mem = [m for m in out if m.name == "memory"]
    assert mem and mem[0].content == "记忆块" and mem[0].pinned is True


# ── 二、记忆 ↔ 追踪双向链接 ──────────────────────────────────────────
def test_tracer_backfills_node_id_from_sink():
    tracer = Tracer("s1", sinks=[lambda step: 42])
    step = asyncio.run(tracer.trace("intent", "user", input={"x": 1}))
    assert step.node_id == 42
    assert tracer.last_node_id == 42


def test_audit_db_add_trace_returns_rowid(tmp_path):
    db = AuditDB(str(tmp_path / "audit.db"))
    tracer = Tracer("s1", sinks=[db.add_trace])
    first = asyncio.run(tracer.trace("intent", "user"))
    second = asyncio.run(tracer.trace("reasoning", "llm"))
    assert isinstance(first.node_id, int) and isinstance(second.node_id, int)
    assert second.node_id > first.node_id


def test_write_stores_trace_pointer():
    svc = _svc()
    result = svc.observe_result(
        "s1",
        "重启 ollama 服务",
        {"success": True, "tools": [{"name": "shell.execute", "args": {"cmd": "systemctl restart ollama"}, "success": True}], "answer": "已重启"},
        trace_id="trace-abc",
        trace_node_id=7,
    )
    assert result is not None
    rec = svc.store.get(result.memory_id)
    assert rec is not None
    assert rec.provenance.get("trace_id") == "trace-abc"
    assert rec.provenance.get("trace_node_id") == 7

    link = svc.trace_link(result.memory_id)
    assert link["ok"] and link["linked"] is True
    assert link["node_id"] == 7


def test_trace_link_unlinked_for_manual_memory():
    svc = _svc()
    mid = _seed(svc)
    link = svc.trace_link(mid)
    assert link["ok"] is True
    assert link["linked"] is False
    assert link["node_id"] is None


def test_trace_endpoint_returns_node_window(tmp_path):
    svc = _svc()
    db = AuditDB(str(tmp_path / "audit.db"))
    tracer = Tracer("s1", sinks=[db.add_trace])

    steps = []
    for kind, actor in (
        ("intent", "user"),
        ("reasoning", "llm"),
        ("tool_call", "llm"),
        ("exec", "executor"),
        ("result", "system"),
    ):
        steps.append(asyncio.run(tracer.trace(kind, actor, output={"k": kind})))

    node_id = steps[2].node_id
    assert node_id is not None

    result = svc.observe_result(
        "s1",
        "重启 ollama 服务",
        {"success": True, "tools": [{"name": "shell.execute", "args": {"cmd": "systemctl restart ollama"}, "success": True}], "answer": "ok"},
        trace_id=tracer.trace_id,
        trace_node_id=node_id,
    )
    assert result is not None

    app = FastAPI()
    app.include_router(build_memory_router(svc, db))
    client = TestClient(app)

    resp = client.get(f"/api/memory/{result.memory_id}/trace?window=1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["linked"] is True
    assert data["node"]["step"] == "tool_call"
    assert data["node"]["current"] is True
    assert 2 <= len(data["context"]) <= 3


def test_trace_endpoint_without_link_is_not_error():
    svc = _svc()
    mid = _seed(svc)
    app = FastAPI()
    app.include_router(build_memory_router(svc, None))
    client = TestClient(app)

    resp = client.get(f"/api/memory/{mid}/trace")
    assert resp.status_code == 200
    data = resp.json()
    assert data["linked"] is False
    assert data["context"] == []
