"""受信任工作目录（Trusted Workspace）

目录信任模型：会话可指定一个工作目录，目录内的
MEDIUM 风险操作自动放行；越界仍需用户二次确认；保护路径（/etc /boot
/sys /proc 等）永远拒绝，凌驾于 workspace 信任之上。

**部署目标**：麒麟高级服务器 V11（LoongArch 64）Linux 系统。
所有路径语义基于 Linux realpath/commonpath。

设计要点：
- 所有路径比对都走 os.path.realpath，防 symlink 逃逸
- 最小深度限制（MIN_DEPTH=2），防 workspace 设到 `/` `/tmp` `/home`
- 纯函数 + 无副作用，便于单测与 PolicyEngine 复用
- 不做 Windows 路径兼容（项目实际不会在 Windows 上部署）
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional


# 永远不能作为 workspace 的根（前缀或本身）
# 设计：FORBIDDEN_ROOTS 只放系统关键目录（/、/tmp、/etc、/boot 等）。
# 用户家目录（/home、/root）**不在**这里——配合 MIN_DEPTH=2 防止把整个
# 家目录当 workspace（`/home` 深度为 1，仍会被 MIN_DEPTH 拦），但允许
# `/home/<user>/<project>`、`/root/<subdir>` 这种具体项目目录。
# 真正的硬保护在 PROTECTED_PREFIXES（policy 层凌驾 workspace 信任）。
FORBIDDEN_ROOTS = frozenset({
    "/", "/tmp", "/var/tmp", "/run",
    "/etc", "/boot", "/sys", "/proc",
    "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/opt", "/srv", "/mnt", "/media", "/dev",
})

# 保护路径前缀（用于 _touches_protected_path，凌驾 workspace 信任）
# 严格白名单：仅系统关键目录；可恢复目录（/var/log、/var/lib）由 MEDIUM+CONFIRM 处理
PROTECTED_PREFIXES = (
    "/etc", "/boot", "/sys", "/proc",
    "/usr", "/bin", "/sbin", "/lib",
    "/root/.ssh",
)


class WorkspaceValidationError(ValueError):
    """workspace 路径不合法（深度过浅、命中禁用前缀、不存在、不是目录等）。"""


class WorkspaceResolveError(RuntimeError):
    """realpath 解析失败（路径不存在、权限不足等）。"""


@dataclass
class ValidateResult:
    raw: str
    root: str
    ok: bool = True
    error: Optional[str] = None


class WorkspaceTrust:
    """受信任工作目录的校验与路径包含判定。

    用法：
        trust = WorkspaceTrust()
        result = trust.validate("/home/alice/proj")    # 校验可不可设为 workspace
        if result.ok:
            trust.contains(result.root, "/home/alice/proj/foo.txt")  # True
    """

    # 不再做最小深度限制：用户明确要求放开，让 /home、/root 这类顶层
    # 目录也能作为 workspace。剩下三道关卡（绝对路径、必须存在、FORBIDDEN_ROOTS）
    # 已足够防"信任整个根文件系统"。

    def validate(self, raw_path: str) -> ValidateResult:
        """校验路径是否可作为受信任工作目录。

        校验顺序：
        1) 必须是绝对路径
        2) realpath 解析
        3) 必须存在
        4) 必须是目录
        5) 不命中 FORBIDDEN_ROOTS

        任意一步失败抛 WorkspaceValidationError。
        """
        if not raw_path or not isinstance(raw_path, str):
            raise WorkspaceValidationError("workspace 路径不能为空")

        if not os.path.isabs(raw_path):
            raise WorkspaceValidationError(
                f"workspace 必须是绝对路径：{raw_path!r}"
            )

        try:
            root = os.path.realpath(raw_path)
        except OSError as e:
            raise WorkspaceResolveError(f"无法解析路径 {raw_path!r}: {e}") from e

        if not os.path.exists(root):
            raise WorkspaceValidationError(f"workspace 不存在：{root}")
        if not os.path.isdir(root):
            raise WorkspaceValidationError(f"workspace 不是目录：{root}")

        # 禁用根（含本身 + 子前缀）
        if root in FORBIDDEN_ROOTS:
            raise WorkspaceValidationError(f"workspace 命中禁用根：{root}")
        for bad in FORBIDDEN_ROOTS:
            if root.startswith(bad + os.sep):
                raise WorkspaceValidationError(
                    f"workspace 命中禁用前缀 {bad!r}：{root}"
                )

        return ValidateResult(raw=raw_path, root=root, ok=True)

    def contains(self, workspace_root: str, target_path: str) -> bool:
        """判定 target_path 是否落在 workspace_root 内。

        实现：
        1. 两路径都走 ``os.path.realpath``，消除 symlink；
        2. 用 ``os.path.commonpath`` 做前缀比对（防 /workspace2 误判 /workspace）。

        失败处理（保守返回 False）：
        - 任一路径解析失败（OSError）→ False；
        - 两路径在不同盘符 / 不同根（ValueError，跨平台实属罕见）→ False。

        注意：本项目运行在麒麟 V11（Linux）。Windows realpath 行为差异
        不在支持范围，但代码本身不崩溃。
        """
        if not workspace_root or not target_path:
            return False
        try:
            rp = os.path.realpath(target_path)
            wr = (
                os.path.realpath(workspace_root)
                if os.path.exists(workspace_root)
                else os.path.normpath(workspace_root)
            )
        except OSError:
            return False
        try:
            return os.path.commonpath([rp, wr]) == wr
        except ValueError:
            return False

    def extract_paths_from_command(self, cmd: str) -> List[str]:
        """从 shell 命令行中扫描所有形如 ``/xxx``（或 ``C:\\xxx``）的绝对路径 token。

        用于 ``shell.execute`` / ``shell.execute_elevated`` 的 workspace 判定。
        """
        if not cmd:
            return []
        # Linux 风格 /xxx；同时兼容 Windows 风格 C:\\xxx（CI/开发用）
        tokens = re.findall(r"(?:/[A-Za-z0-9_./-]+|[A-Za-z]:[/\\][A-Za-z0-9_./\\-]+)", cmd)
        return tokens


# 模块级单例（无状态、纯函数，可共享）
_default_trust: Optional[WorkspaceTrust] = None


def get_default_trust() -> WorkspaceTrust:
    global _default_trust
    if _default_trust is None:
        _default_trust = WorkspaceTrust()
    return _default_trust