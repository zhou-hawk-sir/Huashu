"""Linux bwrap 沙箱实现

基于 bubblewrap (bwrap) 的轻量级 Linux 命名空间隔离。
在麒麟 V11 (LoongArch) 上可用。

集成到现有的 _runner.run_cmd() 中：
- 高危工具（shell.execute）默认启用沙箱
- 低危只读工具跳过沙箱
- 通过 SandboxHint 配置不同级别
"""
from __future__ import annotations

import asyncio
import platform
import shutil
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .sandbox import SandboxHint


@dataclass
class SandboxProfile:
    """沙箱配置文件（对应 sandbox.yaml）"""
    name: str = "default"
    read_only_paths: Tuple[str, ...] = ("/etc", "/boot", "/sys", "/proc")
    tmpfs_paths: Tuple[str, ...] = ("/tmp", "/run")
    writable_paths: Tuple[str, ...] = ()
    network: bool = False  # False = 禁用网络
    max_procs: int = 512
    max_memory_mb: int = 1024
    timeout: int = 30
    seccomp: Optional[str] = None


# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_PROFILES = {
    "default": SandboxProfile(
        name="default",
        read_only_paths=("/etc", "/boot", "/sys", "/proc"),
        tmpfs_paths=("/tmp", "/run"),
        writable_paths=(),
        network=False,
        max_procs=512,
        max_memory_mb=1024,
        timeout=30,
    ),
    "shell_execute": SandboxProfile(
        name="shell_execute",
        read_only_paths=("/etc", "/boot", "/sys", "/proc"),
        tmpfs_paths=("/tmp", "/run"),
        writable_paths=(),
        network=False,
        max_procs=256,
        max_memory_mb=512,
        timeout=15,
    ),
    "read_only": SandboxProfile(
        name="read_only",
        read_only_paths=("/etc", "/boot", "/sys", "/proc", "/tmp", "/run"),
        tmpfs_paths=(),
        writable_paths=(),
        network=False,
        max_procs=128,
        max_memory_mb=256,
        timeout=10,
    ),
}


# ---------------------------------------------------------------------------
# bwrap 沙箱
# ---------------------------------------------------------------------------

class BubblewrapSandbox:
    """
    Bubblewrap Linux 沙箱。

    用法：
        sandbox = BubblewrapSandbox(profile=DEFAULT_PROFILES["shell_execute"])
        result = await sandbox.run(["df", "-h"], user="nobody")
    """

    def __init__(
        self,
        profile: Optional[SandboxProfile] = None,
        hints: Optional[SandboxHint] = None,
    ) -> None:
        if profile is not None:
            self.profile = profile
        elif hints is not None:
            # 从 SandboxHint 转换
            self.profile = self._from_hints(hints)
        else:
            self.profile = DEFAULT_PROFILES["default"]

    def _from_hints(self, hints: SandboxHint) -> SandboxProfile:
        return SandboxProfile(
            name="from_hints",
            read_only_paths=hints.read_only_paths,
            tmpfs_paths=hints.tmpfs_paths,
            network=(hints.network == "host"),
            timeout=hints.timeout,
        )

    def _check_bwrap_available(self) -> bool:
        """检测 bwrap 是否可用"""
        return shutil.which("bwrap") is not None

    def _build_bwrap_args(self, cmd: List[str]) -> List[str]:
        """构建 bwrap 命令行参数"""
        p = self.profile
        args = ["bwrap"]

        # 隔离命名空间
        args += [
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
        ]
        if not p.network:
            args.append("--unshare-net")

        # 只读绑定
        for path in p.read_only_paths:
            args += ["--ro-bind", path, path]

        # tmpfs 挂载
        for path in p.tmpfs_paths:
            args += ["--tmpfs", path]

        # 可写目录绑定
        for path in p.writable_paths:
            args += ["--bind", path, path]

        # procfs（最小化）
        args += ["--proc", "/proc"]

        # dev（最小化）
        args += ["--dev", "/dev/null"]

        # 工作目录
        args += ["--chdir", "/"]

        # 资源限制
        args += ["--ulimit", f"nproc={p.max_procs}"]

        # seccomp（可选）
        if p.seccomp:
            args += ["--seccomp", p.seccomp]

        # 死亡时父进程也退出
        args += ["--die-with-parent"]

        # 新 session
        args += ["--new-session"]

        # 命令
        args += ["--"]
        args += cmd

        return args

    async def run(
        self,
        cmd: List[str],
        user: Optional[str] = None,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
    ) -> "ExecResult":
        """
        在 bwrap 沙箱中运行命令。

        如果 bwrap 不可用（非 Linux 或未安装），fallback 到普通 run_cmd。
        """
        from tools._runner import ExecResult
        import time

        start = time.time()
        timeout = timeout or self.profile.timeout

        # 检测是否可用
        if platform.system() != "Linux" or not self._check_bwrap_available():
            # 非 Linux 或无 bwrap：fallback 到普通执行
            from tools._runner import run_cmd as plain_run_cmd
            return await plain_run_cmd(cmd, user=user, timeout=timeout, cwd=cwd)

        bwrap_args = self._build_bwrap_args(cmd)

        # 如果指定了 user，包装 sudo
        # root 通过 sudo -n 提权时不再拦截
        if user == "root":
            bwrap_args = ["sudo", "-n"] + bwrap_args
        elif user:
            bwrap_args = ["sudo", "-n", "-u", user] + bwrap_args

        try:
            proc = await asyncio.create_subprocess_exec(
                *bwrap_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            duration = int((time.time() - start) * 1000)
            return ExecResult(
                returncode=proc.returncode or 0,
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                duration_ms=duration,
            )
        except FileNotFoundError:
            # bwrap 不可用：fallback
            from tools._runner import run_cmd as plain_run_cmd
            return await plain_run_cmd(cmd, user=user, timeout=timeout, cwd=cwd)


# ---------------------------------------------------------------------------
# 沙箱工厂
# ---------------------------------------------------------------------------

def create_sandbox(
    profile_name: str = "shell_execute",
) -> BubblewrapSandbox:
    """根据 profile 名称创建沙箱实例"""
    profile = DEFAULT_PROFILES.get(profile_name, DEFAULT_PROFILES["default"])
    return BubblewrapSandbox(profile=profile)


def create_sandbox_from_config(config: dict) -> BubblewrapSandbox:
    """从配置字典创建沙箱（用于 sandbox.yaml 加载）"""
    profile = SandboxProfile(
        name=config.get("name", "custom"),
        read_only_paths=tuple(config.get("read_only_paths", [])),
        tmpfs_paths=tuple(config.get("tmpfs_paths", [])),
        writable_paths=tuple(config.get("writable_paths", [])),
        network=config.get("network", False),
        max_procs=config.get("max_procs", 512),
        max_memory_mb=config.get("max_memory_mb", 1024),
        timeout=config.get("timeout", 30),
        seccomp=config.get("seccomp"),
    )
    return BubblewrapSandbox(profile=profile)
