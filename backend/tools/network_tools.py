"""网络工具"""
from __future__ import annotations

import re
from typing import Any

from .base import RiskLevel, Tool, ToolResult
from ._runner import run_cmd


class NetworkListTool(Tool):
    name = "network.list"
    description = "列出网络连接（netstat/ss）"
    risk_level = RiskLevel.LOW
    required_capability = "network.read"
    input_schema = {
        "type": "object",
        "properties": {
            "proto": {"type": "string", "enum": ["tcp", "udp", "all"], "default": "all"},
            "state": {"type": "string", "enum": ["LISTEN", "ESTABLISHED", "all"], "default": "all"},
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        err = self.validate_input(kwargs)
        if err:
            return ToolResult(success=False, error=err)

        # 优先 ss，回退 netstat
        cmd = ["ss", "-tunlp"]
        try:
            result = await run_cmd(cmd, timeout=10)
            backend = "ss"
        except FileNotFoundError:
            cmd = ["netstat", "-tunlp"]
            result = await run_cmd(cmd, timeout=10)
            backend = "netstat"

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr)

        proto_filter = kwargs.get("proto", "all").lower()
        state_filter = kwargs.get("state", "all").upper()

        connections = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if not parts:
                continue
            proto = parts[0].lower() if parts else ""
            state = parts[1] if len(parts) > 1 else ""

            if proto_filter != "all" and proto_filter not in proto:
                continue
            if state_filter != "all" and state_filter != state:
                continue
            connections.append(line)

        return ToolResult(
            success=True,
            output={"backend": backend, "count": len(connections), "connections": connections},
        )


class NetworkConnectivityTool(Tool):
    name = "network.connectivity"
    description = "测试到目标主机的网络连通性"
    risk_level = RiskLevel.LOW
    required_capability = "network.read"
    input_schema = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "目标主机或 IP"},
            "count": {"type": "integer", "default": 3},
        },
        "required": ["host"],
    }

    _HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.\-:]+$")

    async def run(self, **kwargs: Any) -> ToolResult:
        err = self.validate_input(kwargs)
        if err:
            return ToolResult(success=False, error=err)

        host = kwargs["host"]
        if not self._HOST_PATTERN.match(host):
            return ToolResult(success=False, error="Invalid host format")

        count = kwargs.get("count", 3)
        cmd = ["ping", "-c", str(count), "-W", "2", host]
        try:
            result = await run_cmd(cmd, timeout=15)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        return ToolResult(
            success=(result.returncode == 0),
            output={
                "host": host,
                "reachable": result.returncode == 0,
                "output": result.stdout,
            },
            error=result.stderr if result.returncode != 0 else None,
        )