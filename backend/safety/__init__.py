"""安全护栏 - 仿照 Claude 的 safety pipeline

五级防护（与赛题"安全护栏"三层对齐 + 拆分）：
- L1 输入过滤：injection_detector          （抗 Prompt Inject）
- L2 工具风险：risk_classifier              （风险分级：low/medium/high/critical）
- L2.5 短路层：hooks.HookEngine             （PreToolUse 凌驾 risk，仿 claude-code）
- L2.7 命令权限：command_enforcer           （settings.tool_permissions 正则 deny）
- L3 权限隔离：permission_proxy             （RBAC 最小权限 + 受限 Account）
- L4 二次确认：confirmation                 （中风险人审）
- L5 策略引擎：policy_engine                （整合上面所有信号，输出 ALLOW/CONFIRM/DENY）

辅助：
- permissions_loader 三层合并（managed/project/user，allow 反向叠加）
"""
from .risk_classifier import RiskClassifier, RiskDecision, RiskLevel
from .injection_detector import InjectionDetector, InjectionVerdict
from .permission_proxy import PermissionProxy, PermissionResult
from .policy_engine import PolicyEngine
from .confirmation import ConfirmationManager
from .hooks import HookEngine, HookAction, HookRule, load_hooks_from_yaml  # noqa: F401
from .command_enforcer import (  # noqa: F401
    CommandEnforcer,
    Decision,
    get_enforcer,
    reset_enforcer,
)
from .permissions_loader import (  # noqa: F401
    Permissions,
    load_and_merge,
    load_managed,
    load_project,
    load_user,
    merge,
)

__all__ = [
    "RiskClassifier",
    "RiskDecision",
    "RiskLevel",
    "InjectionDetector",
    "InjectionVerdict",
    "PermissionProxy",
    "PermissionResult",
    "PolicyEngine",
    "ConfirmationManager",
    # L2.5 / L2.7
    "HookEngine",
    "HookAction",
    "HookRule",
    "load_hooks_from_yaml",
    "CommandEnforcer",
    "Decision",
    "get_enforcer",
    "reset_enforcer",
    # 三层 permissions
    "Permissions",
    "load_and_merge",
    "load_managed",
    "load_project",
    "load_user",
    "merge",
]