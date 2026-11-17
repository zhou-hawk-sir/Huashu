"""环境指纹：采集、归一化、四维匹配

设计要点：
1. 采集与解析分离。采集依赖系统命令，解析与匹配是纯函数，可在任意平台测试。
2. 匹配返回分级结果而非布尔值。版本差异有轻重之分，一刀切的「兼容/不兼容」
   无法支撑后续分级降权。
3. 不依赖第三方库。os-release 与 uname 都是纯文本解析。
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from utils.logger import get_logger

from .types import (
    MATCH_EXACT,
    MATCH_MAJOR,
    MATCH_MINOR,
    MATCH_NONE,
    EnvFingerprint,
)

logger = get_logger("memory.fingerprint")

OS_RELEASE_PATHS = (Path("/etc/os-release"), Path("/usr/lib/os-release"))

# 关键依赖默认探测项。可按部署实际增删。
DEFAULT_DEPS = ("nginx", "python3", "sqlite3", "bwrap")

_OS_ALIASES = {
    "kylin": "kylin",
    "neokylin": "kylin",
    "uos": "uos",
    "debian": "debian",
    "ubuntu": "ubuntu",
    "centos": "centos",
    "openeuler": "openeuler",
    "fedora": "fedora",
}


# ── 采集 ──────────────────────────────────────────────────────────────
def _run(cmd: list[str], timeout: int = 5) -> str:
    """执行只读探测命令。失败返回空串，绝不抛异常打断主流程。"""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("探测命令失败 %s: %s", cmd, exc)
        return ""


def read_os_release(path: Optional[Path] = None) -> Dict[str, str]:
    """解析 os-release。返回键值字典，缺失字段为空串。"""
    candidates = (path,) if path else OS_RELEASE_PATHS
    for p in candidates:
        if not p or not p.exists():
            continue
        data: Dict[str, str] = {}
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                data[key.strip()] = val.strip().strip('"').strip("'")
        except OSError as exc:
            logger.warning("读取 %s 失败: %s", p, exc)
            continue
        return data
    return {}


def parse_os(raw: Dict[str, str]) -> Tuple[str, str, str]:
    """从 os-release 抽取 (os_id, os_version, os_sp)。

    麒麟的 SP 号只在 PRETTY_NAME 或 VERSION 里出现，需要正则捞。
    版本号统一小写，避免 V11 与 v11 被判成不同环境。
    """
    os_id = (raw.get("ID") or "").strip().lower()
    os_id = _OS_ALIASES.get(os_id, os_id)

    version = (raw.get("VERSION_ID") or "").strip().lower()
    if not version:
        m = re.search(r"(v?\d+(?:\.\d+)*)", raw.get("VERSION") or "")
        version = m.group(1).lower() if m else ""

    text = " ".join(
        filter(None, [raw.get("PRETTY_NAME", ""), raw.get("VERSION", ""), raw.get("VERSION_ID", "")])
    )
    m = re.search(r"\bSP\s*(\d+)\b", text, re.IGNORECASE)
    sp = f"sp{m.group(1)}" if m else ""

    return os_id or "unknown", version or "unknown", sp


def probe_kernel_arch() -> Tuple[str, str]:
    """采集内核版本与 CPU 架构。"""
    kernel = _run(["uname", "-r"]) or platform.release() or "unknown"
    arch = _run(["uname", "-m"]) or platform.machine() or "unknown"
    return kernel.strip(), arch.strip()


def probe_deps(names=DEFAULT_DEPS) -> Dict[str, str]:
    """探测关键依赖版本。rpm 系与 deb 系都尝试，取到即止。"""
    deps: Dict[str, str] = {}
    have_rpm = shutil.which("rpm") is not None
    have_dpkg = shutil.which("dpkg-query") is not None

    for name in names:
        ver = ""
        if have_rpm:
            out = _run(["rpm", "-q", "--qf", "%{VERSION}", name])
            if out and "not installed" not in out.lower():
                ver = out
        if not ver and have_dpkg:
            out = _run(["dpkg-query", "-W", "-f=${Version}", name])
            if out and "no packages found" not in out.lower():
                ver = out
        if not ver:
            path = shutil.which(name)
            if path:
                ver = "present"
        if ver:
            deps[name] = ver
    return deps


def capture(deps=DEFAULT_DEPS, with_deps: bool = True) -> EnvFingerprint:
    """采集当前机器环境。任一环节失败降级为 unknown，不中断。"""
    raw = read_os_release()
    os_id, os_version, os_sp = parse_os(raw)
    kernel, arch = probe_kernel_arch()
    return EnvFingerprint(
        os_id=os_id,
        os_version=os_version,
        os_sp=os_sp,
        kernel=kernel,
        arch=arch,
        deps=probe_deps(deps) if with_deps else {},
        captured_at=_now_iso(),
        verify_result="unknown",
    )


def normalize(raw: Dict[str, object]) -> EnvFingerprint:
    """把散装字典归一成指纹。供测试与外部导入使用。"""
    return EnvFingerprint(
        os_id=str(raw.get("os_id") or raw.get("ID") or "unknown").lower(),
        os_version=str(raw.get("os_version") or raw.get("VERSION_ID") or "unknown").lower(),
        os_sp=str(raw.get("os_sp") or "").lower(),
        kernel=str(raw.get("kernel") or "unknown"),
        arch=str(raw.get("arch") or "unknown").lower(),
        deps=dict(raw.get("deps") or {}),  # type: ignore[arg-type]
        captured_at=str(raw.get("captured_at") or _now_iso()),
        verify_result=str(raw.get("verify_result") or "unknown"),
    )


# ── 匹配 ──────────────────────────────────────────────────────────────
def _kernel_parts(kernel: str) -> Tuple[str, str]:
    """内核版本拆成 (主, 次)。4.19.90-25.10 → ("4.19", "90")。"""
    m = re.match(r"^(\d+\.\d+)\.(\d+)", kernel or "")
    if m:
        return m.group(1), m.group(2)
    return kernel or "unknown", ""


def _match_os(exp: EnvFingerprint, cur: EnvFingerprint) -> str:
    if exp.os_id != cur.os_id:
        return MATCH_NONE
    if exp.os_version != cur.os_version:
        return MATCH_MAJOR
    if exp.os_sp != cur.os_sp:
        return MATCH_MINOR
    return MATCH_EXACT


def _match_arch(exp: EnvFingerprint, cur: EnvFingerprint) -> str:
    return MATCH_EXACT if exp.arch == cur.arch else MATCH_NONE


def _match_kernel(exp: EnvFingerprint, cur: EnvFingerprint) -> str:
    emaj, emin = _kernel_parts(exp.kernel)
    cmaj, cmin = _kernel_parts(cur.kernel)
    if emaj != cmaj:
        return MATCH_MAJOR
    if emin != cmin:
        return MATCH_MINOR
    return MATCH_EXACT


def _match_deps(exp: EnvFingerprint, cur: EnvFingerprint) -> str:
    """依赖匹配。经验依赖的包在当前环境缺失，视为不适用。"""
    if not exp.deps:
        return MATCH_EXACT
    level = MATCH_EXACT
    for name, want in exp.deps.items():
        got = cur.deps.get(name)
        if got is None:
            return MATCH_NONE
        if got != want:
            level = MATCH_MINOR
    return level


def match(exp: EnvFingerprint, cur: EnvFingerprint) -> Dict[str, str]:
    """四维匹配等级：os / arch / kernel / deps。

    返回值为 exact | minor | major | none，供分级降权使用。
    """
    return {
        "os": _match_os(exp, cur),
        "arch": _match_arch(exp, cur),
        "kernel": _match_kernel(exp, cur),
        "deps": _match_deps(exp, cur),
    }


def describe(levels: Dict[str, str]) -> str:
    """把匹配结果转成可读中文，用于日志、注入块与界面提示。"""
    labels = {"os": "操作系统", "arch": "CPU 架构", "kernel": "内核", "deps": "关键依赖"}
    detail = {"exact": "完全匹配", "minor": "次版本差异", "major": "主版本差异", "none": "不匹配"}
    parts = [f"{labels[k]} {detail.get(v, v)}" for k, v in levels.items() if v != MATCH_EXACT]
    return "；".join(parts) if parts else "环境完全匹配"


def is_hard_incompatible(levels: Dict[str, str]) -> Tuple[bool, str]:
    """硬性适用性检查：架构或依赖不满足即排除，不进入打分。

    这是防止「语义相似度 0.95 + 历史成功率 100%」把不兼容经验顶到首位的关键一步。
    """
    if levels.get("arch") == MATCH_NONE:
        return True, "CPU 架构不兼容"
    if levels.get("deps") == MATCH_NONE:
        return True, "关键依赖缺失"
    return False, ""


def _now_iso() -> str:
    from .store import now_iso

    return now_iso()
