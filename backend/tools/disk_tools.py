"""磁盘工具"""
from __future__ import annotations

import os
import re
from typing import Any

from .base import RiskLevel, Tool, ToolResult
from ._runner import run_cmd


class DiskUsageTool(Tool):
    name = "disk.usage"
    description = "查看磁盘使用情况（df -h）"
    risk_level = RiskLevel.LOW
    required_capability = "disk.read"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "/"},
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        path = kwargs.get("path", "/")
        if path and not re.match(r"^[A-Za-z0-9_./-]+$", path):
            return ToolResult(success=False, error="Invalid path")

        cmd = ["df", "-h", path]
        try:
            result = await run_cmd(cmd, timeout=10)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr)

        lines = result.stdout.strip().splitlines()
        return ToolResult(
            success=True,
            output={"path": path, "raw": lines},
        )


class DiskFindLargeTool(Tool):
    name = "disk.find_large"
    description = "查找大于指定大小的文件"
    risk_level = RiskLevel.LOW
    required_capability = "disk.read"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "/var"},
            "min_size_mb": {"type": "integer", "default": 100},
            "limit": {"type": "integer", "default": 20},
        },
        "required": [],
    }

    _SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./-]*$")

    async def run(self, **kwargs: Any) -> ToolResult:
        path = kwargs.get("path", "/var")
        if not self._SAFE_PATH.match(path):
            return ToolResult(success=False, error="Invalid path")

        min_size_mb = kwargs.get("min_size_mb", 100)
        limit = kwargs.get("limit", 20)
        size_kb = f"+{min_size_mb}M"

        # find /path -type f -size +500M -exec ls -lh {} \; | sort -k5 -rh | head -n 20
        # 为简化用 -printf
        cmd = [
            "find", path, "-xdev", "-type", "f", "-size", size_kb,
            "-printf", "%s\t%p\n",
        ]
        try:
            result = await run_cmd(cmd, timeout=60)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        if result.returncode != 0:
            return ToolResult(success=False, error=result.stderr)

        entries = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            try:
                size_b = int(parts[0])
            except ValueError:
                continue
            entries.append({
                "size_bytes": size_b,
                "size_human": _humanize(size_b),
                "path": parts[1],
            })

        # 排序 + 截取
        entries.sort(key=lambda e: e["size_bytes"], reverse=True)
        entries = entries[:limit]

        return ToolResult(
            success=True,
            output={"path": path, "min_size_mb": min_size_mb, "count": len(entries), "files": entries},
        )


class DiskCleanupTool(Tool):
    name = "disk.cleanup"
    description = "清理指定路径的过期文件"
    risk_level = RiskLevel.MEDIUM
    required_capability = "disk.write"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要清理的目录"},
            "pattern": {"type": "string", "default": "*.log.*"},
            "older_than_days": {"type": "integer", "default": 7},
        },
        "required": ["path"],
    }

    _ALLOWED_PREFIXES = ("/tmp/", "/var/tmp/", "/var/log/")

    async def run(self, **kwargs: Any) -> ToolResult:
        path = kwargs.get("path", "")
        if not path.startswith(self._ALLOWED_PREFIXES):
            return ToolResult(
                success=False,
                error=f"Cleanup only allowed in {self._ALLOWED_PREFIXES}",
            )

        pattern = kwargs.get("pattern", "*.log.*")
        days = kwargs.get("older_than_days", 7)

        # 仅做 dry-run 与 ls 列表，让用户确认后再执行
        # 安全起见，本工具仅返回将被删除的文件列表，不直接 rm
        list_cmd = ["find", path, "-name", pattern, "-mtime", f"+{days}", "-type", "f"]
        try:
            result = await run_cmd(list_cmd, timeout=60)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        files = [f for f in result.stdout.splitlines() if f.strip()]

        return ToolResult(
            success=True,
            output={
                "dry_run": True,
                "matched": len(files),
                "files": files[:100],
                "note": "实际删除需在二次确认后执行",
            },
        )


def _humanize(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f}PB"