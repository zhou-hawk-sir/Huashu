"""权限切换工具函数（已清空）

历史遗留：
- `switch_user`：原本用 `os.setuid` 切换用户。但 setuid 在 Linux 上不可逆，
  且与新模型（"Agent = 启动它的用户 + sudo -n 提权"）冲突。
  特权操作应放在**子进程**（`sudo -n -u`）而非 agent 自身进程。
- `deny_root_guard`：装饰器从无调用方，已删除。

新模型下，文件由 `tools/_runner.py:run_cmd(user=...)` 走 `sudo -n`。
如需"切换到 X 用户跑某个命令"，用：
    run_cmd(argv, user="<X>")
如需"提权 root 跑"，用：
    run_cmd(argv, user="root")   # policy allow_root_for_tools 控制
"""
from __future__ import annotations

from utils.logger import get_logger

logger = get_logger("execution.privilege")
