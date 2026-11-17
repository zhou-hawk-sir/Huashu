"""独立 hook 层（YAML 正则 + 短路）。

仿照 claude-code 的 PreToolUse hook。
- action=deny   → 'deny' (PolicyEngine 短路为 DENY)
- action=ask    → 'ask'  (PolicyEngine 短路为 CONFIRM)
- action=allow  → 'allow' (跳过 risk/permission，直接 ALLOW)
- action=log_only → 仅记录，继续评估下游
- 无命中 → None (放行给 RiskClassifier / PolicyEngine)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class HookAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    LOG_ONLY = "log_only"


@dataclass
class HookRule:
    id: str
    event: str = "PreToolUse"
    match_tool: str = "*"
    match_cmd_regex: Optional[str] = None
    match_args_keys: Optional[List[str]] = None
    action: HookAction = HookAction.ALLOW
    message: str = ""
    log_path: Optional[str] = None


def _parse_rule(raw: Dict[str, Any]) -> HookRule:
    return HookRule(
        id=raw["id"],
        event=raw.get("event", "PreToolUse"),
        match_tool=raw.get("match_tool", "*"),
        match_cmd_regex=raw.get("match_cmd_regex"),
        match_args_keys=raw.get("match_args_keys"),
        action=HookAction(raw["action"]),
        message=raw.get("message", ""),
        log_path=raw.get("log_path"),
    )


def load_hooks_from_yaml(path: str | Path) -> List[HookRule]:
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw_hooks = data.get("hooks", []) or []
    return [_parse_rule(r) for r in raw_hooks]


class HookEngine:
    def __init__(self, rules: List[HookRule]):
        self.rules = [r for r in rules if r.event == "PreToolUse"]

    def evaluate(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        for rule in self.rules:
            if not self._match(rule, tool_name, arguments):
                continue
            if rule.action == HookAction.LOG_ONLY:
                self._log(rule, tool_name, arguments)
                continue
            return rule.action.value
        return None

    def _match(self, rule: HookRule, tool_name: str, args: Dict[str, Any]) -> bool:
        if rule.match_tool != "*" and rule.match_tool != tool_name:
            return False
        if rule.match_cmd_regex:
            cmd = args.get("cmd", "")
            if not cmd or not re.search(rule.match_cmd_regex, cmd):
                return False
        return True

    def _log(self, rule: HookRule, tool_name: str, args: Dict[str, Any]) -> None:
        import logging
        logger = logging.getLogger(f"hook.{rule.id}")
        logger.info(f"{tool_name}: {rule.message or 'log_only'} args={args}")
