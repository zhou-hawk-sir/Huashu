"""Shell 命令 → 专用 Tool 的重定向映射。

按建议方案「Tool 优先，Shell 兜底」：
凡是专用 Tool 已覆盖的命令，shell.execute 一律拒绝并提示改走 Tool。

映射表从 config/shell_redirect.json 加载（方便扩展），
fallback 到内置默认值。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from utils.logger import get_logger

logger = get_logger("tools.redirect")


def _load_redirect_map() -> Dict[str, Dict[str, str]]:
    """从 config/shell_redirect.json 加载映射表，失败则用内置默认值。"""
    # 内置默认值
    default: Dict[str, Dict[str, str]] = {
        "ps": {"tool": "process.list", "reason": "查看进程列表请用 process.list"},
        "kill": {"tool": "process.kill", "reason": "杀进程请用 process.kill"},
        "du": {"tool": "disk.find_large", "reason": "查找大文件/目录请用 disk.find_large"},
        "df": {"tool": "disk.find_large", "reason": "查看磁盘空间请用 disk.find_large"},
        "cat": {"tool": "file.read", "reason": "读文件请用 file.read"},
        "rm": {"tool": "file.remove", "reason": "删除请用 file.remove（recursive=true 递归删除）"},
        "systemctl": {"tool": "service.restart", "reason": "服务操作请用 service.status / service.restart"},
        "journalctl": {"tool": "log.journal", "reason": "查日志请用 log.journal"},
    }
    try:
        path = Path(__file__).parent.parent / "config" / "shell_redirect.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 合并：json 覆盖默认值
                merged = dict(default)
                merged.update(data)
                logger.info(f"loaded {len(merged)} redirect rules from {path}")
                return merged
    except Exception as e:
        logger.warning(f"load shell_redirect.json failed: {e}, using defaults")
    return default


SHELL_TOOL_REDIRECT = _load_redirect_map()


def check_redirect(cmd: str) -> Optional[Dict[str, str]]:
    """检查 shell 命令是否被专用 Tool 覆盖。

    Args:
        cmd: shell 命令字符串（可能是 "ps aux" / "  kill  1234 " 等）

    Returns:
        None: 命令未被专用 Tool 覆盖，放行
        Dict: 含 {"tool", "reason"}，调用方应拒绝并提示改走 Tool
    """
    if not cmd or not cmd.strip():
        return None
    # 提取第一个非空白 token（命令名）
    # 用简单的 split：不去 shlex，因为前导可能有 env / sudo 等
    tokens = cmd.strip().split()
    if not tokens:
        return None

    # 跳过 env / sudo 前缀（这些都是包装层）
    first = tokens[0]
    if first == "env":
        # env KEY=VAL cmd args... → 真正的命令是第二个 token 之后第一个非 KEY=VAL 的
        for tok in tokens[1:]:
            if "=" not in tok:
                first = tok
                break
        else:
            return None
    elif first == "sudo" or first == "su" or first == "doas" or first == "pkexec":
        # shell.execute 默认禁止 sudo；policy engine 会拦，这里不重复
        return None

    # 取 basename（去掉 /usr/bin/ 等绝对路径）
    base = first.rsplit("/", 1)[-1]

    return SHELL_TOOL_REDIRECT.get(base)
