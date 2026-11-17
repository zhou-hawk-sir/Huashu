"""二次确认状态机

完善确认机制，支持：
- 状态流转：PENDING → APPROVED | REJECTED | TIMED_OUT | CANCELLED
- 超时自动取消
- 最多重试 N 次
- 支持 WebSocket 端确认响应
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Dict, Optional

from utils.logger import get_logger

logger = get_logger("safety.confirmation")


class ConfirmationState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass
class ConfirmationRequestSM:
    """确认请求状态记录"""
    request_id: str
    tool: str
    arguments: dict
    rationale: str
    state: ConfirmationState = ConfirmationState.PENDING
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    user_reason: str = ""
    retry_count: int = 0
    max_retries: int = 2


class ConfirmationStateMachine:
    """
    确认状态机。

    支持超时和重试的确认请求管理。
    """

    def __init__(
        self,
        default_timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        self._requests: Dict[str, ConfirmationRequestSM] = {}
        self._counter = 0
        self._lock = asyncio.Lock()
        self._timeout_tasks: Dict[str, asyncio.Task] = {}

    def _next_id(self) -> str:
        self._counter += 1
        return f"confirm_{self._counter}_{int(time.time() * 1000)}"

    async def request(
        self,
        tool: str,
        arguments: dict,
        rationale: str,
        timeout: Optional[float] = None,
        retry_count: int = 0,
    ) -> tuple[str, asyncio.Future, ConfirmationRequestSM]:
        """
        创建确认请求，启动超时计时器。

        返回 (request_id, future, request)
        """
        async with self._lock:
            req_id = self._next_id()
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            req = ConfirmationRequestSM(
                request_id=req_id,
                tool=tool,
                arguments=arguments,
                rationale=rationale,
                retry_count=retry_count,
                max_retries=self.max_retries,
            )
            self._requests[req_id] = req

        timeout_val = timeout or self.default_timeout
        self._timeout_tasks[req_id] = asyncio.create_task(
            self._timeout_handler(req_id, timeout_val)
        )

        logger.info(
            f"Confirmation requested: {req_id} tool={tool} "
            f"timeout={timeout_val}s retry={retry_count}/{self.max_retries}"
        )
        return req_id, fut, req

    async def resolve(
        self,
        req_id: str,
        approved: bool,
        reason: str = "",
    ) -> bool:
        """处理确认响应"""
        async with self._lock:
            req = self._requests.get(req_id)
            if req is None or req.state != ConfirmationState.PENDING:
                return False

            # 取消超时任务
            if req_id in self._timeout_tasks:
                self._timeout_tasks[req_id].cancel()
                del self._timeout_tasks[req_id]

            req.state = ConfirmationState.APPROVED if approved else ConfirmationState.REJECTED
            req.resolved_at = time.time()
            req.user_reason = reason

            if not req.fut.done():
                req.fut.set_result({
                    "approved": approved,
                    "reason": reason,
                    "request_id": req_id,
                    "state": req.state.value,
                    "retry_count": req.retry_count,
                    "can_retry": req.retry_count < req.max_retries,
                })

            logger.info(f"Confirmation {req_id}: {req.state.value} reason={reason}")
            return True

    async def _timeout_handler(self, req_id: str, timeout: float) -> None:
        """超时处理"""
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return

        async with self._lock:
            req = self._requests.get(req_id)
            if req is None or req.state != ConfirmationState.PENDING:
                return

            req.state = ConfirmationState.TIMED_OUT
            req.resolved_at = time.time()

            if not req.fut.done():
                req.fut.set_result({
                    "approved": False,
                    "reason": "confirmation timeout",
                    "request_id": req_id,
                    "state": ConfirmationState.TIMED_OUT.value,
                    "retry_count": req.retry_count,
                    "can_retry": req.retry_count < req.max_retries,
                })

            if req_id in self._timeout_tasks:
                del self._timeout_tasks[req_id]

            logger.warning(f"Confirmation {req_id} timed out after {timeout}s")

    async def get_state(self, req_id: str) -> Optional[ConfirmationState]:
        """查询请求状态"""
        async with self._lock:
            req = self._requests.get(req_id)
            return req.state if req else None

    def list_pending(self) -> list[ConfirmationRequestSM]:
        """列出所有待确认请求"""
        return [
            r for r in self._requests.values()
            if r.state == ConfirmationState.PENDING
        ]
