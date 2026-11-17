"""命令行 enforcement（仿 Claude Code settings.json 的 deny 模式）

工作流程：
1. 从 settings.yaml 的 `safety.tool_permissions` 段读出两类正则：
   - denied_commands：硬拒绝（命令直接失败）
   - allowed_commands：未来扩展（预留）
2. 对 shell.execute 的 `cmd` 字段做匹配
3. 唯一拦截：deny。命中即拒，否则一律放行（不弹窗、不二次确认）。

为方便集成，提供：
- is_command_denied(cmd) -> bool
- get_decision(cmd) -> Decision
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Decision:
    """对一条命令的 enforcement 决策。"""
    denied: bool = False
    reason: str = ""
    matched_rule: Optional[str] = None


_DEFAULT_DENY = [
    r"^\s*sudo\b",
    r"^\s*su\b",
    r"^\s*doas\b",
    r"^\s*pkexec\b",
    r"\|\s*(sudo|su|doas)\b",
    r"\$\((sudo|su|doas)\b",
    r"`(sudo|su|doas)\b",
]


class CommandEnforcer:
    """对单条 cmd 字符串做工具权限裁定（仅 deny / allow 两态）。

    只读 config，不写状态；可以放心全局共享一个实例。
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        cfg = cfg or {}
        # 直接从传入 dict 拿 tool_permissions（app.py 传的是 safety 段，tool_permissions 在其下）
        perms = cfg.get("tool_permissions") or {}
        self._enforce = bool(perms.get("enforce", True))
        self._deny_re = self._compile(perms.get("denied_commands", _DEFAULT_DENY))
        self._allow_re = self._compile(perms.get("allowed_commands", []))

    @staticmethod
    def _compile(patterns: List[str]) -> List[re.Pattern]:
        out: List[re.Pattern] = []
        for p in patterns or []:
            try:
                out.append(re.compile(p))
            except re.error:
                continue
        return out

    def decide(self, cmd: str) -> Decision:
        """对一条命令做最终决策。

        仅 deny：命中即拒，否则一律放行（无 confirm、无 ask）。
        """
        if not self._enforce:
            return Decision()

        cmd_clean = (cmd or "").strip()
        for p in self._deny_re:
            if p.search(cmd_clean):
                return Decision(
                    denied=True,
                    reason=f"匹配 denied 规则: {p.pattern}",
                    matched_rule=f"deny:{p.pattern}",
                )
        return Decision()


_enforcer_singleton: Optional[CommandEnforcer] = None


def get_enforcer(cfg: Optional[Dict[str, Any]] = None) -> CommandEnforcer:
    global _enforcer_singleton
    if _enforcer_singleton is None:
        # 单例未初始化时，从 get_config() 兜底拿 safety 段（避免漏掉 46 条真实 deny）
        if cfg is None:
            try:
                from utils.config import get_setting, reload_config
                cfg = get_setting("safety", {}) or {}
            except Exception:
                cfg = {}
        _enforcer_singleton = CommandEnforcer(cfg)
    return _enforcer_singleton


def reset_enforcer() -> None:
    global _enforcer_singleton
    _enforcer_singleton = None
