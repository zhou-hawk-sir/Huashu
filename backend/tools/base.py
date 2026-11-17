"""工具基类

仿照 Claude 的 Tool 抽象：name / description / input_schema / run()
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class ToolResult:
    """工具执行结果。"""
    success: bool = True
    output: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        if not self.success:
            return f"Error: {self.error}"
        if isinstance(self.output, str):
            return self.output
        import json
        return json.dumps(self.output, ensure_ascii=False, indent=2)


class Tool(abc.ABC):
    """所有运维工具的基类。"""

    name: str = ""
    description: str = ""
    input_schema: Dict[str, Any] = {}
    risk_level: RiskLevel = RiskLevel.LOW
    required_capability: str = ""

    @abc.abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """执行工具，返回 ToolResult。"""

    def to_manifest(self) -> Dict[str, Any]:
        """导出 Claude 风格 manifest。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk_level": self.risk_level.value,
            "required_capability": self.required_capability,
        }

    def validate_input(self, kwargs: Dict[str, Any]) -> Optional[str]:
        """基础校验：检查 required 字段是否存在。子类可重写。"""
        required = self.input_schema.get("required", [])
        for k in required:
            if k not in kwargs:
                return f"Missing required field: {k}"
        return None