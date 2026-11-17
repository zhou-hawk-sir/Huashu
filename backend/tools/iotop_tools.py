"""磁盘 I/O 监控工具

赛题要求 OS 环境感知能力包含 iotop/iostat 等 I/O 监控工具。
"""
from __future__ import annotations

from typing import Any

from .base import RiskLevel, Tool, ToolResult
from ._runner import run_cmd


class IOStatTool(Tool):
    """磁盘 I/O 统计（iostat -xz）"""
    name = "disk.iostat"
    description = "报告 CPU 和磁盘 I/O 统计信息（iostat -xz）"
    risk_level = RiskLevel.LOW
    required_capability = "disk.read"
    input_schema = {
        "type": "object",
        "properties": {
            "interval": {
                "type": "integer",
                "default": 1,
                "description": "采样间隔秒数",
            },
            "count": {
                "type": "integer",
                "default": 3,
                "description": "采样次数",
            },
            "device": {
                "type": "string",
                "description": "仅显示指定设备（如 vda / sda）",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        interval = kwargs.get("interval", 1)
        count = kwargs.get("count", 3)
        device = kwargs.get("device")

        cmd = ["iostat", "-x", "-z", str(interval), str(count)]
        if device:
            cmd.append(device)

        try:
            result = await run_cmd(cmd, timeout=interval * count + 10)
        except FileNotFoundError:
            return ToolResult(
                success=False,
                error="iostat 命令未找到（麒麟系统需安装 sysstat 包：yum install sysstat）",
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        return ToolResult(
            success=(result.returncode == 0),
            output=result.stdout,
            error=result.stderr if result.returncode != 0 else None,
        )


class IOTopTool(Tool):
    """按进程显示 I/O 使用量（iotop -bno）"""
    name = "disk.iotop"
    description = "按进程显示 I/O 使用量（iotop -bno），高 I/O 进程排查"
    risk_level = RiskLevel.LOW
    required_capability = "disk.read"
    input_schema = {
        "type": "object",
        "properties": {
            "iterations": {
                "type": "integer",
                "default": 5,
                "description": "采样轮次",
            },
            "process_name": {
                "type": "string",
                "description": "过滤进程名",
            },
            "only_io": {
                "type": "boolean",
                "default": True,
                "description": "仅显示有 I/O 活动的进程",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        iterations = kwargs.get("iterations", 5)
        process_name = kwargs.get("process_name")
        only_io = kwargs.get("only_io", True)

        cmd = ["iotop", "-b", "-n", str(iterations)]
        if only_io:
            cmd.append("-o")
        if process_name:
            cmd += ["-p", process_name]

        try:
            result = await run_cmd(cmd, timeout=30)
        except FileNotFoundError:
            return ToolResult(
                success=False,
                error="iotop 命令未找到（麒麟系统需安装 iotop 包，且需要 root/opsagent 权限）",
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        return ToolResult(
            success=(result.returncode == 0),
            output=result.stdout,
            error=result.stderr if result.returncode != 0 else None,
        )


class DfTool(Tool):
    """磁盘空间查询（df -hT）"""
    name = "disk.df"
    description = "查看磁盘空间和文件系统类型（df -hT）"
    risk_level = RiskLevel.LOW
    required_capability = "disk.read"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "default": "/",
                "description": "查看指定挂载点的磁盘使用",
            },
            "inodes": {
                "type": "boolean",
                "default": False,
                "description": "是否查看 inode 使用率",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        path = kwargs.get("path", "/")
        inodes = kwargs.get("inodes", False)

        cmd = ["df", "-hT"]
        if inodes:
            cmd.insert(2, "-i")
        cmd.append(path)

        try:
            result = await run_cmd(cmd, timeout=5)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr)

        lines = result.stdout.strip().splitlines()
        return ToolResult(
            success=True,
            output={
                "path": path,
                "inodes": inodes,
                "count": len(lines),
                "lines": lines,
            },
        )


class DuTool(Tool):
    """目录/文件大小查询（du -sh）"""
    name = "disk.du"
    description = "查看目录或文件大小（du -sh）"
    risk_level = RiskLevel.LOW
    required_capability = "disk.read"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "default": "/var",
                "description": "查询路径",
            },
            "max_depth": {
                "type": "integer",
                "default": 1,
                "description": "最大目录深度",
            },
            "limit": {
                "type": "integer",
                "default": 20,
                "description": "最多显示条数",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        import re
        path = kwargs.get("path", "/var")
        max_depth = kwargs.get("max_depth", 1)
        limit = kwargs.get("limit", 20)

        # 安全校验：路径只能是绝对路径
        if not re.match(r"^/[A-Za-z0-9_./-]*$", path):
            return ToolResult(success=False, error="Invalid path")

        cmd = ["du", "-sh", "--max-depth", str(max_depth), path]

        try:
            result = await run_cmd(cmd, timeout=30)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr)

        lines = result.stdout.strip().splitlines()
        entries = []
        for line in lines:
            parts = line.split("\t", 1)
            if len(parts) == 2:
                size, p = parts
                entries.append({"size": size, "path": p})

        entries.sort(key=lambda e: _parse_size(e["size"]), reverse=True)
        return ToolResult(
            success=True,
            output={
                "path": path,
                "count": len(entries),
                "entries": entries[:limit],
            },
        )


def _parse_size(size_str: str) -> int:
    """将人类可读大小字符串转为字节数"""
    size_str = size_str.strip().upper()
    units = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    for unit, mult in units.items():
        if size_str.endswith(unit):
            try:
                return int(float(size_str[:-1]) * mult)
            except ValueError:
                return 0
    try:
        return int(size_str)
    except ValueError:
        return 0
