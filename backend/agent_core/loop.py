"""Agent 主循环（增强版：ReAct + 多Agent编排 + 诊断 + 压缩）

增强功能：
- 多 Agent 编排（TaskClassifier 决定走哪条路径）
- 根因诊断（当检测到故障排查时自动触发）
- 上下文语义压缩（token 接近上限时自动压缩）
- 并行工具调度（低风险工具可并行执行）
- 受信任工作目录（Trusted Workspace，Claude Code 风格 cwd 信任）

核心流程：
1. 注入检测
2. Context 加载 + 语义压缩
3. LLM 决策
4. 若有 tool_calls：调度执行 → 结果回填 → 回到 3
5. 若无：返回最终文本
"""
from __future__ import annotations

import asyncio
import contextvars
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

from audit.tracer import Tracer
from audit.chain_logger import ChainLogger
from audit.audit_db import AuditDB
from llm.base import LLMClient, LLMResponse, Message
from safety.policy_engine import PolicyEngine
from safety.confirmation import ConfirmationManager
from safety.workspace import (
    WorkspaceTrust,
    WorkspaceValidationError,
    WorkspaceResolveError,
    ValidateResult,
)
from safety.workspace_registry import WorkspaceRegistry
from utils.logger import get_logger

from .context import ContextManager
from .dispatcher import ToolDispatcher
from .prompt_builder import PromptBuilder
from .context_compressor import ContextCompressor

logger = get_logger("agent_core.loop")


# C8: 防 multi_agent 重入 stream_run 的闸门。
# 用 ContextVar 而非实例布尔：ContextVar 随 asyncio 任务上下文传播，
# 因此同一任务链内的递归调用能被拦截，而并发的不同会话各自独立、互不影响。
_in_orchestrator_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "opsagent_in_orchestrator", default=False
)


