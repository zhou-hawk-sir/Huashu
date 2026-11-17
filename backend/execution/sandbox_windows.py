"""Windows / macOS 沙箱桩实现 — **NoOpRunner**

警告：此实现 **无任何隔离**。生产部署必须在 Linux + bubblewrap。
开发环境仅用于离线测试。

启动检查：若设置了环境变量 `OPSAGENT_REQUIRE_SANDBOX=1`，
本桩在构造时即 raise — 防止误以为有沙箱。
"""
from __future__ import annotations

import os
from typing import List, Optional

from tools._runner import ExecResult


class NoOpRunner:
    """
    非 Linux 平台的 **无隔离** 桩实现。

    行为：
    - 默认：直接透传到 run_cmd，**不**做任何隔离。
    - 启动时若 `OPSAGENT_REQUIRE_SANDBOX=1`，构造时 raise RuntimeError。
    - 每次 run() 调用，若 `OPSAGENT_REQUIRE_SANDBOX=1` 且 sandbox.enabled，
      同样 raise。

    设计意图：避免"开发环境跑通 → 上麒麟 VM 才有沙箱"的隐性配置 bug。
    """

    def __init__(self) -> None:
        if os.environ.get("OPSAGENT_REQUIRE_SANDBOX") == "1":
            raise RuntimeError(
                "NoOpRunner refused to start: OPSAGENT_REQUIRE_SANDBOX=1 "
                "but no real sandbox is available on this platform. "
                "Deploy to Linux + bubblewrap, or unset the env var for dev."
            )

    async def run(
        self,
        cmd: List[str],
        user: Optional[str] = None,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
    ) -> ExecResult:
        """透传到 run_cmd，无任何隔离。

        启动后再设 OPSAGENT_REQUIRE_SANDBOX=1 仍会立即拒跑。
        """
        if os.environ.get("OPSAGENT_REQUIRE_SANDBOX") == "1":
            raise RuntimeError(
                "NoOpRunner refusing to execute: sandbox required but unavailable. "
                f"cmd={cmd!r}"
            )
        from tools._runner import run_cmd as plain_run_cmd
        return await plain_run_cmd(cmd, user=user, timeout=timeout, cwd=cwd)
