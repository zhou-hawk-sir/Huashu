"""lsof / ss 端口分析工具

赛题明确要求 OS 环境感知能力包含 lsof。
"""
from __future__ import annotations

from typing import Any

from .base import RiskLevel, Tool, ToolResult
from ._runner import run_cmd


class LsofTool(Tool):
    """列出打开文件/套接字的进程（lsof -nP）

    等价命令：lsof -nP
    用途：排查端口占用、文件句柄泄露、进程打开情况
    """
    name = "process.lsof"
    description = "列出打开文件/套接字的进程（lsof -nP），用于排查端口占用、文件句柄泄露"
    risk_level = RiskLevel.LOW
    required_capability = "process.read"
    input_schema = {
        "type": "object",
        "properties": {
            "filter_type": {
                "type": "string",
                "enum": ["all", "tcp", "udp", "file", "ipv4", "ipv6"],
                "default": "all",
                "description": "过滤类型",
            },
            "port": {
                "type": "integer",
                "description": "仅显示占用指定端口的进程",
            },
            "process_name": {
                "type": "string",
                "description": "仅显示指定进程名的记录",
            },
            "user": {
                "type": "string",
                "description": "仅显示指定用户的进程",
            },
            "limit": {
                "type": "integer",
                "default": 100,
                "description": "最大返回条数",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        cmd = ["lsof", "-nP"]
        filter_type = kwargs.get("filter_type", "all")
        port = kwargs.get("port")
        process_name = kwargs.get("process_name")
        user = kwargs.get("user")
        limit = kwargs.get("limit", 100)

        if filter_type == "tcp":
            cmd += ["-iTCP"]
        elif filter_type == "udp":
            cmd += ["-iUDP"]
        elif filter_type == "ipv4":
            cmd += ["-i", "4"]
        elif filter_type == "ipv6":
            cmd += ["-i", "6"]

        if port:
            cmd += ["-i", f":{port}"]
        if process_name:
            cmd += ["-c", process_name]
        if user:
            cmd += ["-u", user]

        cmd += ["-l"]  # numeric UIDs

        try:
            result = await run_cmd(cmd, timeout=10)
        except FileNotFoundError:
            return ToolResult(success=False, error="lsof 命令未找到（麒麟系统需安装 lsof 包）")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0 and "permission denied" not in result.stderr.lower():
            return ToolResult(success=False, error=result.stderr)

        lines = result.stdout.splitlines()
        return ToolResult(
            success=True,
            output={
                "count": len(lines),
                "entries": lines[:limit],
                "command": " ".join(cmd),
            },
        )


class PortsTool(Tool):
    """快速端口占用查询（ss 优先，netstat 兜底）"""
    name = "process.ports"
    description = "快速查看端口占用情况（ss -tunlp）"
    risk_level = RiskLevel.LOW
    required_capability = "network.read"
    input_schema = {
        "type": "object",
        "properties": {
            "port": {
                "type": "integer",
                "description": "仅显示指定端口",
            },
            "proto": {
                "type": "string",
                "enum": ["tcp", "udp", "all"],
                "default": "all",
            },
            "state": {
                "type": "string",
                "enum": ["LISTEN", "ESTABLISHED", "all"],
                "default": "all",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        port = kwargs.get("port")
        proto = kwargs.get("proto", "all").lower()
        state = kwargs.get("state", "all").upper()

        # ss 优先
        try:
            cmd = ["ss", "-tunlp"]
            result = await run_cmd(cmd, timeout=10)
            backend = "ss"
            if result.returncode != 0:
                raise FileNotFoundError("ss failed")
        except FileNotFoundError:
            # 兜底 netstat
            cmd = ["netstat", "-tunlp"]
            result = await run_cmd(cmd, timeout=10)
            backend = "netstat"
        except Exception as e:
            return ToolResult(success=False, error=f"端口查询失败: {e}")

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr)

        lines = result.stdout.splitlines()
        filtered = []
        for line in lines[1:]:
            if proto != "all" and proto not in line.lower().split()[0].lower():
                continue
            if state != "all" and state not in line.upper():
                continue
            if port:
                if f":{port}" not in line:
                    continue
            filtered.append(line)

        return ToolResult(
            success=True,
            output={
                "backend": backend,
                "count": len(filtered),
                "connections": filtered,
            },
        )


class FdInfoTool(Tool):
    """查看进程文件描述符信息"""
    name = "process.fd"
    description = "查看指定进程打开的文件描述符（ls /proc/<pid>/fd）"
    risk_level = RiskLevel.LOW
    required_capability = "process.read"
    input_schema = {
        "type": "object",
        "properties": {
            "pid": {
                "type": "integer",
                "description": "进程 PID",
            },
            "limit": {
                "type": "integer",
                "default": 50,
                "description": "最大返回条数",
            },
        },
        "required": ["pid"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        pid = kwargs.get("pid")
        limit = kwargs.get("limit", 50)

        if not isinstance(pid, int) or pid <= 0:
            return ToolResult(success=False, error="pid 必须为正整数")

        fd_dir = f"/proc/{pid}/fd"
        cmd = ["ls", "-la", fd_dir]
        try:
            result = await run_cmd(cmd, timeout=5)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0:
            return ToolResult(
                success=False,
                error=f"无法读取 /proc/{pid}/fd（进程可能不存在或无权限）",
            )

        lines = result.stdout.strip().splitlines()
        return ToolResult(
            success=True,
            output={
                "pid": pid,
                "count": len(lines),
                "fds": lines[:limit],
            },
        )
