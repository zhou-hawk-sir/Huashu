"""系统信息工具

补充核心系统感知工具：
- 系统信息（uname / hostname / uptime）
- 内存信息（free / vmstat）
- 负载信息（uptime / load average）
"""
from __future__ import annotations

from typing import Any

from .base import RiskLevel, Tool, ToolResult
from ._runner import run_cmd


class SystemInfoTool(Tool):
    """系统基本信息（hostname / uname / uptime）"""
    name = "system.info"
    description = "查看系统基本信息（hostname / uname / uptime）"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema = {
        "type": "object",
        "properties": {
            "detail": {
                "type": "boolean",
                "default": False,
                "description": "是否显示详细信息（uname -a）",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        detail = kwargs.get("detail", False)

        results = {}
        errors = []

        # hostname
        try:
            r = await run_cmd(["hostname"], timeout=3)
            results["hostname"] = r.stdout.strip() if r.returncode == 0 else ""
        except Exception as e:
            errors.append(f"hostname: {e}")

        # uptime
        try:
            r = await run_cmd(["uptime"], timeout=3)
            results["uptime"] = r.stdout.strip() if r.returncode == 0 else ""
        except Exception as e:
            errors.append(f"uptime: {e}")

        # uname
        try:
            cmd = ["uname"] if not detail else ["uname", "-a"]
            r = await run_cmd(cmd, timeout=3)
            results["uname"] = r.stdout.strip() if r.returncode == 0 else ""
        except Exception as e:
            errors.append(f"uname: {e}")

        if not results:
            return ToolResult(success=False, error=f"系统信息查询失败: {'; '.join(errors)}")

        return ToolResult(
            success=True,
            output=results,
        )


class MemoryInfoTool(Tool):
    """内存使用信息（free -h / vmstat）"""
    name = "system.memory"
    description = "查看内存和 swap 使用情况（free -h + vmstat）"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema = {
        "type": "object",
        "properties": {
            "vmstat_samples": {
                "type": "integer",
                "default": 0,
                "description": "vmstat 采样次数（0=不采样）",
            },
            "interval": {
                "type": "integer",
                "default": 2,
                "description": "vmstat 采样间隔（秒）",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        vmstat_samples = kwargs.get("vmstat_samples", 0)
        interval = kwargs.get("interval", 2)

        results = {}
        errors = []

        # free -h
        try:
            r = await run_cmd(["free", "-h"], timeout=5)
            if r.returncode == 0:
                results["free"] = r.stdout.strip()
            else:
                errors.append(f"free: {r.stderr}")
        except Exception as e:
            errors.append(f"free: {e}")

        # vmstat（可选采样）
        if vmstat_samples > 0:
            try:
                r = await run_cmd(
                    ["vmstat", str(interval), str(vmstat_samples)],
                    timeout=interval * vmstat_samples + 5,
                )
                if r.returncode == 0:
                    results["vmstat"] = r.stdout.strip()
            except Exception as e:
                results["vmstat_error"] = str(e)

        if not results:
            return ToolResult(success=False, error=f"内存信息查询失败: {'; '.join(errors)}")

        return ToolResult(success=True, output=results)


class LoadAvgTool(Tool):
    """系统负载信息（uptime / cat /proc/loadavg）"""
    name = "system.load"
    description = "查看系统负载（load average / uptime）"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema = {
        "type": "object",
        "properties": {
            "proc_loadavg": {
                "type": "boolean",
                "default": False,
                "description": "是否读取 /proc/loadavg",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        proc_loadavg = kwargs.get("proc_loadavg", False)

        results = {}
        errors = []

        # uptime（包含 load average）
        try:
            r = await run_cmd(["uptime"], timeout=3)
            if r.returncode == 0:
                results["uptime"] = r.stdout.strip()
        except Exception as e:
            errors.append(f"uptime: {e}")

        # /proc/loadavg（精确负载）
        if proc_loadavg:
            try:
                r = await run_cmd(["cat", "/proc/loadavg"], timeout=3)
                if r.returncode == 0:
                    results["loadavg"] = r.stdout.strip()
            except Exception as e:
                errors.append(f"/proc/loadavg: {e}")

        if not results:
            return ToolResult(success=False, error=f"负载查询失败: {'; '.join(errors)}")

        return ToolResult(success=True, output=results)


class UlimitTool(Tool):
    """查看系统资源限制（ulimit -a）"""
    name = "system.ulimit"
    description = "查看当前用户的系统资源限制（ulimit -a）"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        try:
            result = await run_cmd(["ulimit", "-a"], timeout=5)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr)

        lines = result.stdout.strip().splitlines()
        return ToolResult(
            success=True,
            output={"count": len(lines), "limits": lines},
        )


class DmesgTool(Tool):
    """系统日志消息（dmesg -T）"""
    name = "system.dmesg"
    description = "查看系统启动和内核日志（dmesg -T）"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema = {
        "type": "object",
        "properties": {
            "level": {
                "type": "string",
                "enum": ["emerg", "alert", "crit", "err", "warn", "notice", "info", "debug", "all"],
                "default": "err",
                "description": "日志级别过滤",
            },
            "lines": {
                "type": "integer",
                "default": 50,
                "description": "最大返回条数",
            },
            "grep": {
                "type": "string",
                "description": "grep 过滤关键词",
            },
        },
        "required": [],
    }

    _LEVEL_MAP = {
        "emerg": "emerg", "alert": "alert", "crit": "crit",
        "err": "err", "warn": "warn", "warning": "warn",
        "notice": "notice", "info": "info", "debug": "debug",
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        level = kwargs.get("level", "err")
        lines = kwargs.get("lines", 50)
        grep = kwargs.get("grep")

        # dmesg -T 有人类可读时间戳
        cmd = ["dmesg", "-T", "-l", self._LEVEL_MAP.get(level, level), "-n", "8"]
        if lines:
            cmd += ["--lines", str(lines)]

        try:
            result = await run_cmd(cmd, timeout=10)
        except FileNotFoundError:
            return ToolResult(success=False, error="dmesg 命令不可用")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr)

        output = result.stdout
        if grep:
            filtered = [l for l in output.splitlines() if grep.lower() in l.lower()]
            output = "\n".join(filtered)

        return ToolResult(
            success=True,
            output={
                "level": level,
                "count": len(output.splitlines()),
                "lines": output.splitlines(),
            },
        )
