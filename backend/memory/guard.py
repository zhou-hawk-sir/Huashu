"""记忆边界断言：权限独立、风险独立、审批独立、策略独立

这四项是整个「解耦」论述能否成立的地方，因此必须是**可执行断言**，
不能只写在文档里。实现方式是：在每次工具执行前，把本次上下文引用到的记忆
读出来，逐项检查它有没有越界——是否自称能授权、能否降风险、能否免审批、能否改策略。

任一断言失败即拒绝执行，并在追踪里留下 memory.blocked 节点，
让「记忆导致的问题」与「策略导致的问题」在回放中可区分。

关于记忆不可读：记忆不是安全组件，读不到记忆不等于不安全，
此时返回允许并把 memory_readable 置 False，由基础安全链独立把关。
反过来，绝不能因为「记忆说可以」就放行——那才是真正的越界。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from utils.logger import get_logger

from .types import STATUS_QUARANTINED, GuardResult

logger = get_logger("memory.guard")

# 断言名（写入追踪与界面，保持稳定，不要随意改）
A_PERMISSION = "permission_independent"
A_RISK = "risk_independent"
A_APPROVAL = "approval_independent"
A_POLICY = "policy_independent"
A_NO_QUARANTINE = "no_quarantined_reference"
# 第五项为内容检查：记忆正文里不得夹带指令（提示词注入）。四项边界断言之外
# 再加这一条，是因为「记忆不得作为指令来源」与「记忆不得授权」是两件事。
A_NO_INSTRUCTION = "no_instruction_content"

_DICT_KEYS = {
    A_PERMISSION: {
        "capability", "capabilities", "grant", "grants", "authorized", "authorization",
        "permission", "permissions", "allow_tools", "permitted_tools", "sudo", "root",
    },
    A_RISK: {"risk_level", "risk", "risk_override", "risk_class", "severity", "low_risk"},
    A_APPROVAL: {
        "approval", "approved", "approved_by", "skip_confirm", "skip_approval",
        "no_confirm", "confirm_required", "pre_approved",
    },
    A_POLICY: {
        "policy", "policies", "deny_root", "tool_permissions", "allow_root_for_tools",
        "ruleset", "sandbox", "default_user", "elevated_user", "hooks", "whitelist",
        "blacklist", "denied_commands",
    },
}

_TEXT_PATTERNS = {
    A_PERMISSION: (
        r"(已|已经|获得|取得)[^。；\n]{0,8}(授权|批准|许可)",
        r"(管理员|root|运维负责人|安全管理员)[^。；\n]{0,10}(授权|批准|同意|允许)",
        r"(authorized|approved)\s+by\s+(admin|root|operator)",
        r"(有|具备|拥有)[^。；\n]{0,6}(权限|capability)",
    ),
    A_RISK: (
        r"(属|为|视为)[^。；\n]{0,4}(低风险|安全操作|无风险)",
        r"(降低|下调|改为)[^。；\n]{0,6}风险(等级)?",
        r"(无需|不用)[^。；\n]{0,6}(风险)?(评估|分级)",
    ),
    A_APPROVAL: (
        r"(无需|不用|免|跳过|绕过)[^。；\n]{0,6}(确认|审批|审核|复核)",
        r"(已|已经)[^。；\n]{0,6}(确认|审批|批准)[^。；\n]{0,4}(通过|完成)",
        r"(skip|bypass)[^.\n]{0,20}(confirmation|approval)",
    ),
    A_POLICY: (
        r"(修改|变更|关闭|禁用|设置)[^。；\n]{0,8}(策略|规则|白名单|黑名单|沙箱)",
        r"deny_root\s*[:=]\s*(true|false)",
        r"(allow|deny)_root",
        r"tool_permissions",
        r"(写入|编辑)[^。；\n]{0,6}(settings|policy)\.(yaml|json)",
    ),
}

_COMPILED = {
    name: tuple(re.compile(p, re.IGNORECASE) for p in pats)
    for name, pats in _TEXT_PATTERNS.items()
}

_ASSERT_LABELS = {
    A_PERMISSION: "记忆不得授予权限",
    A_RISK: "记忆不得改变风险等级",
    A_APPROVAL: "记忆不得免除审批",
    A_POLICY: "记忆不得修改安全策略",
    A_NO_QUARANTINE: "隔离区记忆不得参与判定",
    A_NO_INSTRUCTION: "记忆正文不得夹带指令",
}


@dataclass
class Violation:
    assertion: str
    memory_id: str
    field: str
    evidence: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assertion": self.assertion,
            "memory_id": self.memory_id,
            "field": self.field,
            "evidence": self.evidence[:160],
        }


def _walk_keys(obj: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    """递归列出所有 (键名, 路径)。值为字符串时键名也返回，便于按 key 命中。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            yield str(k).lower(), path
            yield from _walk_keys(v, path)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_keys(v, f"{prefix}[{i}]")


