"""系统信息工具（system.*）

读取本机硬件/系统信息（麒麟 V11-loongarch 友好）:
- system.distro:  /etc/os-release 或 /etc/lsb-release
- system.kernel:  platform.release / version / system
- system.cpu:     /proc/cpuinfo 或 platform.processor
- system.memory:  /proc/meminfo
- system.network_interfaces: /sys/class/net
- system.disks:   df -h（Windows 优雅降级）

注：本文件工具采用 **同步 run()**（与其他工具的 async run 不同），
因为只读硬件探测是轻量且无 IO 阻塞——同步更便于测试与上层聚合。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import RiskLevel, Tool, ToolResult


def _read_first(path: Path, default: str = "") -> str:
    """读文件全文（去除首尾空白），失败返回 default。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return default


def _parse_os_release(text: str) -> Dict[str, str]:
    """解析 /etc/os-release 的 KEY=VALUE 格式。"""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


def _windows_memory_mb() -> tuple[int, int]:
    """Windows 兜底：通过 ctypes 调用 GlobalMemoryStatusEx。返回 (total_mb, avail_mb)。"""
    if platform.system() != "Windows":
        return (0, 0)
    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)) == 0:
            return (0, 0)
        total_mb = int(stat.ullTotalPhys // (1024 * 1024))
        avail_mb = int(stat.ullAvailPhys // (1024 * 1024))
        return (total_mb, avail_mb)
    except Exception:
        return (0, 0)


class SystemDistroTool(Tool):
    """读取发行版信息（/etc/os-release → PRETTY_NAME）。"""
    name = "system.distro"
    description = "读取本机发行版信息（/etc/os-release 或 /etc/lsb-release）"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        distro = "unknown"
        # 1) /etc/os-release（systemd / 麒麟 / 现代发行版通用）
        os_release = Path("/etc/os-release")
        if os_release.exists():
            data = _parse_os_release(_read_first(os_release))
            distro = data.get("PRETTY_NAME") or data.get("NAME") or distro
        else:
            # 2) /etc/lsb-release（旧版 Ubuntu/Debian/麒麟老版本）
            lsb = Path("/etc/lsb-release")
            if lsb.exists():
                data = _parse_os_release(_read_first(lsb))
                distro = data.get("DISTRIB_DESCRIPTION") or data.get("DISTRIB_ID") or distro
        return {
            "arch": platform.machine(),
            "kernel": platform.release(),
            "distro": distro,
        }


class SystemKernelTool(Tool):
    """读取内核信息（platform.release/version/system）。"""
    name = "system.kernel"
    description = "读取本机内核版本（uname 等价信息）"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        return {
            "kernel": platform.release(),
            "kernel_version": platform.version(),
            "system": platform.system(),
        }


class SystemCpuTool(Tool):
    """读取 CPU 信息（/proc/cpuinfo → model name）。"""
    name = "system.cpu"
    description = "读取 CPU 型号与核心数"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        model = ""
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            text = _read_first(cpuinfo)
            for line in text.splitlines():
                if line.lower().startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
                if line.lower().startswith("processor"):
                    # LoongArch / ARM 常见：model name 缺失，Processor 行带型号
                    val = line.split(":", 1)[1].strip()
                    if val and not model:
                        model = val
        if not model:
            # 兜底：Windows / macOS / 异常环境
            model = platform.processor() or "unknown"

        cores = os.cpu_count() or 1
        return {
            "model": model,
            "cores": cores,
            "arch": platform.machine(),
        }


class SystemMemoryTool(Tool):
    """读取内存信息（/proc/meminfo → total / available MB）。

    注意：与既有 ``MemoryInfoTool``（name=system.memory，跑 free -h）不同，
    本工具直接解析 /proc/meminfo，更轻量、纯读取，无 shell 子进程。
    """
    name = "system.meminfo"
    description = "读取本机内存总量与可用量（MB，直接解析 /proc/meminfo）"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        total_kb = 0
        avail_kb = 0
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in _read_first(meminfo).splitlines():
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        total_kb = int(parts[1])
                elif line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        avail_kb = int(parts[1])
        if total_kb == 0:
            # Windows / 无 /proc 场景：用 ctypes 走 GlobalMemoryStatusEx
            win_total, win_avail = _windows_memory_mb()
            if win_total > 0:
                return {
                    "total_mb": win_total,
                    "available_mb": win_avail,
                    "used_mb": max(0, win_total - win_avail),
                }
            # 兜底：真拿不到就 0（不是错误）
            return {"total_mb": 0, "available_mb": 0, "used_mb": 0}
        total_mb = total_kb // 1024
        avail_mb = avail_kb // 1024
        used_mb = max(0, total_mb - avail_mb)
        return {"total_mb": total_mb, "available_mb": avail_mb, "used_mb": used_mb}


class SystemNetworkInterfacesTool(Tool):
    """列出本机网络接口（/sys/class/net，排除 lo）。"""
    name = "system.network_interfaces"
    description = "列出本机非回环网络接口"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        net_dir = Path("/sys/class/net")
        ifs: List[str] = []
        if net_dir.exists():
            for entry in net_dir.iterdir():
                name = entry.name
                if name == "lo":
                    continue
                ifs.append(name)
        return {"interfaces": ifs}


class SystemDisksTool(Tool):
    """读取磁盘使用（df -h）。"""
    name = "system.disks"
    description = "列出本机磁盘使用（df -h）"
    risk_level = RiskLevel.LOW
    required_capability = "system.read"
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        df_bin = shutil.which("df")
        if df_bin is None:
            return {"disks": [], "note": "df not available"}
        try:
            proc = subprocess.run(
                [df_bin, "-h"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return {"disks": [], "note": "df timeout / error"}

        if proc.returncode != 0:
            return {"disks": [], "note": f"df failed: {proc.stderr.strip()}"}

        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            return {"disks": []}

        header = lines[0].split()
        rows = []
        for ln in lines[1:]:
            parts = ln.split()
            if len(parts) < len(header):
                # df 输出含换行的挂载点，跳过
                continue
            row = dict(zip(header, parts))
            rows.append(row)
        return {"disks": rows}


# ============================================================
# host-status 轻量指标：cpu_pct / disk_pct（无 psutil 依赖）
# ============================================================


def _read_cpu_pct() -> Optional[float]:
    """读取 /proc/stat，返回瞬时 CPU 使用率 (100 - idle/total*100)。

    Linux 专用；Windows 无 /proc/stat → None。任何异常都吞掉返回 None。
    """
    try:
        text = Path("/proc/stat").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            nums = [int(x) for x in parts[1:] if x.lstrip("-").isdigit()]
            if len(nums) < 4:
                return None
            total = sum(nums)
            idle = nums[3]  # idle jiffies
            if total <= 0:
                return None
            return round(100.0 * (1.0 - idle / total), 1)
    return None


def _read_root_disk_pct() -> Optional[int]:
    """读取根文件系统 (mount == '/') 的使用率百分比（整数）。

    通过 `df -h /` 解析末列挂载点为 '/' 的行；Windows / df 不可用 → None。
    """
    df_bin = shutil.which("df") or "/bin/df"
    try:
        r = subprocess.run(
            [df_bin, "-h", "/"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[-1] == "/":
            try:
                return int(parts[4].rstrip("%"))
            except ValueError:
                return None
    return None


# 注册（供 tools/__init__.py import）
__all__ = [
    "SystemDistroTool",
    "SystemKernelTool",
    "SystemCpuTool",
    "SystemMemoryTool",
    "SystemNetworkInterfacesTool",
    "SystemDisksTool",
    "_read_cpu_pct",
    "_read_root_disk_pct",
]
