"""systemd 服务工具"""
from __future__ import annotations

import re
from typing import Any

from .base import RiskLevel, Tool, ToolResult
from ._runner import run_cmd


# 关键服务：禁止 stop/disable
_CRITICAL_SERVICES = {"sshd", "network", "firewalld", "NetworkManager", "systemd-networkd"}


class ServiceStatusTool(Tool):
    name = "service.status"
    description = "查看 systemd 服务状态"
    risk_level = RiskLevel.LOW
    required_capability = "service.read"
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "服务名"},
        },
        "required": ["name"],
    }

    _SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-@]+$")

    async def run(self, **kwargs: Any) -> ToolResult:
        err = self.validate_input(kwargs)
        if err:
            return ToolResult(success=False, error=err)

        name = kwargs["name"]
        if not self._SAFE_NAME.match(name):
            return ToolResult(success=False, error="Invalid service name")

        cmd = ["systemctl", "status", name, "--no-pager", "-n", "10"]
        try:
            result = await run_cmd(cmd, timeout=15)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        return ToolResult(
            success=True,
            output={
                "name": name,
                "active": "active" in result.stdout,
                "raw": result.stdout,
            },
        )


class ServiceRestartTool(Tool):
    name = "service.restart"
    description = "重启 systemd 服务"
    risk_level = RiskLevel.MEDIUM
    required_capability = "service.write"
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "服务名"},
        },
        "required": ["name"],
    }

    _SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-@]+$")

    async def run(self, **kwargs: Any) -> ToolResult:
        err = self.validate_input(kwargs)
        if err:
            return ToolResult(success=False, error=err)

        name = kwargs["name"]
        if not self._SAFE_NAME.match(name):
            return ToolResult(success=False, error="Invalid service name")

        # 关键服务使用 reload 而非 restart
        if name in _CRITICAL_SERVICES:
            cmd = ["systemctl", "reload", name]
            action = "reload"
        else:
            cmd = ["systemctl", "restart", name]
            action = "restart"

        try:
            result = await run_cmd(cmd, user="opsagent", timeout=30)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr or "systemctl failed")

        return ToolResult(
            success=True,
            output={"name": name, "action": action, "ok": True},
            metadata={"command": cmd},
        )