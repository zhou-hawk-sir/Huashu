"""策略引擎

集中入口：接收意图（自然语言/工具调用），输出执行决策（allow/confirm/deny）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from .injection_detector import InjectionDetector, InjectionVerdict
from .permission_proxy import PermissionProxy, PermissionResult
from .risk_classifier import RiskClassifier, RiskDecision, RiskLevel
from .workspace import WorkspaceTrust, PROTECTED_PREFIXES

# Forward import for type hints; avoid circular at runtime by accepting Optional
# and resolving HookEngine lazily in evaluate_tool_call.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .hooks import HookEngine


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass
class PolicyVerdict:
    decision: Decision
    reason: str
    risk_level: Optional[RiskLevel] = None
    permission: Optional[PermissionResult] = None
    injection: Optional[InjectionVerdict] = None
    user_to_run: Optional[str] = None
    workspace_hits: bool = False  # True 表示本次降级是由 workspace 信任触发
    require_confirmation: bool = False  # True 表示本次 CONFIRM 由 hook 触发


def _touches_protected_path(tool_name: str, arguments: Dict[str, Any]) -> bool:
    """提取工具参数的 path 类字段，判断是否触及 PROTECTED_PREFIXES。

    凌驾 workspace 信任之上：即便在 workspace 内，命中保护前缀也拒绝。

    注意：为了在 Windows 等跨平台环境下行为一致，这里**不对输入做
    ``realpath`` 解析**——直接基于原始字符串前缀比对。真实 symlink 逃逸
    在 ``_workspace_hit`` 那一层处理（那里才需要 ``realpath``）。
    """
    path_keys = ("path", "file", "target", "src", "dst")
    for k in path_keys:
        v = arguments.get(k)
        if not isinstance(v, str) or not v:
            continue
        v_norm = v.rstrip("/\\")
        for prefix in PROTECTED_PREFIXES:
            if v_norm == prefix or v_norm.startswith(prefix + "/") or v_norm.startswith(prefix + "\\"):
                return True
    return False


def _workspace_hit(
    tool_name: str,
    arguments: Dict[str, Any],
    workspace_root: str,
    trust: WorkspaceTrust,
) -> bool:
    """判定本工具调用是否落在 workspace 内（任一参数路径或 shell 命令中的 token）。"""
    path_keys = ("path", "file", "target", "src", "dst")
    for k in path_keys:
        v = arguments.get(k)
        if isinstance(v, str) and os.path.isabs(v):
            if trust.contains(workspace_root, v):
                return True

    # shell.execute / elevated：扫描 cmd 中的 /xxx token
    if tool_name in ("shell.execute", "shell.execute_elevated"):
        cmd = arguments.get("cmd", "")
        for token in trust.extract_paths_from_command(cmd):
            if trust.contains(workspace_root, token):
                return True

    # shell.execute 的 cwd 参数
    cwd = arguments.get("cwd")
    if isinstance(cwd, str) and os.path.isabs(cwd):
        if trust.contains(workspace_root, cwd):
            return True

    return False


class PolicyEngine:
    """策略引擎：综合注入检测、风险分级、权限映射输出最终决策。

    新增 workspace 信任：evaluate_tool_call / evaluate_command 接受
    workspace_root: Optional[str]，若设置且路径命中，则 MEDIUM → ALLOW。
    """

    def __init__(
        self,
        llm_client: Any = None,
        workspace_trust: Optional[WorkspaceTrust] = None,
        hook_engine: Optional["HookEngine"] = None,
    ) -> None:
        self.injection = InjectionDetector(llm_client=llm_client)
        self.risk = RiskClassifier()
        self.permission = PermissionProxy()
        self.workspace_trust = workspace_trust or WorkspaceTrust()
        self._hook_engine = hook_engine

    def evaluate_input(self, text: str) -> PolicyVerdict:
        """对用户输入做前置校验。"""
        verdict = self.injection.detect(text)
        if verdict.is_injection:
            return PolicyVerdict(
                decision=Decision.DENY,
                reason=f"输入被检测为注入：{verdict.reason}",
                injection=verdict,
            )
        return PolicyVerdict(
            decision=Decision.ALLOW,
            reason="输入通过注入检测",
            injection=verdict,
        )

    def evaluate_tool_call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        workspace_root: Optional[str] = None,
    ) -> PolicyVerdict:
        """对工具调用做策略评估。

        流程：
        1. 风险分级
        2. 高危直接 DENY（凌驾 workspace）
        3. 权限映射
        4. 保护路径硬拦截（凌驾 workspace）
        5. workspace 信任降级：MEDIUM + 路径在 workspace 内 → ALLOW
        6. 中危 → CONFIRM；低危 → ALLOW
        """
        # 0) Hook 层短路（在风险分级之前 —— hook deny/ask/allow 凌驾风险）
        if self._hook_engine is not None:
            hook_decision = self._hook_engine.evaluate(tool_name, arguments)
            if hook_decision == "deny":
                return PolicyVerdict(
                    decision=Decision.DENY,
                    reason="Blocked by hook",
                    risk_level=RiskLevel.HIGH,
                )
            if hook_decision == "ask":
                return PolicyVerdict(
                    decision=Decision.CONFIRM,
                    reason="Hook requires confirmation",
                    risk_level=RiskLevel.MEDIUM,
                    require_confirmation=True,
                )
            if hook_decision == "allow":
                return PolicyVerdict(
                    decision=Decision.ALLOW,
                    reason="Allowed by hook",
                    risk_level=RiskLevel.LOW,
                )

        risk = self.risk.classify_tool(tool_name, arguments)
        if risk.level == RiskLevel.HIGH:
            return PolicyVerdict(
                decision=Decision.DENY,
                reason=f"高危工具调用被拒：{risk.reason}",
                risk_level=RiskLevel.HIGH,
            )

        # 解析运行用户
        from mcp.registry import get_default_registry
        capability = ""
        tool = get_default_registry().get(tool_name)
        if tool:
            capability = tool.required_capability
        perm = self.permission.resolve_user(capability)
        if not perm.allowed:
            return PolicyVerdict(
                decision=Decision.DENY,
                reason=f"权限不足：{perm.reason}",
                risk_level=risk.level,
                permission=perm,
            )

        # 保护路径硬拦截（凌驾 workspace 信任）
        if _touches_protected_path(tool_name, arguments):
            return PolicyVerdict(
                decision=Decision.DENY,
                reason=f"路径触及受保护前缀：拒绝（即使在 workspace 内也无效）",
                risk_level=risk.level,
                permission=perm,
            )

        # 风险等级 → 决策
        # LOW → ALLOW；MEDIUM → CONFIRM（人审）；HIGH → DENY（已在前置判定）
        if risk.level == RiskLevel.MEDIUM and risk.require_confirmation:
            decision = Decision.CONFIRM
            reason = f"中风险，需用户确认：{risk.reason}"
            require_confirmation = True
        else:
            decision = Decision.ALLOW
            reason = risk.reason
        workspace_hits = False

        # workspace 信任降级：MEDIUM + 路径命中 → ALLOW（用户在 trusted workspace 内的中风险操作免审）
        if workspace_root and risk.level == RiskLevel.MEDIUM:
            if _workspace_hit(tool_name, arguments, workspace_root, self.workspace_trust):
                decision = Decision.ALLOW
                reason = f"{risk.reason}（in trusted workspace）"
                workspace_hits = True

        return PolicyVerdict(
            decision=decision,
            reason=reason,
            risk_level=risk.level,
            permission=perm,
            user_to_run=perm.user,
            workspace_hits=workspace_hits,
            require_confirmation=require_confirmation if decision == Decision.CONFIRM else False,
        )

    def evaluate_command(self, cmd: str, workspace_root: Optional[str] = None) -> PolicyVerdict:
        """对裸 shell 命令做策略评估（兜底）。"""
        risk = self.risk.classify_command(cmd)
        if risk.level == RiskLevel.HIGH:
            return PolicyVerdict(
                decision=Decision.DENY,
                reason=f"高危命令被拒：{risk.reason}",
                risk_level=RiskLevel.HIGH,
            )
        # MEDIUM → CONFIRM（人审）；workspace 内 MEDIUM → ALLOW
        decision = Decision.CONFIRM if risk.level == RiskLevel.MEDIUM else Decision.ALLOW
        reason = (
            f"中风险命令，需用户确认：{risk.reason}"
            if decision == Decision.CONFIRM
            else risk.reason
        )
        workspace_hits = False
        if workspace_root and risk.level == RiskLevel.MEDIUM:
            if any(
                self.workspace_trust.contains(workspace_root, tok)
                for tok in self.workspace_trust.extract_paths_from_command(cmd)
            ):
                decision = Decision.ALLOW
                reason = f"{risk.reason}（in trusted workspace）"
                workspace_hits = True
        return PolicyVerdict(
            decision=decision,
            reason=reason,
            risk_level=risk.level,
            workspace_hits=workspace_hits,
            require_confirmation=decision == Decision.CONFIRM,
        )