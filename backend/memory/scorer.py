"""检索打分：BM25 召回 + 四因子加权 + 乘性惩罚

三层结构，顺序不可调换：
    BM25         先按词项相关度召回候选，解决「找得到」；
    四因子加权    在候选内排序，解决「哪个更该用」；
    乘性惩罚      对不适用项做折扣，解决「不适用的不能被高分掩盖」。

关于乘性而非加性：加权平均会掩盖短板。一条语义相似度 0.95、历史成功率 100%
但来自不同 OS 版本的经验，其短板足以被长板抵消。乘一个系数则不成立——
无论长板多高，结果都会被压下去。

关于 Env 项与 penalty 的分工：两者不重复扣同一个缺陷。
    Env 只算连续可比的维度（内核小版本、依赖版本）；
    OS 大版本与 CPU 架构这类离散硬约束只走 penalty。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .tokenizer import tokens
from .types import (
    MATCH_EXACT,
    MATCH_MAJOR,
    MATCH_MINOR,
    MATCH_NONE,
    STATUS_STALE,
    Candidate,
    EnvFingerprint,
    ScoreParams,
)

# 连续维度的分级得分（用于 Env 项）
_KERNEL_SCORE = {MATCH_EXACT: 1.0, MATCH_MINOR: 0.70, MATCH_MAJOR: 0.40, MATCH_NONE: 0.0}
_DEPS_SCORE = {MATCH_EXACT: 1.0, MATCH_MINOR: 0.60, MATCH_MAJOR: 0.20, MATCH_NONE: 0.0}

# 次版本不适用时的折扣（主版本用配置里的基准值，见 ScoreParams）
_OS_MINOR_RATIO = 0.70
_KERNEL_MINOR_RATIO = 0.85
_DEPS_MINOR_RATIO = 0.85


# ── BM25 ──────────────────────────────────────────────────────────────
class BM25Index:
    """最小可用的 BM25 索引。

    数据量在千条级，全量遍历足够；不引入倒排结构与近似最近邻。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: Dict[str, Dict[str, int]] = {}
        self._len: Dict[str, int] = {}
        self._df: Dict[str, int] = {}
        self._avgdl: float = 0.0
        self._built = False

    def add(self, doc_id: str, text: str) -> None:
        tf: Dict[str, int] = {}
        for t in tokens(text):
            tf[t] = tf.get(t, 0) + 1
        self._docs[doc_id] = tf
        self._len[doc_id] = sum(tf.values())
        self._built = False

    def build(self) -> None:
        df: Dict[str, int] = {}
        for tf in self._docs.values():
            for t in tf:
                df[t] = df.get(t, 0) + 1
        self._df = df
        total = sum(self._len.values())
        self._avgdl = total / len(self._docs) if self._docs else 0.0
        self._built = True

    def _idf(self, term: str) -> float:
        n = len(self._docs)
        df = self._df.get(term, 0)
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def score(self, query: str, doc_id: str) -> float:
        if not self._built:
            self.build()
        tf = self._docs.get(doc_id)
        if not tf:
            return 0.0
        dl = self._len.get(doc_id, 0) or 1
        avgdl = self._avgdl or 1.0
        total = 0.0
        for term in tokens(query):
            f = tf.get(term, 0)
            if f == 0:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * dl / avgdl)
            total += self._idf(term) * (f * (self.k1 + 1)) / denom
        return total

    def search(self, query: str, k: int = 10) -> List[Tuple[str, float]]:
        if not self._built:
            self.build()
        scored = [(did, self.score(query, did)) for did in self._docs]
        scored = [(d, s) for d, s in scored if s > 0]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:k]

    def max_score(self, query: str) -> float:
        """归一化用：当前查询在语料上的最高分。"""
        return max((self.score(query, d) for d in self._docs), default=0.0)

    def __len__(self) -> int:
        return len(self._docs)


