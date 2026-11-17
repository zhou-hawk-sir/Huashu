"""WebSocket 路由：流式推送思维链 + 接收二次确认

安全（C1）：WS 连接必须带 `?token=<auth_token>`，否则 4401 拒绝。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from utils.logger import get_logger

logger = get_logger("api.ws")


def build_ws_router(loop) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/sessions/{session_id}")
    async def ws_session(ws: WebSocket, session_id: str):
        # C1: token 必须有，且与 session 绑定一致
        token = ws.query_params.get("token", "")
        if not loop.validate_session_token(session_id, token):
            await ws.accept()  # 必须先 accept 才能 close with code
            await ws.send_json({
                "type": "error",
                "data": {"message": "unauthorized: invalid or missing token"},
            })
            await ws.close(code=4401)
            logger.warning(
                f"WebSocket auth failed: session={session_id} token={'<empty>' if not token else '<provided>'}"
            )
            return

        await ws.accept()
        logger.info(f"WebSocket connected: session={session_id}")

        ctx = loop.context.get(session_id)
        if ctx is None:
            # 重启兜底：会话在 audit DB 里但内存中没有 → 尝试回补后再判
            try:
                ctx = loop._hydrate_ctx_from_db(session_id)
                if ctx is not None:
                    logger.info(
                        f"WS session {session_id} not in memory, "
                        f"hydrated from audit DB (restart recovery)"
                    )
            except Exception as e:
                logger.warning(f"WS hydrate failed for {session_id}: {e}")
                ctx = None
        if ctx is None:
            # 客户端已断开时 send_json 会触发 RuntimeError("after websocket.close")，
            # 用 try/except 兜住，避免日志被无关异常污染
            try:
                await ws.send_json({"type": "error", "data": {"message": "session not found"}})
                await ws.close()
            except RuntimeError:
                pass
            return

        # ---- 修复：WS 接收循环与 stream_run 必须并发 ----
        # 旧实现把 stream_run 放在 `async for` 里阻塞接收循环，
        # 导致 confirm_response 永远读不到 → 二次确认死锁。
        # 新实现：stream_run 由独立 Task 驱动，接收循环保持活跃。

        _stream_task: asyncio.Task | None = None  # 当前 stream_run 驱动 task
        _stream_done = asyncio.Event()             # stream_run 结束信号

        async def _run_stream(session_id_: str, content_: str, ws_: WebSocket):
            """驱动 stream_run async generator；emit 回调由 generator 内部调用。"""
            async def emit(event):
                try:
                    await ws_.send_json({
                        "type": event.type,
                        "data": event.data,
                    })
                except Exception:
                    pass
            try:
                async for _ in loop.stream_run(session_id_, content_, stream_callback=emit):
                    pass
            except Exception as e:
                logger.warning(f"stream_run task error: {e}")
            finally:
                _stream_done.set()

        try:
            while True:
                msg = await ws.receive_json()
                mtype = msg.get("type")
                data = msg.get("data") or {}

                if mtype == "user":
                    content = data.get("content", "")
                    if not content:
                        await ws.send_json({"type": "error", "data": {"message": "empty content"}})
                        continue

                    # 清理上一轮 stream（理论上上一轮结束才能到这，防并发重入）
                    if _stream_task and not _stream_task.done():
                        logger.warning(f"stream_run still active, cancelling before new user msg")
                        _stream_task.cancel()
                    _stream_done.clear()

                    # 启动 stream_run 独立 Task，不阻塞接收循环
                    _stream_task = asyncio.create_task(
                        _run_stream(session_id, content, ws)
                    )

                    # AI 标题生成：仅在第一次出现 user message 时异步触发（避免每轮重做）
                    sid_ctx = loop.context.get(session_id)
                    if sid_ctx is not None and not sid_ctx.metadata.get("title"):
                        asyncio.create_task(
                            loop.summarize_session_title(session_id, content)
                        )

                elif mtype == "confirm_response":
                    # stream_run 运行中：confirm_response 可以立即到达 confirmation manager
                    req_id = data.get("request_id", "")
                    approved = bool(data.get("approved", False))
                    reason = data.get("reason", "")
                    ok = await loop.handle_confirmation(session_id, req_id, approved, reason)
                    try:
                        await ws.send_json({
                            "type": "confirm_ack",
                            "data": {"request_id": req_id, "ok": ok},
                        })
                    except (RuntimeError, WebSocketDisconnect):
                        pass

                elif mtype == "ping":
                    try:
                        await ws.send_json({"type": "pong", "data": {}})
                    except (RuntimeError, WebSocketDisconnect):
                        pass

                elif mtype == "set_workspace":
                    path = data.get("path", "")
                    if not path:
                        await ws.send_json({
                            "type": "error",
                            "data": {"message": "workspace path 不能为空"},
                        })
                        continue
                    ev = await loop.handle_set_workspace(session_id, path)
                    try:
                        await ws.send_json({"type": ev.type, "data": ev.data})
                    except (RuntimeError, WebSocketDisconnect):
                        pass

                elif mtype == "cancel":
                    # 前端"停止思考"按钮 → 取消当前 worker task
                    cancelled = loop.cancel_session_run(session_id)
                    try:
                        await ws.send_json({
                            "type": "cancel_ack",
                            "data": {"cancelled": cancelled, "session_id": session_id},
                        })
                    except (RuntimeError, WebSocketDisconnect):
                        pass

                elif mtype == "get_workspace":
                    root = loop.get_workspace(session_id)
                    try:
                        await ws.send_json({
                            "type": "workspace_event",
                            "data": {
                                "ok": root is not None,
                                "root": root,
                                "session_id": session_id,
                                "reason": None if root else "未设置 workspace",
                            },
                        })
                    except (RuntimeError, WebSocketDisconnect):
                        pass

                else:
                    try:
                        await ws.send_json({
                            "type": "error",
                            "data": {"message": f"unknown message type: {mtype}"},
                        })
                    except (RuntimeError, WebSocketDisconnect):
                        pass

        except WebSocketDisconnect:
            # 客户端断开：取消正在跑的 stream_run task
            if _stream_task and not _stream_task.done():
                _stream_task.cancel()
                logger.info(f"WebSocket disconnected: session={session_id}, stream cancelled")
            else:
                logger.info(f"WebSocket disconnected: session={session_id}")
        except Exception as e:
            logger.exception("WebSocket error")
            if _stream_task and not _stream_task.done():
                _stream_task.cancel()
            try:
                await ws.send_json({"type": "error", "data": {"message": str(e)}})
            except Exception:
                pass

    return router