def _now_iso() -> str:
    """UTC ISO-8601 with trailing 'Z', matching audit_db format. Always."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class StreamEvent:
    """主循环产出事件（用于流式推送给前端）。"""
    type: str
    data: Dict[str, Any]


def _atomic_record_tool_results(
    ctx,
    tool_calls,
    results,
    tracer,
    audit_db,
    dispatcher_result_to_text,
):
    """原子地回填一轮 assistant tool_calls 的结果。

    一次性把所有 tool 消息 push 进 ctx，然后做 audit + emit；
    任何 audit/emit 抛异常 → 全部回滚（tool 消息 + 对应 assistant 消息），
    避免下次 LLM 调用看到半孤立的 tool 消息（DeepSeek 会 400 拒绝）。

    Returns:
        (ok, texts): ok=True 全部成功；ok=False 全部回滚（不写任何 tool）
        texts 仅在 ok=True 时有值。
    """
    # 1) 找到本轮 assistant 消息（最后一条 role==assistant 且带 tool_calls）
    assistant_idx = None
    for i in range(len(ctx.messages) - 1, -1, -1):
        m = ctx.messages[i]
        if m.role == "assistant" and m.tool_calls:
            assistant_idx = i
            break
        if m.role != "assistant":
            break  # 越过本轮 assistant

    # 2) 计算本轮 tool_calls 的 id 集合
    tc_ids = {tc.id for tc in tool_calls}

    # 3) 一次性 push tool 消息
    pre_len = len(ctx.messages)
    texts = []
    for tc, result in zip(tool_calls, results):
        text = dispatcher_result_to_text(result)
        texts.append(text)
        ctx.add_tool_result(tc.id, tc.name, text)

    # 4) 尝试做 audit + 写 audit_db（任何失败都回滚）
    try:
        for tc, result, text in zip(tool_calls, results, texts):
            audit_db.add_message(
                ctx.session_id, "tool", text, tool_call_id=tc.id
            )
            audit_db.add_tool_invocation(
                session_id=ctx.session_id,
                trace_id=tracer.trace_id,
                tool_name=tc.name,
                args=tc.arguments,
                risk_level=result.decision,
                decision=result.decision,
                result={
                    "success": result.success,
                    "output": result.output,
                    "error": result.error,
                },
                duration_ms=result.duration_ms,
            )
    except Exception:
        # 完整回滚：先把 ctx 截到 helper 入口前的状态（移除本轮 push 的 tool），
        # 然后把 assistant 的 tool_calls 清空（避免 LLM 看到空 tool_calls 但找不到响应）。
        ctx.messages = ctx.messages[:pre_len]
        if assistant_idx is not None and assistant_idx < len(ctx.messages):
            from dataclasses import replace
            ctx.messages[assistant_idx] = replace(
                ctx.messages[assistant_idx], tool_calls=[]
            )
        return False, []

    return True, texts


class AgentLoop:
    """Agent 主循环（增强版）。"""

    def __init__(
        self,
        llm: LLMClient,
        context_manager: Optional[ContextManager] = None,
        prompt_builder: Optional[PromptBuilder] = None,
        dispatcher: Optional[ToolDispatcher] = None,
        policy: Optional[PolicyEngine] = None,
        confirmation_manager: Optional[ConfirmationManager] = None,
        audit_db: Optional[AuditDB] = None,
        chain_logger: Optional[ChainLogger] = None,
        cmd_validator: Any = None,
        max_iterations: int = 30,
        enable_multi_agent: bool = True,
        enable_diagnosis: bool = True,
        enable_compression: bool = True,
        workspace_trust: Optional[WorkspaceTrust] = None,
        memory: Any = None,
    ) -> None:
        self.llm = llm
        self.context = context_manager or ContextManager()
        self.prompt = prompt_builder or PromptBuilder()
        self.confirmation = confirmation_manager or ConfirmationManager()
        self.policy = policy or PolicyEngine(llm_client=llm)
        self.audit_db = audit_db or AuditDB()
        self.chain_logger = chain_logger or ChainLogger()
        self.cmd_validator = cmd_validator
        self.max_iterations = max_iterations
        self.enable_multi_agent = enable_multi_agent
        self.enable_diagnosis = enable_diagnosis
        self.enable_compression = enable_compression
        # 记忆层门面（可选）。只提供参考与边界断言，不参与 allow/deny 取值。
        self.memory = memory

        # 上下文压缩器
        self._compressor: Optional[ContextCompressor] = None

        # 工作目录信任
        self.workspace_trust = workspace_trust or WorkspaceTrust()
        self.workspace_registry = WorkspaceRegistry()

        # Stage 1 Task 1.5：启动时一次性收集本机环境（注入所有 system 消息）
        # 用 try/except 包好，避免探测阶段出错影响 AgentLoop 本身可用
        self._local_context: Dict[str, Any] = {}
        try:
            from tools.shell_discovery import collect_local_context
            self._local_context = collect_local_context()
            logger.info(
                f"local_context collected: arch={self._local_context.get('arch')} "
                f"distro={self._local_context.get('distro')} "
                f"kernel={self._local_context.get('kernel')}"
            )
        except Exception as e:
            logger.warning(f"collect_local_context 失败，将以空上下文启动：{e}")
            self._local_context = {}

        from execution.executor import Executor
        self.executor = Executor(
            policy=self.policy,
            confirmation_manager=self.confirmation,
            tracer=None,
            memory=self.memory,
        )
        self.dispatcher = dispatcher or ToolDispatcher(self.executor)

        # 多 Agent 编排器（延迟初始化）
        self._orchestrator = None

        # C1: session → auth_token 映射（O(1) 校验；不持久化，重启后回退 DB）
        self._session_tokens: Dict[str, str] = {}

        # 当前会话正在跑的 worker task 映射（用于「停止思考」按钮 — WS cancel 消息触发 cancel）
        self._active_tasks: Dict[str, asyncio.Task] = {}

    @property
    def compressor(self) -> ContextCompressor:
        if self._compressor is None:
            self._compressor = ContextCompressor(llm=self.llm)
        return self._compressor

    @property
    def orchestrator(self):
        """延迟初始化的多 Agent 编排器"""
        if self._orchestrator is None:
            from .multi_agent import MultiAgentOrchestrator
            self._orchestrator = MultiAgentOrchestrator(
                llm=self.llm,
                agent_loop_ref=self,
                max_concurrency=4,
            )
        return self._orchestrator

    def create_session(self, metadata: Optional[Dict[str, Any]] = None) -> str:
        return self._create_session_inner(metadata, with_token=False)["session_id"]

    def create_session_with_token(
        self, metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, str]:
        """C1: 创建会话并返回 {session_id, auth_token}。

        token 用于 WS 连接和 confirm_response 鉴权。
        """
        return self._create_session_inner(metadata, with_token=True)

    def _create_session_inner(
        self, metadata: Optional[Dict[str, Any]], with_token: bool
    ) -> Dict[str, Any]:
        session_id = str(uuid.uuid4())
        # 若 metadata 含 workspace 路径，先做校验/规范化
        if metadata and "workspace" in metadata and isinstance(metadata["workspace"], str):
            ws_raw = metadata["workspace"]
            try:
                res = self.set_workspace(session_id, ws_raw)
                if res["ok"]:
                    metadata = dict(metadata)
                    metadata["workspace"] = {
                        "raw": ws_raw,
                        "root": res["root"],
                        "set_at": res["set_at"],
                    }
                else:
                    # 校验失败：丢弃非法的 workspace 字段，但保留其他 metadata
                    metadata = {k: v for k, v in metadata.items() if k != "workspace"}
                    logger.warning(
                        f"session {session_id} workspace 校验失败，已丢弃：{res.get('reason')}"
                    )
            except (WorkspaceValidationError, WorkspaceResolveError) as e:
                metadata = {k: v for k, v in metadata.items() if k != "workspace"}
                logger.warning(f"session {session_id} workspace 异常：{e}")

        if with_token:
            import secrets
            token = secrets.token_urlsafe(32)
            metadata = dict(metadata or {})
            metadata["auth_token"] = token
            metadata["auth_token_set_at"] = _now_iso()
            self._session_tokens[session_id] = token

        self.context.create_session(session_id, metadata)
        self.audit_db.upsert_session(session_id, metadata=metadata)
        logger.info(f"Created session: {session_id}")

        # 会话数量限制：自动清理超过30条的旧会话
        self._cleanup_old_sessions(max_sessions=30)

        if with_token:
            return {"session_id": session_id, "auth_token": token}
        return {"session_id": session_id}

    def _cleanup_old_sessions(self, max_sessions: int = 30) -> None:
        """清理超过最大数量限制的旧会话。

        按 updated_at 降序保留最近 max_sessions 条，删除多余的旧会话。
        """
        try:
            all_sessions = self.audit_db.list_sessions()
            if len(all_sessions) <= max_sessions:
                return

            # 找出需要删除的旧会话（跳过前 max_sessions 条）
            sessions_to_delete = all_sessions[max_sessions:]
            deleted_count = 0

            for session in sessions_to_delete:
                session_id = session.get("id")
                if session_id:
                    try:
                        self.delete_session(session_id)
                        deleted_count += 1
                    except Exception as e:
                        logger.warning(f"清理旧会话 {session_id} 失败: {e}")

            if deleted_count > 0:
                logger.info(f"已自动清理 {deleted_count} 条旧会话，保留最近 {max_sessions} 条")

        except Exception as e:
            logger.warning(f"清理旧会话时出错: {e}")

    def validate_session_token(self, session_id: str, token: str) -> bool:
        """C1: 校验 session 绑定的 token。

        内存映射优先；重启后内存为空时，从审计库回补
        （create_session_with_token 已把 auth_token 写进 session metadata）。
        """
        if not token:
            return False
        expected = self._session_tokens.get(session_id)
        if expected is None:
            expected = self._load_token_from_db(session_id)
            if expected:
                self._session_tokens[session_id] = expected
        if expected is None:
            return False
        # 恒定时序比较防 timing attack
        import hmac
        return hmac.compare_digest(expected, token)

    def _load_token_from_db(self, session_id: str) -> Optional[str]:
        """重启后从审计库读回会话的 auth_token（无则 None）。"""
        try:
            row = self.audit_db.get_session(session_id)
        except Exception as e:
            logger.warning(f"load token from db failed for {session_id}: {e}")
            return None
        if not row:
            return None
        meta = row.get("metadata")
        if isinstance(meta, str):
            import json
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                return None
        if isinstance(meta, dict):
            tok = meta.get("auth_token")
            if isinstance(tok, str) and tok:
                return tok
        return None

    # -------------------------------------------------------------
    # 工作目录（workspace）信任
    # -------------------------------------------------------------
    def set_workspace(self, session_id: str, raw_path: str) -> Dict[str, Any]:
        """设置/更新会话的工作目录（Claude Code cwd 信任风格）。

        返回 dict 含 ok/root/reason 等字段，调用方应原样作为 ``workspace_event`` payload。
        """
        try:
            res: ValidateResult = self.workspace_trust.validate(raw_path)
        except (WorkspaceValidationError, WorkspaceResolveError) as e:
            return {
                "ok": False,
                "raw": raw_path,
                "root": None,
                "previous_root": self.workspace_registry.get(session_id),
                "set_at": _now_iso(),
                "reason": str(e),
            }

        previous = self.workspace_registry.get(session_id)
        self.workspace_registry.set(session_id, res.root)

        ctx = self.context.get(session_id)
        if ctx is None:
            ctx = self._hydrate_ctx_from_db(session_id)
        ctx.metadata["workspace"] = {
            "raw": raw_path,
            "root": res.root,
            "set_at": _now_iso(),
        }
        # 重建 system message 以告知 LLM 新 workspace
        if ctx.messages and ctx.messages[0].role == "system":
            ctx.messages[0] = self.prompt.build_system_message(
                ctx_metadata=ctx.metadata,
                local_context=self._local_context,
            )

        logger.info(f"session {session_id} workspace set to {res.root}")
        return {
            "ok": True,
            "raw": raw_path,
            "root": res.root,
            "previous_root": previous,
            "set_at": ctx.metadata["workspace"]["set_at"],
            "reason": None,
        }

    def get_workspace(self, session_id: str) -> Optional[Dict[str, Any]]:
        """查询会话当前工作目录（含 raw/root/set_at 完整结构）。

        返回 dict {raw, root, set_at}，缺失时 None。
        优先从 ctx.metadata 取（来源是 set_workspace 写入的完整结构），
        仅在 metadata 无 workspace 时退回 registry（仅 root）。
        会话不在内存时尝试从 audit DB 回补（重启后仍可查）。
        """
        ctx = self.context.get(session_id)
        if ctx is None:
            ctx = self._hydrate_ctx_from_db(session_id)
        if ctx is not None:
            ws = ctx.metadata.get("workspace")
            if isinstance(ws, dict) and ws.get("root"):
                return {
                    "raw": ws.get("raw"),
                    "root": ws.get("root"),
                    "set_at": ws.get("set_at"),
                }
        root = self.workspace_registry.get(session_id)
        if root is None:
            return None
        return {"raw": None, "root": root, "set_at": None}

    def _hydrate_ctx_from_db(self, session_id: str):
        """重启后会话不在内存时，从 audit DB 回补 metadata + token + 最近 30 轮消息。

        返回 Context（如存在）；否则 None。
        """
        try:
            row = self.audit_db.get_session(session_id)
        except Exception as e:
            logger.warning(f"hydrate ctx from db failed for {session_id}: {e}")
            return None
        if not row:
            return None
        meta = row.get("metadata")
        if isinstance(meta, str):
            import json
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        # 创建内存 ctx，metadata 来自 DB（包含 workspace / auth_token 等）
        ctx = self.context.create_session(session_id, metadata=meta)
        # token 也同步到内存映射
        tok = meta.get("auth_token")
        if isinstance(tok, str) and tok:
            self._session_tokens[session_id] = tok
        # workspace 也回填到 registry（供 get_workspace 兜底路径）
        ws = meta.get("workspace")
        if isinstance(ws, dict) and ws.get("root"):
            self.workspace_registry.set(session_id, ws["root"])
        # 回填最近 30 轮消息（从 audit_db.messages 表）
        # 一轮 = 1 user + 1 assistant(+ tool results)，30 轮约 60-120 条
        try:
            db_msgs = self.audit_db.list_messages(session_id, limit=60)
            if db_msgs:
                from utils.message import Message
                rehydrated = []
                for m in db_msgs:
                    tc_raw = m.get("tool_calls")
                    tool_calls = []
                    if tc_raw:
                        try:
                            tc_data = json.loads(tc_raw) if isinstance(tc_raw, str) else tc_raw
                            from llm.base import ToolCall
                            for tc in tc_data:
                                tool_calls.append(ToolCall(
                                    id=tc.get("id", ""),
                                    name=tc.get("name", ""),
                                    arguments=tc.get("arguments", {}),
                                ))
                        except Exception:
                            pass
                    rehydrated.append(Message(
                        role=m.get("role", "user"),
                        content=m.get("content", "") or "",
                        name=m.get("name"),
                        tool_call_id=m.get("tool_call_id"),
                        tool_calls=tool_calls,
                    ))
                # 插入到 system message 之后
                if rehydrated:
                    sys_idx = 0
                    if ctx.messages and ctx.messages[0].role == "system":
                        sys_idx = 1
                    ctx.messages[sys_idx:sys_idx] = rehydrated
                logger.info(
                    f"hydrated {len(rehydrated)} messages from DB for session {session_id}"
                )
        except Exception as e:
            logger.warning(f"hydrate messages from db failed for {session_id}: {e}")
        return ctx

    async def handle_confirmation(
        self, session_id: str, req_id: str, approved: bool, reason: str = ""
    ) -> bool:
        # C2: 把 session_id 传给 manager 做跨 session 校验
        return await self.confirmation.resolve(
            req_id, approved, reason, session_id=session_id
        )

    async def handle_set_workspace(self, session_id: str, raw_path: str) -> StreamEvent:
        """处理 WS `set_workspace` 消息。

        与同步 set_workspace 区别：返回 StreamEvent，便于调用方转发给前端。
        """
        payload = self.set_workspace(session_id, raw_path)
        return StreamEvent("workspace_event", payload)

    async def run(
        self,
        session_id: str,
        user_input: str,
        stream_callback: Optional[Callable[[StreamEvent], Awaitable[None]]] = None,
    ) -> str:
        """同步运行一轮，返回最终文本。"""
        async for event in self.stream_run(session_id, user_input, stream_callback):
            if event.type == "final":
                return event.data.get("content", "")
            if event.type == "error":
                return f"[error] {event.data.get('message', '')}"
        return ""

    async def stream_run(
        self,
        session_id: str,
        user_input: str,
        stream_callback: Optional[Callable[[StreamEvent], Awaitable[None]]] = None,
        *,
        bypass_reentrancy: bool = False,
        silent_phase: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        """流式运行：通过 Queue 在内部 worker 与生成器之间传事件。

        bypass_reentrancy: True 表示本次调用是 multi_agent 内部合法的 nested 调用，
        跳过 ContextVar 防重入闸门。默认 False（用户路径必须防）。
        silent_phase: True 表示嵌套调用不再发 phase=intent_understanding（外层已发），
        避免前端 PhaseBar 反复切。
        """
        # C8: 防 multi_agent 重入 stream_run（ContextVar：同任务链拦截，跨会话独立）
        if not bypass_reentrancy and _in_orchestrator_ctx.get():
            logger.error(
                f"stream_run re-entrancy blocked: session={session_id}, "
                "orchestrator is calling stream_run recursively"
            )
            yield StreamEvent("error", {
                "message": "multi_agent recursion blocked",
                "stage": "reentrancy_guard",
            })
            return

        queue: asyncio.Queue = asyncio.Queue()
        done_sentinel = object()

        # 先发 phase=thinking（前端 PhaseBar 立即有反馈），后续阶段由编排器覆盖
        # silent_phase=True 时跳过（嵌套调用，由外层负责 phase）
        if not silent_phase:
            await queue.put(StreamEvent("phase", {"phase": "intent_understanding"}))

        async def send(event):
            # 归一化：多 Agent 编排器（multi_agent.py）通过 stream_callback 发的是
            # 普通 dict，而 run()/REST/WS 回调都按 StreamEvent.type 取值。
            # 在入队边界统一转成 StreamEvent，避免 'dict' object has no attribute 'type'。
            if isinstance(event, dict):
                event = StreamEvent(event.get("type", "unknown"), event.get("data", {}) or {})
            # final / error 时把 phase 清空（避免 L2/L3 卡在前端不消失）
            if event.type in ("final", "error", "user_cancelled"):
                queue.put_nowait(StreamEvent("phase", {"phase": None}))
            await queue.put(event)

        async def worker():
            # C8: 进入 stream_run worker 阶段，标记防重入（ContextVar，随本任务传播）
            _tok = _in_orchestrator_ctx.set(True)
            try:
                # 1) 注入检测（使用增强后的 InjectionDetector）
                verdict = self.policy.evaluate_input(user_input)
                if verdict.decision.value == "deny":
                    await send(StreamEvent("error", {
                        "message": verdict.reason, "stage": "input_filter"
                    }))
                    return

                # 2) Context
                ctx = self.context.get_or_create(session_id)
                if not ctx.messages:
                    ctx.messages.append(
                        self.prompt.build_system_message(
                            ctx_metadata=ctx.metadata,
                            local_context=self._local_context,
                        )
                    )

                # 2.1) 记忆检索（在输入检测之后，确保提示注入先被拦掉）
                # 检索失败一律降级为空块：记忆不是安全组件，不能因为它坏了就不干活。
                mem_result = None
                mem_block = ""
                tool_log: list = []
                if self.memory is not None:
                    try:
                        mem_result = self.memory.retrieve(user_input, session_id=session_id)
                        mem_block = self.memory.render_block(mem_result, session_id)
                    except Exception as e:
                        logger.warning(f"memory retrieve failed, degraded: {e}")
                        mem_block = ""

                # 命令知识库增强
                raw_user_msg = Message(role="user", content=user_input)
                user_msg_for_ctx = self.prompt.augment_user_message_with_kb(raw_user_msg)
                ctx.add_user(user_msg_for_ctx.content)
                self.audit_db.add_message(session_id, "user", user_input)

                # 记忆块以独立 system 消息注入，并标注 pinned：
                # 拼进用户消息会被模型当成用户要求；不 pinned 会在第二轮被窗口丢掉。
                # 位置放在首条 system 之后：部分 OpenAI 兼容接口要求 system 消息靠前。
                if mem_block:
                    ctx.messages = [
                        m
                        for m in ctx.messages
                        if not (m.role == "system" and m.name == "memory")
                    ]
                    insert_at = 1 if ctx.messages and ctx.messages[0].role == "system" else 0
                    ctx.messages.insert(
                        insert_at,
                        Message(
                            role="system",
                            content=mem_block,
                            name="memory",
                            pinned=True,
                        ),
                    )

                self.context.slide_window(ctx)

                # 2.5) 语义压缩（接近 token 上限时）
                if self.enable_compression and self.compressor.should_compress(ctx.messages):
                    ctx.messages = await asyncio.to_thread(self.compressor.compress, ctx.messages)
                    await send(StreamEvent("context_event", {
                        "type": "compressed",
                        "message": "上下文已自动压缩"
                    }))

                # 3) Tracer
                tracer = Tracer(
                    session_id,
                    sinks=[self.chain_logger, self.audit_db.add_trace],
                )
                self.executor.tracer = tracer
                await tracer.trace("intent", "user", input={"content": user_input})

                # 3.1) 记忆链路追踪：检索 → 命中
                if self.memory is not None:
                    self.memory.bind_tracer(session_id, tracer)
                    if mem_result is not None:
                        await self.memory.emit_retrieve(tracer, mem_result)

                # 4) 多 Agent 编排决策
                if self.enable_multi_agent:
                    plan = self.orchestrator.task_classifier.classify(user_input)
                    await tracer.trace("reasoning", "llm", output={
                        "task_type": plan.task_type.value,
                        "reasoning": plan.reasoning,
                    })

                    # 如果是诊断或多域任务，走多 Agent 编排器
                    if plan.task_type.value in ("fault_diagnosis", "multi_domain"):
                        await send(StreamEvent("agent_decision", {
                            "task_type": plan.task_type.value,
                            "reasoning": plan.reasoning,
                        }))
                        try:
                            multi_result = await self.orchestrator.run(
                                task=user_input,
                                session_id=session_id,
                                stream_callback=send,
                                context_hints={"session_id": session_id},
                            )
                            ctx.add_assistant(multi_result)
                            await self._persist_memory(
                                tracer, session_id, user_input, tool_log, multi_result
                            )
                            await send(StreamEvent("final", {"content": multi_result}))
                            return
                        except Exception as e:
                            logger.warning(f"Multi-agent failed, falling back to ReAct: {e}")

                # 5) 标准 ReAct 主循环
                for iteration in range(self.max_iterations):
                    tools_schema = self.prompt.build_tools_schema()
                    try:
                        # 同步 OpenAI 客户端会阻塞事件循环 → 丢到线程池，
                        # 避免慢 LLM 调用卡住健康检查与其他会话的 WebSocket。
                        resp: LLMResponse = await asyncio.to_thread(
                            self.llm.chat,
                            messages=ctx.messages,
                            tools=tools_schema,
                        )
                    except Exception as e:
                        logger.exception("LLM call failed")
                        await send(StreamEvent("error", {"message": f"LLM error: {e}"}))
                        return

                    await tracer.trace(
                        "reasoning", "llm",
                        input={"iteration": iteration},
                        output={
                            "content": resp.content[:200] if resp.content else "",
                            "tool_calls": [tc.to_dict() for tc in resp.tool_calls],
                        },
                    )

                    if not resp.has_tool_calls:
                        ctx.add_assistant(resp.content)
                        self.audit_db.add_message(session_id, "assistant", resp.content)
                        # 结果回写：只有真的执行过工具的成功处置才值得沉淀
                        await self._persist_memory(
                            tracer, session_id, user_input, tool_log, resp.content
                        )
                        await send(StreamEvent("token", {"content": resp.content}))
                        await send(StreamEvent("final", {"content": resp.content}))
                        return

                    # 工具调用
                    ctx.add_assistant(resp.content, tool_calls=resp.tool_calls)
                    self.audit_db.add_message(
                        session_id, "assistant", resp.content,
                        tool_calls=[tc.to_dict() for tc in resp.tool_calls],
                    )
                    await send(StreamEvent("assistant_text", {"content": resp.content}))

                    for tc in resp.tool_calls:
                        await send(StreamEvent("tool_call", {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        }))
                        # 记录 tool_call 类型的 trace
                        await tracer.trace(
                            "tool_call", "executor",
                            input={"tool": tc.name, "arguments": tc.arguments},
                        )

                    async def confirm_cb(req_id: str, payload: Dict[str, Any]):
                        await send(StreamEvent("confirm_request", payload))

                    results = await self.dispatcher.dispatch(
                        resp.tool_calls,
                        confirm_callback=confirm_cb,
                        workspace_root=self.workspace_registry.get(session_id),
                        session_id=session_id,
                    )

                    # 原子回填：先 push 所有 tool 消息，audit 失败则整轮回滚
                    # （防 DeepSeek 400：tool 消息找不到对应 tool_calls）
                    ok, texts = _atomic_record_tool_results(
                        ctx=ctx,
                        tool_calls=resp.tool_calls,
                        results=results,
                        tracer=tracer,
                        audit_db=self.audit_db,
                        dispatcher_result_to_text=self.dispatcher.result_to_text,
                    )
                    if not ok:
                        logger.warning(
                            "本轮 tool 结果回填失败，已回滚 ctx 防孤立 tool 消息"
                        )
                        await send(StreamEvent("error", {
                            "message": "本轮工具结果审计失败，已重置上下文该轮",
                            "stage": "atomic_record_tool_results",
                        }))
                        continue  # 进入下一轮 LLM 决策（但本轮没改 ctx）

                    for tc, result, text in zip(resp.tool_calls, results, texts):
                        tool_log.append(
                            {
                                "name": tc.name,
                                "args": tc.arguments,
                                "success": bool(result.success),
                                "decision": result.decision,
                            }
                        )
                        await send(StreamEvent("tool_result", {
                            "name": tc.name,
                            "success": result.success,
                            "duration_ms": result.duration_ms,
                            "decision": result.decision,
                            "output": result.output if result.success else None,
                            "error": result.error if not result.success else None,
                        }))

                    # 压缩检查（每轮结束后）
                    if self.enable_compression and self.compressor.should_compress(ctx.messages):
                        ctx.messages = await asyncio.to_thread(self.compressor.compress, ctx.messages)
                        await send(StreamEvent("context_event", {
                            "type": "compressed",
                            "message": "上下文已自动压缩"
                        }))

                await send(StreamEvent("error", {
                    "message": "max iterations reached", "stage": "loop"
                }))
            except Exception as e:
                logger.exception("AgentLoop worker error")
                await send(StreamEvent("error", {"message": f"worker error: {e}"}))
            finally:
                # C8: 离开 worker 时清掉重入闸门
                _in_orchestrator_ctx.reset(_tok)
                await queue.put(done_sentinel)

        # 启动 worker 并 yield 事件
        task = asyncio.create_task(worker())
        # 注册到 active tasks（支持 WS cancel 终止）
        self._active_tasks[session_id] = task
        task.add_done_callback(lambda t, sid=session_id: self._active_tasks.pop(sid, None))

        async def callback_wrapper(event: StreamEvent):
            if stream_callback:
                await stream_callback(event)

        try:
            while True:
                # 检查是否被外部 cancel
                if task.done() and task.cancelled():
                    yield StreamEvent("error", {
                        "message": "用户已停止生成",
                        "stage": "user_cancelled",
                    })
                    return
                event = await queue.get()
                if event is done_sentinel:
                    break
                await callback_wrapper(event)
                yield event
        finally:
            # 如果还在跑，cancel 兜底（用户拔 WS 走这里）
            if not task.done():
                task.cancel()
            self._active_tasks.pop(session_id, None)

        await task

    async def _persist_memory(
        self,
        tracer: Any,
        session_id: str,
        user_input: str,
        tool_log: list,
        answer: str,
    ) -> None:
        """把本轮处置结果沉淀为经验。

        这是整个链路里唯一允许 fail-open 的位置：记忆写入不是安全判定，
        写失败只记日志，绝不改变本轮结果。
        """
        if self.memory is None:
            return
        try:
            result = self.memory.observe_result(
                session_id,
                user_input,
                {"success": True, "tools": tool_log, "answer": answer},
            )
            if result is not None:
                await self.memory.emit_write(tracer, result)
        except Exception as e:
            logger.warning(f"memory write failed: {e}")

    # -------------------------------------------------------------
    # 取消正在跑的 stream_run（前端"停止"按钮 → WS cancel）
    # -------------------------------------------------------------
    def cancel_session_run(self, session_id: str) -> bool:
        """取消指定会话当前正在跑的 worker task。返回 True 表示有任务被取消。"""
        task = self._active_tasks.get(session_id)
        if task is None or task.done():
            return False
        try:
            task.cancel()
            return True
        except Exception:
            return False

    # -------------------------------------------------------------
    # 删除会话（前端"删除"按钮 → DELETE /api/sessions/{id}）
    # -------------------------------------------------------------
    def delete_session(self, session_id: str) -> bool:
        """从审计库 + 内存清理指定会话。返回 True 表示删除了。"""
        # 先取消可能在跑的任务
        self.cancel_session_run(session_id)
        # 内存清理
        self._session_tokens.pop(session_id, None)
        self._active_tasks.pop(session_id, None)
        if hasattr(self.context, "remove"):
            self.context.remove(session_id)
        # 落库清理
        try:
            return self.audit_db.delete_session(session_id)
        except Exception as e:
            logger.warning(f"delete_session db error for {session_id}: {e}")
            return False

    # -------------------------------------------------------------
    # AI 标题生成（异步后台；首轮 user message 后调用）
    # -------------------------------------------------------------
    async def summarize_session_title(self, session_id: str, first_user_msg: str) -> str:
        """调用 LLM 把 first_user_msg 压缩成 ≤16 字标题；失败回退前 16 字。

        结果写入 session.metadata.title，并异步落盘到 audit_db。
        """
        title = ""
        try:
            from llm.base import Message
            prompt = (
                "请把用户下面的运维诉求压缩成 ≤16 字的中文短语标题，"
                "直接返回标题文本本身（不加引号、不加前缀、不要标点结尾）。\n\n"
                f"用户输入：{first_user_msg[:500]}"
            )
            # 用同样的 LLM（Mock/DeepSeek 都行），避免新增依赖
            resp = await asyncio.to_thread(
                self.llm.chat, [Message(role="user", content=prompt)]
            )
            title = (resp.content or "").strip().splitlines()[0].strip().strip('"\'`')[:32]
        except Exception as e:
            logger.warning(f"summarize_session_title LLM failed: {e}")
            title = ""

        # 兜底：取用户输入的前 16 字
        if not title:
            cleaned = "".join(c for c in first_user_msg if c not in "\n\r\t")
            title = cleaned[:16] + ("…" if len(cleaned) > 16 else "")

        # 写回内存 + DB
        ctx = self.context.get(session_id) or self._hydrate_ctx_from_db(session_id)
        if ctx is not None:
            ctx.metadata["title"] = title
            ctx.metadata["title_set_at"] = _now_iso()
        try:
            row = self.audit_db.get_session(session_id) or {}
            meta = row.get("metadata")
            import json as _json
            if isinstance(meta, str):
                try: meta = _json.loads(meta)
                except Exception: meta = {}
            if not isinstance(meta, dict): meta = {}
            meta["title"] = title
            meta["title_set_at"] = _now_iso()
            self.audit_db.upsert_session(session_id, metadata=meta)
        except Exception as e:
            logger.warning(f"summarize_session_title db write failed: {e}")
        return title

    # -------------------------------------------------------------
    # AI 标题生成（异步后台；首轮 user message 后调用）— END
    # -------------------------------------------------------------