def _flatten_text(obj: Any) -> str:
    if isinstance(obj, dict):
        return " ".join(_flatten_text(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return " ".join(_flatten_text(v) for v in obj)
    return str(obj) if obj is not None else ""


class MemoryGuard:
    """四项边界断言。只读记忆，不做任何写入与打分。"""

    def __init__(self, store: Any = None) -> None:
        self.store = store

    # ── 单条记忆检查 ─────────────────────────────────────────────────
    def check_record(self, rec: Any) -> List[Violation]:
        """检查一条记忆是否越界。返回违规列表，空列表表示合规。"""
        out: List[Violation] = []
        mid = str(getattr(rec, "id", ""))

        fields = {
            "conditions": getattr(rec, "conditions", {}) or {},
            "provenance": getattr(rec, "provenance", {}) or {},
        }
        for field_name, payload in fields.items():
            for key, path in _walk_keys(payload):
                for assertion, keys in _DICT_KEYS.items():
                    if key in keys:
                        out.append(
                            Violation(
                                assertion=assertion,
                                memory_id=mid,
                                field=f"{field_name}.{path}",
                                evidence=f"含越界字段 {key}",
                            )
                        )

        text_fields = {
            "title": getattr(rec, "title", ""),
            "symptom": getattr(rec, "symptom", ""),
            "diagnosis": getattr(rec, "diagnosis", ""),
            "solution": getattr(rec, "solution", ""),
        }
        for field_name, text in text_fields.items():
            if not text:
                continue
            for assertion, patterns in _COMPILED.items():
                for rx in patterns:
                    m = rx.search(str(text))
                    if m:
                        out.append(
                            Violation(
                                assertion=assertion,
                                memory_id=mid,
                                field=field_name,
                                evidence=m.group(0),
                            )
                        )
                        break

        # 内容检查：正文夹带指令（提示词注入）
        try:
            from .writer import scan_instructions

            hits = scan_instructions(" ".join(str(v) for v in text_fields.values()))
        except Exception:
            hits = []
        if hits:
            out.append(
                Violation(
                    assertion=A_NO_INSTRUCTION,
                    memory_id=mid,
                    field="content",
                    evidence="、".join(hits),
                )
            )

        # 同一字段同一断言只留一条，避免刷屏
        dedup: Dict[tuple, Violation] = {}
        for v in out:
            dedup.setdefault((v.assertion, v.field), v)

        if getattr(rec, "status", "") == STATUS_QUARANTINED:
            dedup[(A_NO_QUARANTINE, "status")] = Violation(
                assertion=A_NO_QUARANTINE,
                memory_id=mid,
                field="status",
                evidence="quarantined",
            )
        return list(dedup.values())

    # ── 主入口 ───────────────────────────────────────────────────────
    def assert_boundary(
        self,
        memory_ids: Optional[Sequence[str]] = None,
        tool_name: str = "",
        arguments: Optional[Dict[str, Any]] = None,
        risk_level: str = "",
    ) -> GuardResult:
        """四项断言。任一失败 → allowed=False，reason 为断言名。

        memory_ids 为空表示本次上下文没有引用任何记忆，此时四项断言天然成立。
        """
        assertions = {
            A_PERMISSION: True,
            A_RISK: True,
            A_APPROVAL: True,
            A_POLICY: True,
            A_NO_QUARANTINE: True,
            A_NO_INSTRUCTION: True,
        }

        if self.store is None or not memory_ids:
            return GuardResult.ok(assertions)

        violations: List[Violation] = []
        readable = True
        for mid in memory_ids:
            if not mid:
                continue
            try:
                rec = self.store.get(str(mid))
            except Exception as exc:
                readable = False
                logger.warning("边界断言读取记忆失败（降级为无记忆）：%s", exc)
                break
            if rec is None:
                continue
            violations.extend(self.check_record(rec))

        if not readable:
            assertions["memory_readable"] = False
            return GuardResult.ok(assertions)

        if not violations:
            return GuardResult.ok(assertions)

        for v in violations:
            assertions[v.assertion] = False

        first = violations[0]
        reason = (
            f"{_ASSERT_LABELS.get(first.assertion, first.assertion)}"
            f"（{first.memory_id} / {first.field}：{first.evidence[:60]}）"
        )
        logger.warning(
            "记忆边界断言拒绝：tool=%s risk=%s reason=%s 共 %d 项",
            tool_name,
            risk_level,
            reason,
            len(violations),
        )
        result = GuardResult.deny(reason, assertions)
        result.violations = [v.to_dict() for v in violations]  # type: ignore[attr-defined]
        return result

    # ── 说明文案 ─────────────────────────────────────────────────────
    @staticmethod
    def describe(assertions: Dict[str, bool]) -> str:
        parts = []
        for name, ok in assertions.items():
            label = _ASSERT_LABELS.get(name, name)
            parts.append(f"{label}{'✓' if ok else '✗'}")
        return "；".join(parts)
