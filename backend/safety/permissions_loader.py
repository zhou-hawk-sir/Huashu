"""三层 permissions 加载与合并。

- managed: /etc/opsagent/permissions.yaml (system-level, MDM-pushed)
- project: config/permissions.yaml (team-shareable)
- user: ~/.opsagent/permissions.local.yaml (personal override)

合并规则：
- deny: 三层并集
- ask: 三层并集
- allow: 按 user > project > managed 顺序拼接
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml


@dataclass
class Permissions:
    allow: List[str] = field(default_factory=list)
    ask: List[str] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def load_managed() -> Dict[str, Any]:
    return _load_yaml(Path("/etc/opsagent/permissions.yaml"))


def load_project() -> Dict[str, Any]:
    return _load_yaml(Path("config/permissions.yaml"))


def load_user() -> Dict[str, Any]:
    return _load_yaml(Path.home() / ".opsagent" / "permissions.local.yaml")


def merge(*sources: Dict[str, Any]) -> Permissions:
    out = Permissions()
    for src in sources:
        perms = src.get("permissions", {}) if isinstance(src, dict) else {}
        out.deny.extend(perms.get("deny", []) or [])
        out.ask.extend(perms.get("ask", []) or [])
    for src in reversed(sources):
        perms = src.get("permissions", {}) if isinstance(src, dict) else {}
        out.allow.extend(perms.get("allow", []) or [])
    return out


def load_and_merge() -> Permissions:
    return merge(load_managed(), load_project(), load_user())
