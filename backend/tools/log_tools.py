"""日志工具"""
from __future__ import annotations

import re
from typing import Any

from .base import RiskLevel, Tool, ToolResult
from ._runner import run_cmd


class LogJournalTool(Tool):
    name = "log.journal"
    description = "查询 systemd 日志"
    risk_level = RiskLevel.LOW
    required_capability = "log.read"
    input_schema = {
        "type": "object",
        "properties": {
            "unit": {"type": "string", "description": "服务单元名"},
            "since": {"type": "string", "description": "起始时间，如 '1 hour ago'"},
            "priority": {
                "type": "string",
                "enum": ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"],
            },
            "lines": {"type": "integer", "default": 100},
        },
        "required": [],
    }

    _SAFE_UNIT = re.compile(r"^[A-Za-z0-9_.\-@]+$")
    _SAFE_PRIORITY = {"emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"}
    _SAFE_SINCE = re.compile(r"^[A-Za-z0-9 :.\-]+$")

    async def run(self, **kwargs: Any) -> ToolResult:
        cmd = ["journalctl", "--no-pager", "-n", str(kwargs.get("lines", 100))]

        unit = kwargs.get("unit")
        if unit:
            if not self._SAFE_UNIT.match(unit):
                return ToolResult(success=False, error="Invalid unit name")
            cmd += ["-u", unit]

        priority = kwargs.get("priority")
        if priority:
            if priority not in self._SAFE_PRIORITY:
                return ToolResult(success=False, error="Invalid priority")
            cmd += ["-p", priority]

        since = kwargs.get("since")
        if since:
            if not self._SAFE_SINCE.match(since):
                return ToolResult(success=False, error="Invalid since value")
            cmd += ["--since", since]

        try:
            result = await run_cmd(cmd, timeout=15)
        except FileNotFoundError:
            return ToolResult(success=False, error="journalctl not available (systemd not installed?)")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr)

        return ToolResult(
            success=True,
            output={
                "unit": unit,
                "lines": len(result.stdout.splitlines()),
                "content": result.stdout,
            },
        )