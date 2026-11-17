"""三级风险分类器

基于正则与配置规则对工具调用进行分级：
- low: 自动执行
- medium: 需二次确认
- high: 直接拒绝

设计原则（仿照 Claude Code 的 settings.allow/deny + hooks）：
1. 高危永远 DENY，先匹配（让 HIGH 优先生效）
2. 中危需确认
3. 低危自动放行
4. 路径在 protected_paths 下，升级到 HIGH（覆盖 LOW/MEDIUM）
5. 命令规范化：去除绝对路径/env 前缀，使 ^cmd 能匹配 `/usr/bin/mkdir`
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from utils.config import get_config


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class RiskDecision:
    level: RiskLevel
    allow: bool
    reason: str
    matched_rule: Optional[str] = None
    require_confirmation: bool = False


class RiskClassifier:
    """风险分类器。"""

    # 会在命令前剥离的前缀集合，让 `^mkdir` 能匹配 `/usr/bin/mkdir`
    _ABS_PATH_PREFIXES = (
        "/usr/bin/", "/usr/sbin/", "/usr/local/bin/", "/usr/local/sbin/",
        "/bin/", "/sbin/",
        "/usr/libexec/", "/snap/bin/",
    )

    def __init__(self) -> None:
        cfg = get_config()
        rules = cfg.get("risk_rules", {}).get("risk_rules", {})
        self._low_patterns = [
            re.compile(p) for p in rules.get("low", {}).get("patterns", [])
        ]
        self._medium_patterns = [
            re.compile(p) for p in rules.get("medium", {}).get("patterns", [])
        ]
        self._high_patterns = [
            re.compile(p) for p in rules.get("high", {}).get("patterns", [])
        ]
        self._parameter_checks = rules.get("parameter_checks", {})

    def _normalize_command(self, cmd: str) -> str:
        """去除命令前缀的绝对路径或 env 调用，便于 `^cmd` 正则匹配。

        例子：
            /usr/bin/mkdir /tmp/a   -> mkdir /tmp/a
            env FOO=bar ls          -> ls
            sudo cat /etc/passwd    -> cat /etc/passwd    # 不剥离 sudo（视作 MEDIUM/HIGH）
        """
        s = cmd.strip()
        # 多次剥离（处理 env X=1 env Y=2 cmd 这种嵌套）
        for _ in range(3):
            changed = False
            # /usr/bin/cmd /usr/sbin/...
            for pref in self._ABS_PATH_PREFIXES:
                if s.startswith(pref):
                    s = s[len(pref):]
                    changed = True
                    break
            # env KEY=VAL cmd -> cmd
            if s.startswith("env "):
                # 吃 env 后所有 KEY=VAL token
                rest = s[4:]
                tokens = rest.split(None, 1)
                if tokens:
                    rest = tokens[1] if len(tokens) > 1 else ""
                if rest:
                    s = rest
                    changed = True
            if not changed:
                break
        return s

    def classify_command(self, cmd: str) -> RiskDecision:
        """对一条 shell 命令分级。"""
        cmd = cmd.strip()
        if not cmd:
            return RiskDecision(RiskLevel.LOW, True, "empty command")

        # 0) 规范化：去除 /usr/bin/、env 前缀，让简单 ^cmd 正则能匹配
        cmd_n = self._normalize_command(cmd)

        # 1) 高危优先（先匹配高，因为高优先于中低，且不可被降级覆盖）
        for pat in self._high_patterns:
            if pat.search(cmd) or pat.search(cmd_n):
                return RiskDecision(
                    level=RiskLevel.HIGH,
                    allow=False,
                    reason=f"Command matches high-risk pattern: {pat.pattern}",
                    matched_rule=pat.pattern,
                )

        # 2) 中危
        for pat in self._medium_patterns:
            if pat.search(cmd) or pat.search(cmd_n):
                decision = RiskDecision(
                    level=RiskLevel.MEDIUM,
                    allow=True,
                    reason=f"Command matches medium-risk pattern: {pat.pattern}",
                    matched_rule=pat.pattern,
                    require_confirmation=True,
                )
                return self._escalate_for_protected_paths(cmd, decision)

        # 3) 低危
        for pat in self._low_patterns:
            if pat.search(cmd) or pat.search(cmd_n):
                decision = RiskDecision(
                    level=RiskLevel.LOW,
                    allow=True,
                    reason=f"Command matches low-risk pattern: {pat.pattern}",
                    matched_rule=pat.pattern,
                )
                return self._escalate_for_protected_paths(cmd, decision)

        # 默认按中危处理：未识别命令需确认
        decision = RiskDecision(
            level=RiskLevel.MEDIUM,
            allow=True,
            reason="Command not recognized by any rule; default to medium",
            require_confirmation=True,
        )

        # 升级检查：若命令触碰 protected_paths 下的文件，即便命令本身 LOW 也升级 HIGH
        return self._escalate_for_protected_paths(cmd, decision)

    def _escalate_for_protected_paths(self, cmd: str, decision: RiskDecision) -> RiskDecision:
        """若 cmd 触碰 protected_paths 下的关键文件，升级到 HIGH。

        适用于 LOW / MEDIUM 命中后的二次校准；HIGH 已 deny 时保持原状。
        """
        cfg = get_config()
        # 注意：protected_paths 在 risk_rules 顶层（与 risk_rules: 同级）
        protected = cfg.get("risk_rules", {}).get("protected_paths", []) or []
        if not protected:
            return decision

        # 提取命令中所有路径 token（排除 URL、参数前缀）
        tokens = re.findall(r"(?<!\w)/(?:[\w./-]+)", cmd)
        for tok in tokens:
            for prot in protected:
                norm_prot = prot.rstrip("/")
                if tok == prot or tok == norm_prot or tok.startswith(norm_prot + "/"):
                    if decision.level in (RiskLevel.LOW, RiskLevel.MEDIUM):
                        return RiskDecision(
                            level=RiskLevel.HIGH,
                            allow=False,
                            reason=f"path 命中保护前缀 ({prot})：{tok}",
                            matched_rule=f"protected_path:{prot}",
                        )
        return decision

    def classify_tool(self, tool_name: str, arguments: Dict[str, Any]) -> RiskDecision:
        """对工具调用分级（结合工具自身 risk_level 与参数检查）。

        特殊：shell.execute 的真实风险跟随 arguments['cmd']。
        这让 mkdir / cat 这类 low 命令不会被工具的 MEDIUM 默认值拖累。
        """
        from tools.base import RiskLevel as ToolRisk

        # 特殊：shell.execute / shell.execute_elevated 看实际命令
        if tool_name in ("shell.execute", "shell.execute_elevated") and arguments.get("cmd"):
            cmd_risk = self.classify_command(arguments["cmd"])
            # elevated 工具无论内层 cmd 风险如何，必须 require_confirmation=True
            # 提权到 root 本身是高危动作，不能被内层 LOW 拖平
            if tool_name == "shell.execute_elevated":
                cmd_risk = type(cmd_risk)(
                    level=cmd_risk.level,
                    allow=cmd_risk.allow,
                    reason=f"{cmd_risk.reason} (elevated: requires confirmation)",
                    matched_rule=cmd_risk.matched_rule,
                    require_confirmation=True,
                )
            return cmd_risk

        tool_level = ToolRisk.LOW
        try:
            from mcp.registry import get_default_registry
            tool = get_default_registry().get(tool_name)
            if tool:
                tool_level = tool.risk_level
        except Exception:
            pass

        # 参数级检查：例如 rm 路径在 /etc 下要升级
        base_name = tool_name.split(".")[-1] if tool_name else ""
        arg_checks = self._parameter_checks.get(base_name, {})
        if arg_checks:
            escalate = arg_checks.get("escalate_to")
            conditions = arg_checks.get("when", [])
            for cond in conditions:
                if cond.startswith("path_starts_with:"):
                    prefix = cond.split(":", 1)[1].strip()
                    path_arg = arguments.get("path") or ""
                    if path_arg.startswith(prefix) and escalate == "high":
                        return RiskDecision(
                            level=RiskLevel.HIGH,
                            allow=False,
                            reason=f"Path '{path_arg}' under protected '{prefix}'",
                        )

        if tool_level == ToolRisk.HIGH:
            return RiskDecision(
                level=RiskLevel.HIGH,
                allow=False,
                reason=f"Tool '{tool_name}' is high-risk by definition",
            )
        if tool_level == ToolRisk.MEDIUM:
            return RiskDecision(
                level=RiskLevel.MEDIUM,
                allow=True,
                reason=f"Tool '{tool_name}' is medium-risk",
                require_confirmation=True,
            )
        return RiskDecision(
            level=RiskLevel.LOW,
            allow=True,
            reason=f"Tool '{tool_name}' is low-risk",
        )

    def classify_paths(self, paths: List[str]) -> RiskDecision:
        """检查路径是否触及保护目录。"""
        cfg = get_config()
        protected = cfg.get("risk_rules", {}).get("protected_paths") or []
        for p in paths:
            for prot in protected:
                if p.startswith(prot):
                    return RiskDecision(
                        level=RiskLevel.HIGH,
                        allow=False,
                        reason=f"Path '{p}' under protected '{prot}'",
                    )
        return RiskDecision(RiskLevel.LOW, True, "all paths safe")
