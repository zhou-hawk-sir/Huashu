"""REST 路由"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from mcp.registry import get_default_registry
from .schemas import (
    CreateSessionRequest,
    MessageItem,
    SendMessageRequest,
    SendMessageResponse,
    SessionResponse,
    SetWorkspaceRequest,
    ToolInfo,
    ToolListResponse,
    TraceItem,
    TracesResponse,
    WorkspaceResponse,
)
from audit.replay import replay_session
from utils.logger import get_logger

logger = get_logger("api.routes")


def build_router(loop) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health() -> Dict[str, Any]:
        return {"status": "ok"}

    @router.get("/system/local-context")
    async def get_local_context() -> Dict[str, Any]:
        """返回本机环境快照（arch / distro / kernel / cpu / memory / executables / history）。

        前端顶栏 / 任何"我现在跑在什么机器上"的展示用。
        收集失败时返回 ok=False + 空 dict（不抛 500，便于前端展示"未知"）。
        """
        from tools.shell_discovery import collect_local_context
        try:
            ctx = collect_local_context()
            return {"ok": True, "context": ctx}
        except Exception as e:
            return {"ok": False, "context": {}, "error": str(e)}

    @router.get("/system/metrics")
    async def get_system_metrics() -> Dict[str, Any]:
        """实时监控大屏数据 —— 后端直接 run_cmd 采集，不经过 LLM（不依赖 API Key）。

        字段对齐 MonitorView.vue 解析器：hostname / uptime / load{1,5,15} /
        memTotalGb / memUsedGb / memPercent / diskPercent / diskUsed / diskTotal /
        listenCount / estCount。
        任意子项失败不抛 500，对应字段返回 None / 0，前端展示"未知"或"—"。
        """
        import asyncio
        import re as _re
        import platform as _platform

        from tools._runner import run_cmd

        is_linux = _platform.system() == "Linux"

        async def _safe(cmd: str, timeout: float = 3.0) -> str:
            try:
                r = await run_cmd(["bash", "-c", cmd], timeout=timeout)
                return r.stdout if r.returncode == 0 else ""
            except Exception:
                return ""

        # 并行跑所有采集命令
        hostname_s, uptime_s, free_s, df_s, ss_s, uname_s = await asyncio.gather(
            _safe("hostname 2>/dev/null | head -1"),
            _safe("uptime 2>/dev/null"),
            _safe("free -g 2>/dev/null") if is_linux else _safe("systeminfo 2>/dev/null"),
            _safe("df -h / 2>/dev/null") if is_linux else _safe("wmic logicaldisk get caption,size,freespace /value 2>nul"),
            _safe("{ ss -tlnp 2>/dev/null; ss -tnp 2>/dev/null; } | head -200"),
            _safe("uname -a 2>/dev/null"),
        )

        # ---- 解析 hostname / uptime ----
        hostname = hostname_s.strip().splitlines()[0] if hostname_s else ""
        uname = uname_s.strip()
        uptime_str = uptime_s.strip()
        load1 = load5 = load15 = None
        m = _re.search(r"load average:\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", uptime_str)
        if m:
            load1, load5, load15 = float(m[1]), float(m[2]), float(m[3])

        # ---- 解析 free -g（Linux） / systeminfo（Windows） ----
        def _parse_gb_simple(s: str) -> float:
            """接受 '15G' / '512M' / '1024' 这种紧凑格式"""
            s = s.strip()
            mm = _re.match(r"([\d.]+)\s*([KMGT])?", s, _re.IGNORECASE)
            if not mm: return 0.0
            val = float(mm[1]); unit = (mm[2] or "").upper()
            return val * {"K": 1/1024/1024, "M": 1/1024, "G": 1, "T": 1024}.get(unit, 1)

        mem_total_gb = mem_used_gb = mem_percent = None
        for line in free_s.splitlines():
            # Ubuntu 中文 locale 输出 "内存：" 而非 "Mem:"，兼容两种
            if line.startswith("Mem:") or line.startswith("内存："):
                parts = line.split()
                if len(parts) >= 3:
                    mem_total_gb = _parse_gb_simple(parts[1])
                    mem_used_gb = _parse_gb_simple(parts[2])
                    if mem_total_gb:
                        mem_percent = round((mem_used_gb / mem_total_gb) * 100, 1)
                break
        # Windows systeminfo 兜底
        if mem_total_gb is None and free_s:
            tm = _re.search(r"Total Physical Memory:\s*([\d,]+)\s*MB", free_s)
            am = _re.search(r"Available Physical Memory:\s*([\d,]+)\s*MB", free_s)
            if tm and am:
                mem_total_gb = float(tm[1].replace(",", "")) / 1024
                mem_used_gb = (float(tm[1].replace(",", "")) - float(am[1].replace(",", ""))) / 1024
                mem_percent = round((mem_used_gb / mem_total_gb) * 100, 1)

        # ---- 解析 df -h /（Linux） / wmic（Windows） ----
        disk_percent = disk_used = disk_total = None
        for line in df_s.splitlines():
            if line.startswith("/dev/") or line.startswith("Caption"):
                parts = line.split()
                if len(parts) >= 5 and parts[4].rstrip("%").isdigit():
                    disk_percent = int(parts[4].rstrip("%"))
                    disk_used = parts[2]; disk_total = parts[1]
                    break
        # Windows wmic 兜底
        if disk_percent is None and df_s:
            tm = _re.search(r"FreeSpace=(\d+)", df_s)
            sz = _re.search(r"Size=(\d+)", df_s)
            if tm and sz:
                free = int(tm[1]); total = int(sz[1])
                disk_total = f"{total // (1024**3)}G"
                disk_used = f"{(total - free) // (1024**3)}G"
                disk_percent = round((total - free) / total * 100) if total else None

        # ---- 解析网络监听 / 已建连 ----
        listen_count = 0; est_count = 0
        for line in ss_s.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("State") or stripped.startswith("Netid"):
                continue
            parts = stripped.split()
            if not parts:
                continue
            # ss -tlnp 输出：第一列直接是 State（LISTEN / ESTAB）
            # ss -tnp  输出：第一列是 Netid（tcp/udp），第二列是 State
            state = parts[0].upper()
            if state == "LISTEN":
                listen_count += 1
            elif state.startswith("ESTAB"):
                est_count += 1
            elif len(parts) >= 2:
                state2 = parts[1].upper()
                if state2 == "LISTEN":
                    listen_count += 1
                elif state2.startswith("ESTAB"):
                    est_count += 1
        # 兜底：netstat -an 风格
        if listen_count == 0 and est_count == 0 and ss_s:
            for ln in ss_s.splitlines():
                L = ln.upper()
                if "LISTENING" in L or " LISTEN " in L:
                    listen_count += 1
                elif "ESTABLISHED" in L:
                    est_count += 1

        return {
            "ok": True,
            "ts": int(asyncio.get_event_loop().time()),
            "hostname": hostname,
            "uname": uname,
            "uptime": uptime_str,
            "load1": load1, "load5": load5, "load15": load15,
            "memTotalGb": mem_total_gb, "memUsedGb": mem_used_gb, "memPercent": mem_percent,
            "diskPercent": disk_percent, "diskUsed": disk_used, "diskTotal": disk_total,
            "listenCount": listen_count, "estCount": est_count,
        }

    @router.post("/sessions", response_model=SessionResponse)
    async def create_session(req: CreateSessionRequest) -> SessionResponse:
        # C1: 创建会话同时生成 auth_token，WS 必须带此 token
        result = loop.create_session_with_token(
            metadata=req.metadata or {"user_id": req.user_id}
        )
        # Concern #2: updated_at 从 DB 读回，保证与 GET /sessions 列表格式一致（可比较）
        summary = loop.audit_db.get_session(result["session_id"]) or {}
        return SessionResponse(
            session_id=result["session_id"],
            auth_token=result["auth_token"],
            updated_at=summary.get("updated_at"),
        )

    @router.get("/sessions")
    async def list_sessions() -> Dict[str, Any]:
        """列出所有会话摘要（含 updated_at / title，供前端 .recent 高亮 & 标题展示）。

        updated_at 取 session 记录与最新消息 ts 的较大值（见 AuditDB.list_sessions）。
        title 取自 metadata.title（首轮 user message 后由 LLM 异步写入）。

        会话数量限制：只保留最近30条会话记录，超出部分自动清理。
        """
        # 获取所有会话
        all_sessions = loop.audit_db.list_sessions()

        # 如果会话数量超过30条，只保留最近30条
        if len(all_sessions) > 30:
            # list_sessions() 已按 updated_at 降序返回，取前30条
            all_sessions = all_sessions[:30]
            logger.info(f"会话数量超过30条，已自动截断为最近30条")

        sessions: List[Dict[str, Any]] = []
        for row in all_sessions:
            meta = row.get("metadata") or {}
            if isinstance(meta, str):
                import json
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            ws = meta.get("workspace") if isinstance(meta, dict) else None
            ws_root = ws.get("root") if isinstance(ws, dict) else None
            title = meta.get("title") if isinstance(meta, dict) else None
            sessions.append({
                "session_id": row["id"],
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at") or row.get("created_at"),
                "workspace": ws_root,
                "title": title,
            })
        return {"sessions": sessions}

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> Dict[str, Any]:
        from utils.i18n import t
        ctx = loop.context.get(session_id)
        if ctx is None:
            # 后端重启后会话不在内存：从 audit DB 回填最近 30 轮消息
            loop._hydrate_ctx_from_db(session_id)
            ctx = loop.context.get(session_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail=t("session.not_found", id=session_id))
        return {
            "session_id": session_id,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "name": m.name,
                    "tool_call_id": m.tool_call_id,
                    "tool_calls": [tc.to_dict() for tc in (m.tool_calls or [])],
                }
                for m in ctx.messages
            ],
        }

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> Dict[str, Any]:
        """删除会话：清理内存 + 审计库 + 关联消息/trace/工具调用。"""
        ok = loop.delete_session(session_id)
        if not ok:
            # 不存在也算成功（幂等）
            return {"ok": True, "session_id": session_id, "existed": False}
        return {"ok": True, "session_id": session_id, "existed": True}

    @router.post("/sessions/batch-delete")
    async def batch_delete_sessions(body: Dict[str, Any]) -> Dict[str, Any]:
        """批量删除会话：接受 {"session_ids": [...]} 列表，逐个清理。"""
        session_ids = body.get("session_ids") or []
        if not isinstance(session_ids, list) or not session_ids:
            return {"ok": False, "error": "需要提供 session_ids 列表"}
        if len(session_ids) > 200:
            return {"ok": False, "error": "单次最多删除 200 个会话"}
        results = []
        deleted_count = 0
        for sid in session_ids:
            if not isinstance(sid, str):
                results.append({"session_id": str(sid), "deleted": False, "error": "invalid id"})
                continue
            existed = loop.delete_session(sid)
            results.append({"session_id": sid, "deleted": existed})
            if existed:
                deleted_count += 1
        return {
            "ok": True,
            "deleted": deleted_count,
            "total": len(session_ids),
            "results": results,
        }

    @router.post("/sessions/{session_id}/confirm")
    async def confirm_rest(session_id: str, request: Request):
        """REST 端点：发送 confirm_response（替代 WS confirm 通道，WS 断开时的兜底）。"""
        if not loop.context.get(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        body = await request.json()
        request_id = body.get("request_id", "")
        approved = body.get("approved", True)
        reason = body.get("reason", "")
        if not request_id:
            raise HTTPException(status_code=422, detail="missing request_id")
        ok = await loop.handle_confirmation(session_id, request_id, approved, reason)
        logger.info(
            f"[confirm] REST confirm: session={session_id} req_id={request_id} "
            f"approved={approved} ok={ok}"
        )
        if not ok:
            raise HTTPException(
                status_code=410,
                detail=f"confirm failed: {request_id} 已失效（超时/取消/不存在）",
            )
        return {"ok": True, "request_id": request_id, "approved": approved}

    @router.post("/sessions/{session_id}/messages", response_model=SendMessageResponse)
    async def send_message(session_id: str, req: SendMessageRequest) -> SendMessageResponse:
        from utils.i18n import t
        ctx = loop.context.get(session_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail=t("session.not_found", id=session_id))

        tool_calls_seen: List[Dict[str, Any]] = []

        async def cb(event):
            if event.type == "tool_call":
                tool_calls_seen.append(event.data)

        content = await loop.run(session_id, req.content, stream_callback=cb)
        return SendMessageResponse(content=content, tool_calls=tool_calls_seen)

    @router.post(
        "/sessions/{session_id}/workspace",
        response_model=WorkspaceResponse,
    )
    async def set_workspace(
        session_id: str, req: SetWorkspaceRequest
    ) -> WorkspaceResponse:
        """设置/更新会话的受信任工作目录。

        校验失败仍返回 200，调用方根据 ok 字段判断（与 Claude Code 一致）。
        """
        from utils.i18n import t
        # 兼容：先确保 session 存在；否则立即创建（空 metadata）
        if loop.context.get(session_id) is None:
            loop.create_session(metadata={"_synthetic": True})
            # 重新拿一次 session_id（create_session 会分配新 uuid，但语义上我们用请求中的 sid）
            # 由于现有 API 设计：必须先 POST /sessions 才能使用，这里按其语义要求侧错误：
            raise HTTPException(
                status_code=404, detail=t("session.not_found", id=session_id)
            )

        result = loop.set_workspace(session_id, req.path)
        return WorkspaceResponse(
            session_id=session_id,
            raw=result.get("raw"),
            root=result.get("root"),
            previous_root=result.get("previous_root"),
            set_at=result.get("set_at", ""),
            ok=result.get("ok", False),
            reason=result.get("reason"),
        )

    @router.get("/sessions/{session_id}/workspace", response_model=WorkspaceResponse)
    async def get_workspace(session_id: str) -> WorkspaceResponse:
        from utils.i18n import t
        ws = loop.get_workspace(session_id)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=t("workspace.not_set", id=session_id),
            )
        # ws 是 {raw, root, set_at} dict（由 loop.get_workspace 提供）
        return WorkspaceResponse(
            session_id=session_id,
            raw=ws.get("raw"),
            root=ws.get("root"),
            previous_root=None,
            set_at=ws.get("set_at") or "",
            ok=True,
            reason=None,
        )

    @router.get("/sessions/{session_id}/traces", response_model=TracesResponse)
    async def get_traces(session_id: str) -> TracesResponse:
        replay = replay_session(session_id)
        from utils.redactor import redact
        items = [
            TraceItem(
                trace_id=t.get("trace_id", ""),
                step=t.get("step", ""),
                actor=t.get("actor", ""),
                ts=t.get("ts", ""),
                input=redact(_safe_json(t.get("input"))),
                output=redact(_safe_json(t.get("output"))),
                decision_rationale=t.get("rationale"),
                user_confirmed=t.get("user_confirmed"),
            )
            for t in replay["traces"]
        ]
        return TracesResponse(session_id=session_id, traces=items)

    @router.get("/tools", response_model=ToolListResponse)
    async def list_tools() -> ToolListResponse:
        reg = get_default_registry()
        tools = [
            ToolInfo(
                name=t.name,
                description=t.description,
                required_capability=t.required_capability,
                input_schema=t.input_schema,
            )
            for t in reg.list_tools()
        ]
        return ToolListResponse(tools=tools)

    return router


# ---- MCP JSON-RPC 端点（顶层路由，不在 /api 前缀下）----
# 作为独立 router 让 api.app 在 create_app() 中按需 include。
# Stage 5 Task 5.2：暴露 OpsAgent 工具集给外部 MCP 客户端（stdio 模式见 main.py）。
mcp_router = APIRouter()


@mcp_router.post("/mcp")
async def mcp_endpoint(request: Request):
    """JSON-RPC MCP 端点 — 转发到 MCPServer.handle_request。

    直接读 raw body（不依赖 FastAPI 的 JSON 解析），
    这样无效 JSON 会落到 MCPServer 的 PARSE_ERROR 错误码而不是 422。
    HTTP 状态始终 200，错误通过 JSON-RPC body 表达。
    """
    from fastapi.responses import PlainTextResponse
    from mcp.server import MCPServer

    raw_bytes = await request.body()
    raw = raw_bytes.decode("utf-8") if raw_bytes else ""
    server = MCPServer()
    response_str = await server.handle_request(raw)
    return PlainTextResponse(content=response_str, media_type="application/json")


def _safe_json(s: Any) -> Dict[str, Any]:
    import json
    if not s:
        return {}
    if isinstance(s, dict):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}