# ── 打分器 ────────────────────────────────────────────────────────────
class Scorer:
    """四因子打分与乘性惩罚。"""

    def __init__(self, params: Optional[ScoreParams] = None) -> None:
        self.p = params or ScoreParams()

    # 单因子
    def env_score(self, levels: Dict[str, str], has_dep_requirement: bool) -> float:
        """环境匹配度，只计连续维度。

        OS 与架构不进这里：它们是离散硬约束，由 penalty 处理，避免重复扣分。
        """
        k = _KERNEL_SCORE.get(levels.get("kernel", MATCH_NONE), 0.0)
        d = _DEPS_SCORE.get(levels.get("deps", MATCH_NONE), 0.0)
        if not has_dep_requirement:
            return round(k, 6)
        return round(0.5 * k + 0.5 * d, 6)

    def freshness(self, last_verified_at: Optional[str], now: Optional[datetime] = None) -> float:
        """按半衰期衰减。无时间戳时返回 0.5，表示「无从判断」。"""
        if not last_verified_at:
            return 0.5
        try:
            ts = datetime.fromisoformat(last_verified_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return 0.5
        cur = now or datetime.now(timezone.utc)
        days = max(0.0, (cur - ts).total_seconds() / 86400.0)
        return round(math.exp(-math.log(2) * days / self.p.freshness_half_life_days), 6)

    def penalties(self, levels: Dict[str, str], status: str) -> Dict[str, float]:
        """返回生效的惩罚系数。未触发的维度不出现在结果里，便于解释。

        主版本/架构/内核/依赖不匹配时的折扣值来自配置（ScoreParams），
        次版本用固定比值——次版本差异的轻重是结构问题，不该由调参决定。
        系数为 1.0 的维度不出现在结果里，避免「看起来扣了其实没扣」。
        """
        out: Dict[str, float] = {}

        os_level = levels.get("os", MATCH_EXACT)
        if os_level == MATCH_MINOR:
            out["os"] = _OS_MINOR_RATIO
        elif os_level in (MATCH_MAJOR, MATCH_NONE):
            out["os"] = self.p.penalty_os

        if levels.get("arch", MATCH_EXACT) == MATCH_NONE:
            out["arch"] = self.p.penalty_arch

        k_level = levels.get("kernel", MATCH_EXACT)
        if k_level == MATCH_MINOR:
            out["kernel"] = _KERNEL_MINOR_RATIO
        elif k_level in (MATCH_MAJOR, MATCH_NONE):
            out["kernel"] = self.p.penalty_kernel

        d_level = levels.get("deps", MATCH_EXACT)
        if d_level == MATCH_MINOR:
            out["deps"] = _DEPS_MINOR_RATIO
        elif d_level in (MATCH_MAJOR, MATCH_NONE):
            out["deps"] = self.p.penalty_dep

        if status == STATUS_STALE:
            out["stale"] = self.p.penalty_stale

        # 1.0 等于没折扣，不写进结果
        return {k: v for k, v in out.items() if v < 1.0}

    def final_score(self, cand: Candidate) -> float:
        """(α·Sim + β·Env + γ·Success + δ·Freshness) × Π penalty"""
        p = self.p
        base = (
            p.alpha * cand.sim
            + p.beta * cand.env_score
            + p.gamma * cand.success
            + p.delta * cand.freshness
        )
        factor = 1.0
        for v in cand.penalties.values():
            factor *= v
        cand.score = round(base * factor, 6)
        return cand.score

    def apply(
        self,
        cand: Candidate,
        sim: float,
        levels: Dict[str, str],
        has_dep_requirement: bool,
    ) -> Candidate:
        """一次性填满候选的四个因子与惩罚项。"""
        cand.sim = round(sim, 6)
        cand.env_score = self.env_score(levels, has_dep_requirement)
        cand.success = float(cand.memory.get("verify_rate", 0.5))
        cand.freshness = self.freshness(cand.memory.get("last_verified_at"))
        cand.penalties = self.penalties(levels, str(cand.memory.get("status", "active")))
        self.final_score(cand)
        return cand

    def explain(self, cand: Candidate) -> str:
        """生成可读的打分说明，供界面与答辩使用。"""
        parts = [
            f"语义 {cand.sim:.2f}",
            f"环境 {cand.env_score:.2f}",
            f"成功率 {cand.success:.2f}",
            f"新鲜度 {cand.freshness:.2f}",
        ]
        if cand.penalties:
            pen = "、".join(f"{k}×{v:.2f}" for k, v in cand.penalties.items())
            parts.append(f"惩罚 {pen}")
        return f"{' · '.join(parts)} → {cand.score:.3f}"


def normalize_sim(raw: float, top: float) -> float:
    """把 BM25 原始分压到 [0,1]。同一查询内按最高分归一，保证可比。"""
    if top <= 0:
        return 0.0
    return max(0.0, min(1.0, raw / top))
