"""思维链追踪器"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class TraceStepKind(str, Enum):
    INTENT = "intent"
    PERCEPTION = "perception"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    RISK_EVAL = "risk_eval"
    KB_VALIDATE = "kb_validate"   # 命令知识库校验步骤
    EXEC = "exec"
    CONFIRM = "confirm"
    CONFIRM_REQUEST = "confirm_request"   # 发起人审请求（executor）
    CONFIRM_RESULT = "confirm_result"     # 用户确认/拒绝结果
    DENY = "deny"
    RESULT = "result"
    ERROR = "error"
    # ── 记忆层 5 类节点（V5.4）─────────────────────────────────────
    # 让「记忆是否影响了决策」可回放：检索 → 命中 → 阻断 → 写入 → 过期
    MEMORY_RETRIEVE = "memory.retrieve"
    MEMORY_HIT = "memory.hit"
    MEMORY_BLOCKED = "memory.blocked"
    MEMORY_WRITE = "memory.write"
    MEMORY_EXPIRE = "memory.expire"


class TraceActor(str, Enum):
    USER = "user"
    LLM = "llm"
    SAFETY = "safety"
    EXECUTOR = "executor"
    SYSTEM = "system"


@dataclass
class TraceStep:
    trace_id: str
    session_id: str
    step: TraceStepKind
    actor: TraceActor
    ts: str
    input: Dict[str, Any] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    decision_rationale: Optional[str] = None
    user_confirmed: Optional[bool] = None
    # 落库后的自增主键。记忆的 provenance 存这个值，就能反向定位到
    # 产生它的那条决策节点；未落库（只走 JSONL）时为 None。
    node_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["step"] = self.step.value
        d["actor"] = self.actor.value
        return d

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class Tracer:
    """追踪器：产生 TraceStep 并交给 logger/db 落盘。"""

    def __init__(self, session_id: str, sinks: Optional[list] = None) -> None:
        self.session_id = session_id
        self._trace_id = str(uuid.uuid4())
        self.sinks = sinks or []
        # 最近一条成功落库的节点主键，供调用方回填到记忆 provenance
        self.last_node_id: Optional[int] = None

    def add_sink(self, sink: Any) -> None:
        self.sinks.append(sink)

    async def trace(
        self,
        step: str | TraceStepKind,
        actor: str | TraceActor,
        input: Optional[Dict[str, Any]] = None,
        output: Optional[Dict[str, Any]] = None,
        decision_rationale: Optional[str] = None,
        user_confirmed: Optional[bool] = None,
    ) -> TraceStep:
        """记录一条 trace step。"""
        if isinstance(step, str):
            step = TraceStepKind(step)
        if isinstance(actor, str):
            actor = TraceActor(actor)

        # H2: 脱敏 input / output（password/token/api_key 等不落盘）
        from utils.redactor import redact
        redacted_input = redact(input) if input else {}
        redacted_output = redact(output) if output else {}

        ts_step = TraceStep(
            trace_id=self._trace_id,
            session_id=self.session_id,
            step=step,
            actor=actor,
            ts=datetime.utcnow().isoformat() + "Z",
            input=redacted_input,
            output=redacted_output,
            decision_rationale=decision_rationale,
            user_confirmed=user_confirmed,
        )

        node_id: Optional[int] = None
        for sink in self.sinks:
            try:
                if hasattr(sink, "write"):
                    maybe = sink.write(ts_step)
                    if hasattr(maybe, "__await__"):
                        maybe = await maybe
                elif callable(sink):
                    maybe = sink(ts_step)
                else:
                    maybe = None
            except Exception:
                maybe = None
            # 落库 sink 返回自增主键；JSONL sink 返回 None
            if isinstance(maybe, int) and not isinstance(maybe, bool):
                node_id = maybe

        if node_id is not None:
            ts_step.node_id = node_id
            self.last_node_id = node_id

        return ts_step

    @property
    def trace_id(self) -> str:
        return self._trace_id