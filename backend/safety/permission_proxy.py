"""最小权限代理

策略（仿 Claude Code settings-strict.json）：
- **默认 = 启动 Agent 的当前用户**（不再强制 nobody 中间层）。
  Agent 的权限 = 用户的权限，由内核 DAC 自然约束。
- **通过 `sudo -n -u <user>` 切换**：用于显式需要其他账户能力的场景。
- **root 提权**：仅 `safety.allow_root_for_tools` 白名单内的工具可调
  `shell.execute_elevated`，policy engine 已硬拦。
- **能力 → 用户的映射** 在 policy.yaml 中定义（user_capability_map）。
  字段缺失时**回落到"当前用户"**而非 nobody——保持 Agent 与启动者同权。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from utils.config import get_config


@dataclass
class PermissionResult:
    allowed: bool
    user: str
    reason: str
    elevated: bool = False


class PermissionProxy:
    """权限代理：根据工具能力映射到允许的运行用户。"""

    def __init__(self) -> None:
        cfg = get_config()
        safety = cfg.get("settings", {}).get("safety", {})
        # 默认值改为 None 表示"当前用户"（不再 nobody）
        # 保留 settings 字段以兼容旧配置；如果用户显式配了 nobody 才用它
        cfg_default = safety.get("default_user")
        if cfg_default == "nobody":
            # 旧配置自动升级：忽略 nobody，落到当前用户
            self.default_user: Optional[str] = None
        else:
            self.default_user = cfg_default  # None 或显式用户

        self.elevated_user = safety.get("elevated_user", "opsagent")
        self.allow_root = safety.get("allow_root_for_tools", []) or []
        # 兼容旧字段 deny_root=True：默认允许 elevated 工具提权 root
        self.deny_root = safety.get("deny_root", False)

        policy = cfg.get("policy", {}).get("permissions", {})
        self.user_capability_map = policy.get("user_capability_map", {}) or {}

    def resolve_user(self, capability: str, requested_user: Optional[str] = None) -> PermissionResult:
        """根据能力解析允许的用户。

        Args:
            capability: 工具声明的能力，如 file.write
            requested_user: 调用方指定的用户（可选）

        Returns:
            PermissionResult 包含允许的用户与原因
        """
        # 显式 root 请求：仅在 allow_root 白名单内允许
        if requested_user == "root":
            if self.allow_root and capability in self.allow_root:
                return PermissionResult(
                    allowed=True,
                    user="root",
                    reason=f"root 提权允许（capability={capability} 在 allow_root 白名单）",
                    elevated=True,
                )
            return PermissionResult(
                allowed=False,
                user="",
                reason="root 提权被 policy 拒绝（不在 allow_root 白名单）",
            )

        allowed_users = self.user_capability_map.get(capability, [self.default_user])

        if requested_user:
            if requested_user in allowed_users:
                return PermissionResult(
                    allowed=True,
                    user=requested_user,
                    reason=f"User '{requested_user}' explicitly allowed for '{capability}'",
                    elevated=(requested_user != self.default_user),
                )
            return PermissionResult(
                allowed=False,
                user="",
                reason=f"User '{requested_user}' not allowed for capability '{capability}' "
                       f"(allowed: {allowed_users})",
            )

        # 默认用户（None → 当前用户）
        target = self.default_user
        return PermissionResult(
            allowed=True,
            user=target if target else _current_user(),
            reason=f"使用{'当前用户' if not target else target}（capability={capability}）",
            elevated=False,
        )


def _current_user() -> str:
    """取启动 Agent 的当前用户名（pwnam.getpwuid os.getuid）。"""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        # Windows / 非 POSIX 兜底
        return os.environ.get("USER") or os.environ.get("USERNAME") or "current"