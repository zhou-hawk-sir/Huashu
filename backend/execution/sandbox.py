"""沙箱接口（预留）

本轮仅提供接口与提示信息，不实现真实 cgroups/namespaces 隔离。
实际部署可在麒麟 V11 上配合 systemd-run / bwrap / unshare 实现。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SandboxHint:
    """沙箱提示信息（说明性，不做强制）。"""
    backend: str = "none"  # none | bwrap | systemd-run | unshare
    read_only_paths: tuple = ("/etc", "/boot", "/sys", "/proc")
    tmpfs_paths: tuple = ("/tmp", "/run")
    network: str = "off"  # off | host
    timeout: int = 30

    def describe(self) -> str:
        return (
            f"Sandbox[{self.backend}]: read_only={self.read_only_paths}, "
            f"tmpfs={self.tmpfs_paths}, net={self.network}, timeout={self.timeout}s"
        )