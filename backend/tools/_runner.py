"""工具执行辅助：通过 subprocess 执行 shell 命令

权限模型：
- **默认 = 启动 Agent 的当前用户**（不切 nobody、不切 opsagent）。
  Agent 的权限 = 启动它的用户的权限，由内核 DAC 自然放行/拒绝。
- **可选 user 参数**：显式以其他账户运行（通过 `sudo -n -u <user>`）。
- **root 提权**：仅在 `shell.execute_elevated` 这类显式高危工具里允许，
  通过 `sudo -n` 提权；调用方需确保 sudoers 已配 NOPASSWD。
- **沙箱感知**：可选包一层 `bwrap`（对应 `sandbox.enabled`）。

平台说明：
- Linux：通过 `sudo -n -u <user>` 切换账户（生产 V11 路径）
- Windows / macOS：跳过 sudo 直接运行（用于本地开发测试）
"""
from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int


async def run_cmd(
    cmd: List[str],
    user: Optional[str] = None,
    timeout: int = 30,
    cwd: Optional[str] = None,
) -> ExecResult:
    """异步执行命令，可指定运行用户（通过 sudo -u）。

    权限语义：
    - user=None（默认）：以启动 Agent 的当前用户跑（**不做 sudo 切换**）。
      Agent 自身的权限 = 用户权限；写 `/home/alice/x` 成功是自然的。
    - user="<some_user>"：通过 `sudo -n -u <user>` 切到该账户（需 NOPASSWD）。
    - user="root"：通过 `sudo -n` 提权到 root。**仅 elevated 工具可调**，
      普通工具不要传这个值（policy engine 已经会拦）。

    ★ 已精简：argv 数组模式（shlex.split 后）下，shell 重定向（> >> <）/管道（|）
    /错误重定向（2>）**不在 argv 里表达**。如果调用方传入的是字符串（带 | 2>
    > 等），需要切到 shell 模式（"bash", "-c", cmd_str）。本函数默认走 argv 列表；
    shell_tools.run() 检测到含 shell 元字符时传字符串，再走 bash -c 路径。
    """
    import platform

    is_posix = platform.system() == "Linux"

    if isinstance(cmd, str):
        # 字符串模式：交给 bash -c 解析（支持管道 / 重定向 / 命令替换）
        shell_prefix = []
        if user and is_posix:
            if user == "root":
                shell_prefix = ["sudo", "-n"]
            else:
                shell_prefix = ["sudo", "-n", "-u", user]
        full_cmd = [*shell_prefix, "bash", "-c", cmd]
    elif user and is_posix:
        if user == "root":
            full_cmd = ["sudo", "-n", *cmd]
        else:
            full_cmd = ["sudo", "-n", "-u", user, *cmd]
    else:
        full_cmd = cmd

    start = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Command not found: {cmd[0]}") from e


def safe_quote(s: str) -> str:
    """安全 shell 引号转义。"""
    return shlex.quote(s)