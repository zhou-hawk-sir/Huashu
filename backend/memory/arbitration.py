"""四级冲突仲裁：环境匹配 > 验证次数 > 最近验证时间 > 并列标记

核心约束：**禁止自动择一**。
四级规则都用尽仍无法分出高下时，必须并列呈现并标记 conflict，
由人决定，而不是让系统「挑一个看起来合理的」。

为什么不用打分高低代替仲裁：
分数是连续值，两条经验差 0.001 也能排出先后，但那是噪声不是依据。
仲裁只在可解释的离散维度上比较，比不出来就承认比不出来。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils.logger import get_logger

from .tokenizer import tokens
from .types import (
    STATUS_CONFLICT,
    Candidate,
    EnvFingerprint,
)

logger = get_logger("memory.arbitration")

LEVEL_ENV = "env"            # 环境匹配度分出高下
LEVEL_SUCCESS = "success"    # 历史验证次数分出高下
LEVEL_RECENT = "recent"      # 最近验证时间分出高下
LEVEL_TIE = "tie"            # 四级用尽仍并列
LEVEL_SINGLE = "single"      # 组内只有一条，无需仲裁

_ENV_MARGIN = 0.05           # 环境分差阈值，小于此值视为同档
_SYMPTOM_SIM = 0.55          # 症状相似度阈值：超过视为「同一类问题」
_SOLUTION_DIFF = 0.45        # 解法相似度阈值：低于视为「结论不一致」


def _tokens(text: str) -> set:
    return {t for t in tokens(text or "") if len(t) > 0}


def similarity(a: str, b: str) -> float:
    """词项 Jaccard。纯标准库，无向量依赖。"""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _parse_ts(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _env_rank(cand: Candidate) -> float:
    """环境匹配的综合档位分。只用于仲裁比较，不参与最终打分。"""
    levels = cand.memory.get("levels") or {}
    weights = {"os": 0.5, "arch": 0.3, "kernel": 0.1, "deps": 0.1}
    score_map = {"exact": 1.0, "minor": 0.7, "major": 0.3, "none": 0.0}
    total = 0.0
    for dim, w in weights.items():
        total += w * score_map.get(str(levels.get(dim, "none")), 0.0)
    return round(total, 6)


def _verify_count(cand: Candidate) -> int:
    m = cand.memory
    return int(m.get("success_count", 0)) + int(m.get("fail_count", 0))


@dataclass
class ArbitrationResult:
    """一组的仲裁结论。"""

    level: str = LEVEL_SINGLE
    winner: Optional[Candidate] = None
    reason: str = ""
    group: List[Candidate] = field(default_factory=list)

    @property
    def decided(self) -> bool:
        return self.winner is not None

    @property
    def tied(self) -> bool:
        return self.winner is None and len(self.group) > 1

    @property
    def auto_pick(self) -> bool:
        """是否发生了「无依据的自动择一」。

        只有 winner 存在而 level 为 tie 才算，代码路径里不可能出现；
        这条属性用于实验统计与断言（自动择一率必须为 0）。
        """
        return self.winner is not None and self.level == LEVEL_TIE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "decided": self.decided,
            "tied": self.tied,
            "auto_pick": self.auto_pick,
            "winner": self.winner.memory.get("id") if self.winner else None,
            "reason": self.reason,
            "group": [c.memory.get("id") for c in self.group],
        }


@dataclass
class ConflictGroup:
    """检出的冲突。dimension 记录仲裁所依据的维度，未判定则为 unresolved。"""

    memory_ids: List[str] = field(default_factory=list)
    dimension: str = "unresolved"
    symptom: str = ""
    solutions: List[str] = field(default_factory=list)
    arbitration: Optional[ArbitrationResult] = None

    @property
    def unresolved(self) -> bool:
        return self.dimension == "unresolved"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_ids": list(self.memory_ids),
            "dimension": self.dimension,
            "symptom": self.symptom,
            "solutions": list(self.solutions),
            "unresolved": self.unresolved,
            "arbitration": self.arbitration.to_dict() if self.arbitration else None,
        }


class Arbiter:
    """冲突检出与四级仲裁。"""

    def __init__(
        self,
        store: Any = None,
        symptom_sim: float = _SYMPTOM_SIM,
        solution_diff: float = _SOLUTION_DIFF,
    ) -> None:
        self.store = store
        self.symptom_sim = symptom_sim
        self.solution_diff = solution_diff

    # ── 仲裁 ─────────────────────────────────────────────────────────
    def arbitrate(self, group: Sequence[Candidate]) -> ArbitrationResult:
        """对同一问题的多条经验做四级仲裁。"""
        members = list(group)
        if len(members) <= 1:
            return ArbitrationResult(
                level=LEVEL_SINGLE,
                winner=members[0] if members else None,
                reason="" if not members else "唯一候选",
                group=members,
            )

        # 第一级：环境匹配度
        best_env = max(_env_rank(c) for c in members)
        env_winners = [c for c in members if best_env - _env_rank(c) <= _ENV_MARGIN]
        if len(env_winners) == 1:
            return ArbitrationResult(
                level=LEVEL_ENV,
                winner=env_winners[0],
                reason="按环境匹配度裁决",
                group=members,
            )

        # 第二级：历史验证次数
        pool = env_winners
        best_cnt = max(_verify_count(c) for c in pool)
        cnt_winners = [c for c in pool if _verify_count(c) == best_cnt]
        if len(cnt_winners) == 1:
            return ArbitrationResult(
                level=LEVEL_SUCCESS,
                winner=cnt_winners[0],
                reason=f"按历史验证次数裁决（{best_cnt} 次）",
                group=members,
            )

        # 第三级：最近验证时间
        pool = cnt_winners
        stamped = [(_parse_ts(c.memory.get("last_verified_at")), c) for c in pool]
        if all(ts is not None for ts, _ in stamped):
            latest = max(ts for ts, _ in stamped)  # type: ignore[type-var]
            recent = [c for ts, c in stamped if ts == latest]
            if len(recent) == 1:
                return ArbitrationResult(
                    level=LEVEL_RECENT,
                    winner=recent[0],
                    reason="按最近验证时间裁决",
                    group=members,
                )

        # 第四级：并列标记，不选
        return ArbitrationResult(
            level=LEVEL_TIE,
            winner=None,
            reason="四级仲裁无法判定，并列呈现，需人工确认",
            group=members,
        )

    # ── 冲突检出 ─────────────────────────────────────────────────────
    def group_candidates(self, cands: Sequence[Candidate]) -> List[List[Candidate]]:
        """按「同一症状」把候选归并成若干组。

        用并查集而非简单聚类：相似关系不传递时也能得到稳定分组。
        """
        members = [c for c in cands if not c.excluded]
        n = len(members)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        texts = [
            f"{c.memory.get('title', '')} {c.memory.get('symptom', '')}" for c in members
        ]
        for i, j in combinations(range(n), 2):
            if similarity(texts[i], texts[j]) >= self.symptom_sim:
                union(i, j)

        buckets: Dict[int, List[Candidate]] = {}
        for i in range(n):
            buckets.setdefault(find(i), []).append(members[i])
        return list(buckets.values())

    def detect(self, cands: Sequence[Candidate]) -> List[ConflictGroup]:
        """检出结论不一致的组，附仲裁结论。"""
        out: List[ConflictGroup] = []
        for group in self.group_candidates(cands):
            if len(group) < 2:
                continue
            solutions = [str(c.memory.get("solution", "")) for c in group]
            divergent = False
            for a, b in combinations(solutions, 2):
                if similarity(a, b) < self.solution_diff:
                    divergent = True
                    break
            if not divergent:
                continue

            arb = self.arbitrate(group)
            dimension = "unresolved" if arb.level == LEVEL_TIE else arb.level
            out.append(
                ConflictGroup(
                    memory_ids=[str(c.memory.get("id")) for c in group],
                    dimension=dimension,
                    symptom=str(group[0].memory.get("symptom", "")),
                    solutions=solutions,
                    arbitration=arb,
                )
            )
        return out

    # ── 组合入口 ─────────────────────────────────────────────────────
    def apply(
        self, cands: Sequence[Candidate], persist: bool = True
    ) -> Tuple[List[Candidate], List[ConflictGroup]]:
        """跑一遍冲突检出，把「与谁冲突」写回候选，并按需落库。"""
        conflicts = self.detect(cands)
        if not conflicts:
            return list(cands), []

        for group in conflicts:
            ids = group.memory_ids
            for c in cands:
                mid = str(c.memory.get("id"))
                if mid in ids:
                    others = [i for i in ids if i != mid]
                    c.memory["conflict_with"] = others
                    c.memory["conflict_dimension"] = group.dimension

            if persist and self.store is not None:
                for a, b in combinations(ids, 2):
                    try:
                        self.store.add_conflict(a, b, group.dimension, None)
                    except Exception as exc:  # 落库失败不影响检索返回
                        logger.warning("冲突落库失败: %s", exc)
                for mid in ids:
                    rec = self.store.get(mid)
                    if rec is not None and rec.status == "active" and group.unresolved:
                        try:
                            self.store.set_status(
                                mid, STATUS_CONFLICT, "检索时检出结论冲突且无法仲裁"
                            )
                        except Exception as exc:
                            logger.warning("冲突状态写入失败: %s", exc)

        return list(cands), conflicts


def mark_conflicts(cands: Sequence[Candidate], conflicts: Sequence[ConflictGroup]) -> None:
    """只做标记、不落库的轻量版本，供实验组（治理关闭）复用。"""
    for group in conflicts:
        ids = group.memory_ids
        for c in cands:
            mid = str(c.memory.get("id"))
            if mid in ids:
                c.memory["conflict_with"] = [i for i in ids if i != mid]
                c.memory["conflict_dimension"] = group.dimension
