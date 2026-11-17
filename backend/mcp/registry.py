"""工具注册中心"""
from __future__ import annotations

from typing import Dict, List, Optional

from tools.base import Tool
from .protocol import ToolDefinition, ToolsListResult, ToolCallResult


class ToolRegistry:
    """工具注册中心：维护 name → Tool 实例的映射。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool must have a name")
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    def list_definitions(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                risk_level=t.risk_level.value,
                required_capability=t.required_capability,
            )
            for t in self._tools.values()
        ]

    def to_tools_list_result(self) -> ToolsListResult:
        return ToolsListResult(tools=self.list_definitions())

    async def call(self, name: str, arguments: Dict) -> ToolCallResult:
        tool = self.get(name)
        if tool is None:
            return ToolCallResult(
                content=[{"type": "text", "text": f"Tool not found: {name}"}],
                is_error=True,
            )

        try:
            result = await tool.run(**arguments)
        except Exception as e:
            return ToolCallResult(
                content=[{"type": "text", "text": f"Tool execution error: {e}"}],
                is_error=True,
            )

        return ToolCallResult(
            content=[{"type": "text", "text": result.to_text()}],
            is_error=not result.success,
        )


_default_registry: Optional[ToolRegistry] = None


def get_default_registry() -> ToolRegistry:
    """获取全局默认注册中心（懒加载，注册全部内置工具）。"""
    global _default_registry
    if _default_registry is None:
        from tools import all_tools
        _default_registry = ToolRegistry()
        for tool in all_tools():
            _default_registry.register(tool)
    return _default_registry


def reset_default_registry() -> None:
    """测试用：重置全局注册中心。"""
    global _default_registry
    _default_registry = None