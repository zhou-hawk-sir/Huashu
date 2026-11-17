"""二次确认管理器

中危操作需用户确认。通过 asyncio.Future 等待 WebSocket 端的确认响应。

安全保证（C2 修复）：
- request_id 与 session_id 绑定：跨 session 解析 request_id 直接拒绝
- 不存在的 request_id 解析返回 False
- 一次 request_id 只能 resolve 一次（第二次 resolve 返回 False）
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class ConfirmationRequest:
    tool: str
    arguments: dict
    rationale: str
    future: asyncio.Future
    session_id: str  # 绑定创建它的 session


class ConfirmationManager:
    """管理进行中的确认请求。"""

    def __init__(self, default_timeout: float = 60.0) -> None:
        self._pending: Dict[str, ConfirmationRequest] = {}
        self._counter = 0
        self._lock = asyncio.Lock()
        self.default_timeout = default_timeout

    def _next_id(self) -> str:
        self._counter += 1
        return f"confirm_{self._counter}"

    async def request(
        self,
        tool: str,
        arguments: dict,
        rationale: str,
        timeout: Optional[float] = None,
        session_id: str = "default",
    ) -> tuple[str, asyncio.Future]:
        """注册一个待确认请求，返回 (request_id, future)。

        session_id 绑定到该 request，跨 session 解析将被拒。
        """
        async with self._lock:
            req_id = self._next_id()
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending[req_id] = ConfirmationRequest(
                tool=tool,
                arguments=arguments,
                rationale=rationale,
                future=fut,
                session_id=session_id,
            )
            return req_id, fut

    async def resolve(
        self,
        req_id: str,
        approved: bool,
        reason: str = "",
        session_id: str = "default",
    ) -> bool:
        """处理用户的确认响应。

        校验：
        1. req_id 必须存在
        2. req_id 绑定的 session_id 必须与传入的 session_id 一致
        3. 同一 req_id 只能 resolve 一次

        注意（竞态修复）：**不再 pop** _pending，由 wait() 拿到 future 结果后再清。
        旧实现里 resolve 立刻 pop + set_result，而 wait 是分两步（先 get dict，再
        await future），竞态下 wait 看到 dict 已被清空，返回 "request not found"，
        executor 误判为 user_denied，整个工具被跳过。
        """
        async with self._lock:
            req = self._pending.get(req_id)
            if req is None:
                return False
            if req.session_id != session_id:
                return False
            if req.future.done():
                return False
            req.future.set_result({"approved": approved, "reason": reason})
            return True

    async def wait(self, req_id: str, timeout: Optional[float] = None) -> dict:
        """等待确认结果。"""
        fut: Optional[asyncio.Future] = None
        async with self._lock:
            req = self._pending.get(req_id)
            if req is not None:
                fut = req.future
        if fut is None:
            return {"approved": False, "reason": "request not found"}
        try:
            result = await asyncio.wait_for(fut, timeout=timeout or self.default_timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                self._pending.pop(req_id, None)
            return {"approved": False, "reason": "confirmation timeout"}
        # 拿到结果后清 _pending（resolve 不再 pop，避免与本方法两步查询竞态）
        async with self._lock:
            self._pending.pop(req_id, None)
        return result

    def pending_count(self) -> int:
        return len(self._pending)
