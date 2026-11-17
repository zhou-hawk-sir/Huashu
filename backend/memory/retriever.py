"""检索编排：硬性适用性检查 → BM25 召回 → 四因子打分 → 乘性惩罚 → 排序

顺序不可调换，每一步都有明确职责：
    hard_filter  先剔除「根本不能用」的经验，避免它们靠高分挤进结果；
    bm25         解决「找得到」，在海量记录里召回语义相关的一批；
    score        解决「哪个更该用」，四因子加权；
    penalty      解决「不适用不能靠长板掩盖」，乘性折扣；
    arbitrate    解决「两条都像，但不能替人决定」，冲突并列。

硬性检查必须前置：如果放到打分之后，一条语义 0.95、成功率 100% 但架构不兼容的
经验仍可能排第一，那样后面的乘法只是在给错误结果打补丁。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from utils.logger import get_logger

from .arbitration import Arbiter, ConflictGroup
from .expiry import stale_hint
from .fingerprint import describe, is_hard_incompatible, match
from .scorer import BM25Index, Scorer, normalize_sim
from .store import MemoryStore
from .tokenizer import explained_hits
from .types import (
    MATCH_EXACT,
    SEARCHABLE_KINDS,
    STATUS_ACTIVE,
    STATUS_CONFLICT,
    STATUS_DELETED,
    STATUS_QUARANTINED,
    STATUS_STALE,
    Candidate,
    EnvFingerprint,
    MemoryRecord,
    ScoreParams,
)

logger = get_logger("memory.retriever")

# 参与打分的状态：过期与冲突仍可召回，但会被降权/带提示
_RETRIEVABLE = (STATUS_ACTIVE, STATUS_STALE, STATUS_CONFLICT)


@dataclass
class RetrievalResult:
    """一次检索的完整结果，供注入块、界面与实验记录三方共用。"""

    query: str = ""
    candidates: List[Candidate] = field(default_factory=list)
    excluded: List[Candidate] = field(default_factory=list)
    conflicts: List[ConflictGroup] = field(default_factory=list)
    hint: str = ""
    stale_note: str = ""
    scanned: int = 0
    took_ms: float = 0.0
    degraded_reason: str = ""

    @property
    def ids(self) -> List[str]:
        return [str(c.memory.get("id")) for c in self.candidates]

    @property
    def top1(self) -> Optional[Candidate]:
        return self.candidates[0] if self.candidates else None

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)

    @property
    def unresolved_conflicts(self) -> List[ConflictGroup]:
        return [g for g in self.conflicts if g.unresolved]

    def top(self, k: int) -> List[Candidate]:
        return self.candidates[:k]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "ids": self.ids,
            "candidates": [
                {
                    **c.to_dict(),
                    "title": c.memory.get("title", ""),
                    "kind": c.memory.get("kind", ""),
                    "status": c.memory.get("status", ""),
                    "levels": c.memory.get("levels", {}),
                    "conflict_with": c.memory.get("conflict_with", []),
                }
                for c in self.candidates
            ],
            "excluded": [
                {
                    "memory_id": c.memory.get("id"),
                    "title": c.memory.get("title", ""),
                    "reason": c.exclude_reason,
                }
                for c in self.excluded
            ],
            "conflicts": [g.to_dict() for g in self.conflicts],
            "hint": self.hint,
            "stale_note": self.stale_note,
            "scanned": self.scanned,
            "took_ms": round(self.took_ms, 3),
            "degraded_reason": self.degraded_reason,
        }


class Retriever:
    """两步检索器。store 变化后自动重建索引，也可显式 reload。"""

    def __init__(
        self,
        store: MemoryStore,
        params: Optional[ScoreParams] = None,
        k: int = 10,
        arbiter: Optional[Arbiter] = None,
        now: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.params = params or ScoreParams()
        self.k = k
        self.scorer = Scorer(self.params)
        self.arbiter = arbiter or Arbiter(store=store)
        self._index = BM25Index()
        self._records: List[MemoryRecord] = []
        self._envs: Dict[str, EnvFingerprint] = {}
        self._signature: Optional[tuple] = None
        self._now = now

    # ── 索引 ─────────────────────────────────────────────────────────
    def _db_signature(self) -> tuple:
        """用「条数 + 最大更新时间」判断是否需要重建索引，一次查询搞定。"""
        try:
            row = self.store.conn.execute(
                "SELECT COUNT(*) AS c, MAX(updated_at) AS m FROM memories"
            ).fetchone()
            return (int(row["c"]), row["m"])
        except Exception:  # 库不可用时退化，让上层决定是否降级
            return (0, None)

    def invalidate(self) -> None:
        self._signature = None

    def reload(self) -> None:
        """强制重建索引。写入侧改动后调用。"""
        self._signature = None
        self._ensure_index()

    def _ensure_index(self) -> None:
        sig = self._db_signature()
        if sig == self._signature and self._index is not None:
            return

        records = self.store.list(limit=100000)
        index = BM25Index()
        kept: List[MemoryRecord] = []
        for rec in records:
            if rec.status == STATUS_DELETED or rec.status == STATUS_QUARANTINED:
                continue
            if rec.kind not in SEARCHABLE_KINDS:
                continue
            text = " ".join(
                filter(
                    None,
                    [rec.title, rec.symptom, rec.diagnosis, rec.solution],
                )
            )
            index.add(rec.id, text)
            kept.append(rec)
        index.build()

        self._index = index
        self._records = kept
        self._envs = self.store.all_envs()
        self._signature = sig

    @property
    def size(self) -> int:
        self._ensure_index()
        return len(self._records)

    # ── 第一步：硬性适用性检查 ───────────────────────────────────────
    def hard_filter(
        self, cands: Sequence[Candidate], fp: Optional[EnvFingerprint]
    ) -> List[Candidate]:
        """剔除架构/关键依赖不兼容的候选。单独暴露，便于单测与消融实验。"""
        survivors: List[Candidate] = []
        for cand in cands:
            levels = cand.memory.get("levels") or {}
            blocked, reason = is_hard_incompatible(levels)
            if blocked:
                cand.excluded = True
                cand.exclude_reason = reason
                continue
            survivors.append(cand)
        return survivors

    # ── 主流程 ───────────────────────────────────────────────────────
    def search(
        self,
        query: str,
        fp: Optional[EnvFingerprint] = None,
        k: Optional[int] = None,
        kinds: Optional[Iterable[str]] = None,
        session_id: str = "",
        include_stale: bool = True,
        include_quarantined: bool = False,
        detect_conflicts: bool = True,
        hard_filter: bool = True,
    ) -> RetrievalResult:
        started = time.perf_counter()
        top_k = k or self.k
        result = RetrievalResult(query=query)

        try:
            self._ensure_index()
        except Exception as exc:
            result.degraded_reason = f"记忆库不可用：{exc}"
            logger.warning("记忆索引不可用，检索降级：%s", exc)
            return result

        allowed_kinds = set(kinds) if kinds else set(SEARCHABLE_KINDS)
        result.scanned = len(self._records)

        cands: List[Candidate] = []
        for rec in self._records:
            if rec.kind not in allowed_kinds:
                continue
            if rec.status == STATUS_QUARANTINED and not include_quarantined:
                continue
            if rec.status == STATUS_STALE and not include_stale:
                continue
            if rec.status not in _RETRIEVABLE and not include_quarantined:
                continue

            env = self._envs.get(rec.id)
            if env is None:
                # 无指纹：无法判断适用性，按 unknown 处理，不硬性排除
                levels = {k2: "unknown" for k2 in ("os", "arch", "kernel", "deps")}
                has_dep_req = False
            else:
                levels = match(env, fp) if fp is not None else {
                    k2: "unknown" for k2 in ("os", "arch", "kernel", "deps")
                }
                has_dep_req = bool(env.deps)

            cands.append(
                Candidate(
                    memory={
                        "id": rec.id,
                        "kind": rec.kind,
                        "title": rec.title,
                        "symptom": rec.symptom,
                        "diagnosis": rec.diagnosis,
                        "solution": rec.solution,
                        "status": rec.status,
                        "version": rec.version,
                        "success_count": rec.success_count,
                        "fail_count": rec.fail_count,
                        "verify_rate": rec.verify_rate,
                        "last_verified_at": rec.last_verified_at,
                        "created_at": rec.created_at,
                        "conditions": dict(rec.conditions),
                        "provenance": dict(rec.provenance),
                        "env": env.to_dict() if env else {},
                        "levels": levels,
                        "has_dep_requirement": has_dep_req,
                    }
                )
            )

        # 1) 硬性检查前置
        if hard_filter:
            survivors = self.hard_filter(cands, fp)
            result.excluded = [c for c in cands if c.excluded]
        else:
            survivors = list(cands)

        if not survivors:
            result.took_ms = (time.perf_counter() - started) * 1000
            return result

        # 2) BM25 召回（先粗排取 Top-N，避免全量精算）
        raw = {c.memory["id"]: self._index.score(query, str(c.memory["id"])) for c in survivors}
        positives = [(mid, s) for mid, s in raw.items() if s > 0]
        positives.sort(key=lambda x: (-x[1], x[0]))
        shortlist_ids = {mid for mid, _ in positives[: max(top_k * 3, top_k)]}

        shortlisted = [
            c for c in survivors if str(c.memory["id"]) in shortlist_ids
        ] or survivors[:top_k]
        # 语义分为 0 的也保留少量候选（字面没命中但环境完全一致的常识类记忆）
        if len(shortlisted) < top_k:
            seen = {id(c) for c in shortlisted}
            for c in survivors:
                if id(c) not in seen:
                    shortlisted.append(c)
                    seen.add(id(c))
                if len(shortlisted) >= top_k * 2:
                    break

        top_raw = max((raw.get(str(c.memory["id"]), 0.0) for c in shortlisted), default=0.0)

        # 3) 四因子 + 4) 乘性惩罚
        for cand in shortlisted:
            mid = str(cand.memory["id"])
            self.scorer.apply(
                cand,
                sim=normalize_sim(raw.get(mid, 0.0), top_raw),
                levels=cand.memory.get("levels") or {},
                has_dep_requirement=bool(cand.memory.get("has_dep_requirement")),
            )
            cand.memory["hits"] = explained_hits(query, self._doc_text(cand), top=5)
            cand.memory["explain"] = self.scorer.explain(cand)

        shortlisted.sort(key=lambda c: (-c.score, str(c.memory.get("id"))))

        # 5) 冲突检出与仲裁
        if detect_conflicts:
            top_for_conflict = shortlisted[: max(top_k, 5)]
            ranked, conflicts = self.arbiter.apply(top_for_conflict)
            result.conflicts = conflicts
        else:
            ranked = shortlisted

        result.candidates = ranked[:top_k]
        result.hint = self._build_hint(result)
        stale_recs = [
            r
            for r in (
                self.store.get(str(c.memory.get("id"))) for c in result.candidates
            )
            if r is not None and r.status == STATUS_STALE
        ]
        result.stale_note = stale_hint(stale_recs)
        result.took_ms = (time.perf_counter() - started) * 1000
        return result

    # ── 辅助 ─────────────────────────────────────────────────────────
    @staticmethod
    def _doc_text(cand: Candidate) -> str:
        m = cand.memory
        return " ".join(
            str(m.get(k2, "")) for k2 in ("title", "symptom", "diagnosis", "solution")
        )

    def _build_hint(self, result: RetrievalResult) -> str:
        parts: List[str] = []

        if result.excluded:
            reasons = sorted({c.exclude_reason for c in result.excluded})
            parts.append(
                f"已前置排除 {len(result.excluded)} 条不适用经验（{'；'.join(reasons)}）"
            )

        mismatch: List[str] = []
        for cand in result.candidates:
            levels = cand.memory.get("levels") or {}
            if any(v not in (MATCH_EXACT, "unknown") for v in levels.values()):
                mismatch.append(describe(levels))
        if mismatch:
            uniq = sorted(set(mismatch))
            parts.append("环境差异：" + "；".join(uniq[:2]))

        if result.unresolved_conflicts:
            parts.append(
                f"检出 {len(result.unresolved_conflicts)} 组结论冲突且无法仲裁，"
                "已并列呈现，需人工确认，系统不代为选择"
            )
        elif result.conflicts:
            parts.append(f"检出 {len(result.conflicts)} 组结论冲突，已按四级规则标注依据")

        if result.stale_note:
            parts.append(result.stale_note)

        return "。".join(parts)
