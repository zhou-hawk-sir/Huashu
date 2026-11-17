"""工具调度器：调用 Executor 并把结果回填到上下文

支持并行调度：
- 只读/低风险工具（LOW）可并行执行
- 中高风险工具（MEDIUM/HIGH）必须串行
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from llm.base import ToolCall
from mcp.registry import get_default_registry
from mcp.manifest import resolve_tool_name
from tools.base import RiskLevel
from utils.logger import get_logger
from execution.executor import Executor, ExecutionResult

logger = get_logger("agent_core.dispatcher")


class ToolDispatcher:
    """工具调度器：接收 LLM 的 tool_calls 列表，并行/串行调用 Executor。"""

    def __init__(self, executor: Executor) -> None:
        self.executor = executor
        try:
            from mcp.manifest import to_provider_name
            self._provider_names = [to_provider_name(t.name) for t in get_default_registry().list_tools()]
        except Exception:
            self._provider_names = []

    def _to_internal_name(self, name: str) -> str:
        """LLM 返回的名字 → 内部工具名（含 dot 的形式）。"""
        if not name:
            return name
        if "." in name:
            return name
        return resolve_tool_name(name, self._provider_names)

    def _get_risk_level(self, tool_name: str) -> RiskLevel:
        """获取工具的风险等级"""
        try:
            tool = get_default_registry().get(tool_name)
            if tool:
                return tool.risk_level
        except Exception:
            pass
        return RiskLevel.MEDIUM

    def _split_independent(
        self,
        tool_calls: List[ToolCall],
    ) -> List[List[ToolCall]]:
        """
        将工具调用分组：独立组（可并行）vs 依赖组（必须串行）。

        策略：
        - LOW 风险工具可以并行（无副作用的查询操作）
        - MEDIUM/HIGH 风险工具必须串行（状态修改或高危操作）
        """
        independent: List[ToolCall] = []
        dependent: List[ToolCall] = []

        for tc in tool_calls:
            internal_name = self._to_internal_name(tc.name)
            risk = self._get_risk_level(internal_name)
            if risk == RiskLevel.LOW:
                independent.append(tc)
            else:
                dependent.append(tc)

        groups = []
        if independent:
            groups.append(independent)
        for tc in dependent:
            groups.append([tc])
        return groups

    async def dispatch(
        self,
        tool_calls: List[ToolCall],
        confirm_callback = None,
        workspace_root: Optional[str] = None,
        session_id: Optional[str] = None,
        confirm_timeout: Optional[float] = None,
    ) -> List[ExecutionResult]:
        """
        分发工具调用：

        1. 将 tool_calls 分为独立组（可并行）和依赖组（必须串行）
        2. 独立组内并行执行（asyncio.gather）
        3. 依赖组串行执行
        4. 结果按原顺序拼接返回

        workspace_root: 受信任工作目录（realpath 后），向下传给 executor；
        session_id: 当前会话 ID，用于 confirm_request 与该会话绑定。
                        若路径落在 workspace 内，MEDIUM 自动 ALLOW。
        confirm_timeout: 用户在前端确认的最长等待时间（秒），向下传给 executor。
                        None → ConfirmationManager.default_timeout（默认 60s）。
                        H6 修复：移除了 executor 中硬编码的 10s。
        """
        if not tool_calls:
            return []

        groups = self._split_independent(tool_calls)
        results: List[ExecutionResult] = []

        for group in groups:
            if len(group) == 1:
                tc = group[0]
                internal_name = self._to_internal_name(tc.name)
                logger.info(f"Dispatching tool (serial): {internal_name}")
                result = await self.executor.execute(
                    tool_name=internal_name,
                    arguments=tc.arguments,
                    confirm_callback=confirm_callback,
                    workspace_root=workspace_root,
                    session_id=session_id,
                    confirm_timeout=confirm_timeout,
                )
                results.append(result)
            else:
                logger.info(f"Dispatching {len(group)} tools in parallel")
                # return_exceptions=True 防止一个兄弟 raise 取消其它（C7）
                gathered = await asyncio.gather(*[
                    self._execute_one(tc, confirm_callback, workspace_root, session_id, confirm_timeout)
                    for tc in group
                ], return_exceptions=True)
                group_results: List[ExecutionResult] = []
                for tc, item in zip(group, gathered):
                    if isinstance(item, Exception):
                        # 异常包装为失败 ExecutionResult，不让任何 tool_call 缺位
                        logger.error(
                            f"Tool {tc.name}({tc.id}) raised: {item!r}"
                        )
                        group_results.append(ExecutionResult(
                            tool=tc.name,
                            success=False,
                            output=None,
                            error=f"dispatcher exception: {item!r}",
                            duration_ms=0,
                            decision="error",
                        ))
                    else:
                        group_results.append(item)
                results.extend(group_results)

        return results

    async def _execute_one(
        self,
        tc: ToolCall,
        confirm_callback = None,
        workspace_root: Optional[str] = None,
        session_id: Optional[str] = None,
        confirm_timeout: Optional[float] = None,
    ) -> ExecutionResult:
        """执行单个工具调用"""
        internal_name = self._to_internal_name(tc.name)
        logger.info(f"Dispatching tool (parallel): {internal_name}")
        return await self.executor.execute(
            tool_name=internal_name,
            arguments=tc.arguments,
            confirm_callback=confirm_callback,
            workspace_root=workspace_root,
            session_id=session_id,
            confirm_timeout=confirm_timeout,
        )

    @staticmethod
    def result_to_text(result: ExecutionResult) -> str:
        if result.success:
            return str(result.output) if result.output is not None else "OK"
        return f"Error: {result.error}"
