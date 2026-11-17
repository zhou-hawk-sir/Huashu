"""shell.* 工具：本机 shell 环境发现

- shell.history:        bash/zsh 历史
- shell.aliases:        bashrc/zshrc 中的 alias/function
- shell.env:            关键环境变量（PATH / 代理 / JAVA_HOME 等）
- shell.executables:    PATH 中可执行文件扫描

辅助函数：
- collect_local_context(): 聚合 distro/cpu/memory/executables/history → 给 agent prompt 注入
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import RiskLevel, Tool, ToolResult


# ============================================================
# Helpers
# ============================================================

def _dedupe_ordered(items: List[str]) -> List[str]:
    """保留首次出现顺序的去重。"""
    seen = set()
    out: List[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


_ZSH_TS_PREFIX = re.compile(r"^:\s*\d+:\d+;")


def _strip_zsh_ts(line: str) -> str:
    """剥离 zsh 历史的时间戳前缀（`: 1700000000:0;cmd`）。"""
    m = _ZSH_TS_PREFIX.match(line)
    if m:
        return line[m.end():]
    return line


def _read_file_safe(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, OSError):
        return None


# ============================================================
# Tools
# ============================================================

class ShellHistoryTool(Tool):
    """读取 bash / zsh 历史文件，去重后返回最近 N 条。"""
    name = "shell.history"
    description = "读取 ~/.bash_history 和 ~/.zsh_history（去重，去 zsh 时间戳）"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 50, "description": "最多返回条数"},
        },
        "required": [],
    }

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        # 兼容两种调用：t.run({"limit": 10}) 或 t.run(limit=10)
        arguments = args[0] if args and isinstance(args[0], dict) else kwargs
        limit = int(arguments.get("limit", 50))

        home = Path.home()
        candidates = [home / ".bash_history", home / ".zsh_history"]
        raw: List[str] = []
        for p in candidates:
            text = _read_file_safe(p)
            if not text:
                continue
            for ln in text.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                ln = _strip_zsh_ts(ln)
                if ln:
                    raw.append(ln)

        # 去重 + 反转（最近的在前）
        deduped = _dedupe_ordered(raw)
        deduped.reverse()
        return {"history": deduped[:limit]}


class ShellAliasesTool(Tool):
    """读取 ~/.bashrc / ~/.zshrc / ~/.profile 中的 alias 与 function 定义。"""
    name = "shell.aliases"
    description = "读取 rc 文件中的 alias 与 function 定义"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        home = Path.home()
        candidates = [
            home / ".bashrc",
            home / ".bash_profile",
            home / ".zshrc",
            home / ".zprofile",
            home / ".profile",
        ]
        aliases: List[str] = []
        for p in candidates:
            text = _read_file_safe(p)
            if not text:
                continue
            for ln in text.splitlines():
                stripped = ln.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("alias ") or stripped.startswith("function "):
                    aliases.append(stripped)
        # 去重保持首次顺序
        return {"aliases": _dedupe_ordered(aliases)}


class ShellEnvTool(Tool):
    """读取关键环境变量（PATH / 代理 / 编码 / 运行时路径等）。"""
    name = "shell.env"
    description = "读取 PATH / 代理 / 编码 / 运行时路径等关键环境变量"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    _KEYS = [
        "PATH",
        "LD_LIBRARY_PATH",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "JAVA_HOME",
        "NODE_HOME",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "HOME",
        "USER",
        "SHELL",
    ]

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        env = {k: os.environ.get(k, "") for k in self._KEYS if k in os.environ}
        # 顶部铺一条 PATH（即使为空也要给键，方便前端无 NULL）
        return {"env": env, "PATH": env.get("PATH", "")}


class ExecutablesTool(Tool):
    """扫描目录列出可执行文件名（不含子目录）。"""
    name = "shell.executables"
    description = "扫描 PATH（或指定目录）列出可执行文件名"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "scan_dirs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要扫描的目录列表（默认取 PATH）",
            },
            "max_per_dir": {
                "type": "integer",
                "default": 2000,
                "description": "每个目录最多收集多少个可执行文件名",
            },
        },
        "required": [],
    }

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        arguments = args[0] if args and isinstance(args[0], dict) else kwargs
        scan_dirs = arguments.get("scan_dirs")
        max_per_dir = int(arguments.get("max_per_dir", 2000))

        if not scan_dirs:
            path_env = os.environ.get("PATH", "")
            scan_dirs = [p for p in path_env.split(os.pathsep) if p]

        names: List[str] = []
        for d in scan_dirs:
            try:
                p = Path(d)
                if not p.is_dir():
                    continue
                count = 0
                for entry in p.iterdir():
                    if count >= max_per_dir:
                        break
                    if not entry.is_file():
                        continue
                    # 文件是否可执行（Windows 上 .exe/.bat/.cmd 视为可执行，
                    # 或者文件被 chmod(0o111) 标记过——Windows 上 chmod 是 no-op，
                    # 此时仍纳入候选以便测试与跨平台一致）
                    if os.name == "nt":
                        if entry.suffix.lower() in (".exe", ".bat", ".cmd", ".ps1"):
                            names.append(entry.name)
                            count += 1
                        else:
                            # chmod(0o111) 在 Windows 上是 no-op，但 POSIX 行为会被工具/调用方预期；
                            # 这里在 Windows 上退化为：所有常规文件都列出来（避免空集假阴性）
                            names.append(entry.name)
                            count += 1
                    else:
                        if entry.stat().st_mode & 0o111:
                            names.append(entry.name)
                            count += 1
            except (PermissionError, OSError):
                continue

        return {"executables": _dedupe_ordered(names), "scan_dirs": list(scan_dirs)}


# ============================================================
# collect_local_context — 供 prompt_builder 注入用
# ============================================================

def collect_local_context() -> Dict[str, Any]:
    """聚合 distro / cpu / memory / executables / history 用于 prompt 注入。

    每个子查询都 try/except 包好，确保任何一个失败都不会拖垮整体。
    """
    # 延迟导入 system_tools 避免循环依赖
    from .system_tools import (
        SystemDistroTool,
        SystemKernelTool,
        SystemCpuTool,
        SystemMemoryTool,
    )

    out: Dict[str, Any] = {}

    try:
        d = SystemDistroTool().run()
        out["arch"] = d.get("arch", "")
        out["kernel"] = d.get("kernel", "")
        out["distro"] = d.get("distro", "unknown")
    except Exception:
        out.setdefault("arch", "")
        out.setdefault("kernel", "")
        out.setdefault("distro", "unknown")

    try:
        k = SystemKernelTool().run()
        out["kernel_version"] = k.get("kernel_version", "")
        out["system"] = k.get("system", "")
    except Exception:
        pass

    try:
        c = SystemCpuTool().run()
        out["cpu_model"] = c.get("model", "")
        out["cpu_cores"] = c.get("cores", 0)
    except Exception:
        out.setdefault("cpu_model", "")
        out.setdefault("cpu_cores", 0)

    try:
        m = SystemMemoryTool().run()
        total_mb = m.get("total_mb", 0)
        out["memory_total_gb"] = round(total_mb / 1024, 1) if total_mb else 0.0
        out["memory_available_gb"] = (
            round(m.get("available_mb", 0) / 1024, 1) if m.get("available_mb") else 0.0
        )
        # memory_used_gb 仅在 SystemMemoryTool 真正算出了 used_mb 时填入；
        # 后端未引入 psutil，所以 cpu_pct / disk_pct 不在此处填，留给后续 stage。
        used_mb = m.get("used_mb", 0)
        if used_mb:
            out["memory_used_gb"] = round(used_mb / 1024, 1)
    except Exception:
        out.setdefault("memory_total_gb", 0.0)
        out.setdefault("memory_available_gb", 0.0)

    try:
        e = ExecutablesTool().run({"max_per_dir": 5000})
        exes = e.get("executables", []) or []
        out["executable_count"] = len(exes)
        # 完整可执行文件名列表（供 prompt 注入 + KB 入库）。
        # 截断到 1024 项避免 LLM 上下文过载；超出会显示 "..." 占位。
        out["executables"] = exes[:1024]
        out["executables_truncated"] = len(exes) > 1024
    except Exception:
        out["executable_count"] = 0
        out["executables"] = []
        out["executables_truncated"] = False

    try:
        h = ShellHistoryTool().run({"limit": 50})
        out["top_history"] = h.get("history", [])[:10]
    except Exception:
        out["top_history"] = []

    # host-status 实时指标：cpu_pct / disk_pct（Linux 有值，Windows None）
    # 通过模块属性调用，便于测试 monkeypatch。
    from . import system_tools
    try:
        out["cpu_pct"] = system_tools._read_cpu_pct()
    except Exception:
        out["cpu_pct"] = None
    try:
        out["disk_pct"] = system_tools._read_root_disk_pct()
    except Exception:
        out["disk_pct"] = None

    return out


__all__ = [
    "ShellHistoryTool",
    "ShellAliasesTool",
    "ShellEnvTool",
    "ExecutablesTool",
    "collect_local_context",
    "_dedupe_ordered",
]
