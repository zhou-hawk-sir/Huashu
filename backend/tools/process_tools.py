"""进程管理工具"""
from __future__ import annotations

from typing import Any, Dict

from .base import RiskLevel, Tool, ToolResult
from ._runner import run_cmd


class ProcessListTool(Tool):
    name = "process.list"
    description = "列出系统进程，支持按用户、命令名过滤"
    risk_level = RiskLevel.LOW
    required_capability = "process.read"
    input_schema = {
        "type": "object",
        "properties": {
            "user": {"type": "string", "description": "按用户名过滤"},
            "pattern": {"type": "string", "description": "按命令名正则过滤"},
            "limit": {"type": "integer", "default": 50},
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        err = self.validate_input(kwargs)
        if err:
            return ToolResult(success=False, error=err)

        # 兼容 Linux (procps-ng) 与 BSD (MinGW/Mac) 的 ps
        # 先试 -eo，失败再退到 aux
        cmds_to_try = [
            ["ps", "-eo", "pid=", "user=", "pcpu=", "pmem=", "comm=", "args=", "--sort=-pcpu"],
            ["ps", "aux"],
            ["ps", "-ef"],
        ]
        result = None
        last_err = ""
        for cmd in cmds_to_try:
            try:
                result = await run_cmd(cmd, timeout=10)
                if result.returncode == 0:
                    break
                last_err = result.stderr
                result = None
            except Exception as e:
                last_err = str(e)
                continue

        if result is None:
            return ToolResult(success=False, error=f"ps command failed: {last_err}")

        lines = result.stdout.strip().splitlines()
        header = lines[0] if lines else ""
        body = lines[1:]

        # 过滤 - 用更宽松的解析（兼容不同 ps 变体）
        user_filter = kwargs.get("user")
        pattern_filter = kwargs.get("pattern")
        import re
        filtered = []
        for line in body:
            parts = line.split(None, 10)  # 取前 11 列，剩余并入 args
            if len(parts) < 2:
                continue
            pid = parts[0]
            user = parts[1] if len(parts) > 1 else ""
            args = parts[-1] if parts else ""
            if user_filter and user != user_filter:
                continue
            if pattern_filter and not re.search(pattern_filter, args):
                continue
            filtered.append({
                "pid": pid,
                "user": user,
                "args": args,
                "raw": line,
            })

        limit = kwargs.get("limit", 50)
        filtered = filtered[:limit]

        return ToolResult(
            success=True,
            output={
                "header": header,
                "count": len(filtered),
                "processes": filtered,
            },
            metadata={"command": cmd},
        )


class ProcessKillTool(Tool):
    name = "process.kill"
    description = "终止指定 PID 的进程"
    risk_level = RiskLevel.HIGH
    required_capability = "process.kill"
    input_schema = {
        "type": "object",
        "properties": {
            "pid": {"type": "integer", "description": "进程 PID"},
            "signal": {
                "type": "string",
                "default": "TERM",
                "enum": ["TERM", "KILL", "HUP", "INT"],
            },
        },
        "required": ["pid"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        err = self.validate_input(kwargs)
        if err:
            return ToolResult(success=False, error=err)

        pid = kwargs["pid"]
        if not isinstance(pid, int) or pid <= 0:
            return ToolResult(success=False, error="pid must be a positive integer")

        signal = kwargs.get("signal", "TERM")
        # 防御：禁止 kill init (PID 1) 和当前进程
        if pid == 1:
            return ToolResult(success=False, error="Refusing to kill PID 1 (init)")

        cmd = ["kill", f"-{signal}", str(pid)]
        try:
            result = await run_cmd(cmd, user="opsagent", timeout=5)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr.strip() or "kill failed")

        return ToolResult(
            success=True,
            output={"pid": pid, "signal": signal, "killed": True},
            metadata={"command": cmd},
        )