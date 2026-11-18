"""Agent Core 主引擎

Agent 主循环设计：
- 接收用户输入
- 上下文管理（滑动窗口）
- Prompt 构建（系统提示 + 工具 schema + 历史）
- LLM 决策
- 工具调度 + 安全护栏
- 结果回填 + 循环

入口在 loop.py
"""
from .context import ContextManager, SessionContext
from .prompt_builder import PromptBuilder
from .dispatcher import ToolDispatcher
from .loop import AgentLoop

__all__ = [
    "ContextManager",
    "SessionContext",
    "PromptBuilder",
    "ToolDispatcher",
    "AgentLoop",
]