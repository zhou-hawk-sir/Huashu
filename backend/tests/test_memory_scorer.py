"""打分与惩罚单元测试

核心验证两条：
1. 同义表述能通过症状归一被召回；
2. 乘性惩罚能让「高相似度但不适用」的经验排到「低相似度但适用」之后，
   而纯加法做不到——这是设计成立与否的判定点。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from memory.scorer import BM25Index, Scorer, normalize_sim
from memory.types import STATUS_STALE, Candidate, ScoreParams


# ── BM25 ──────────────────────────────────────────────────────────────
@pytest.fixture()
def index() -> BM25Index:
    idx = BM25Index()
    idx.add("m1", "ollama 服务启动失败，11434 端口无响应，systemctl status 显示 failed")
    idx.add("m2", "磁盘空间不足，/var 分区写满，no space left on device")
    idx.add("m3", "内存持续增长触发 oom，进程被内核杀掉")
    idx.build()
    return idx


def test_bm25_recalls_relevant_doc(index):
    hits = index.search("11434 端口 无响应")
    assert hits
    assert hits[0][0] == "m1"


def test_bm25_synonym_recall(index):
    """「起不来」应能召回写作「启动失败」的经验，靠的是症状归一。"""
    hits = [d for d, _ in index.search("ollama 起不来")]
    assert "m1" in hits


def test_bm25_unrelated_query_returns_empty(index):
    assert index.search("打印机 驱动 安装") == []


def test_bm25_score_is_ordered(index):
    top = index.search("磁盘 空间不足")[0]
    assert top[0] == "m2"
    assert top[1] > 0


def test_normalize_sim_bounds():
    assert normalize_sim(5, 10) == 0.5
    assert normalize_sim(20, 10) == 1.0
    assert normalize_sim(1, 0) == 0.0


# ── 四因子 ────────────────────────────────────────────────────────────
def test_env_score_ignores_os_and_arch():
    s = Scorer()
    levels = {"os": "none", "arch": "none", "kernel": "exact", "deps": "exact"}
    # OS 与架构不进 Env 项，避免与 penalty 重复扣分
    assert s.env_score(levels, has_dep_requirement=True) == 1.0


def test_env_score_kernel_only_when_no_dep_requirement():
    s = Scorer()
    levels = {"os": "exact", "arch": "exact", "kernel": "minor", "deps": "none"}
    assert s.env_score(levels, has_dep_requirement=False) == 0.70


def test_freshness_decays_by_half_life():
    s = Scorer(ScoreParams(freshness_half_life_days=90))
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    old = (now - timedelta(days=90)).isoformat()
    assert s.freshness(old, now) == pytest.approx(0.5, abs=1e-6)
    assert s.freshness(now.isoformat(), now) == pytest.approx(1.0, abs=1e-6)
    assert s.freshness(None, now) == 0.5


def test_freshness_handles_bad_timestamp():
    assert Scorer().freshness("not-a-date") == 0.5


# ── 惩罚 ──────────────────────────────────────────────────────────────
def test_no_penalty_when_environment_matches():
    s = Scorer()
    assert s.penalties({"os": "exact", "arch": "exact", "kernel": "exact", "deps": "exact"},
                       "active") == {}


def test_penalties_stack_multiplicatively():
    s = Scorer()
    pen = s.penalties({"os": "major", "arch": "exact", "kernel": "exact", "deps": "exact"},
                      "active")
    assert pen == {"os": 0.30}


def test_stale_adds_penalty():
    s = Scorer()
    pen = s.penalties({"os": "exact", "arch": "exact", "kernel": "exact", "deps": "exact"},
                      STATUS_STALE)
    assert pen == {"stale": 0.50}


def test_arch_mismatch_is_near_exclusion():
    s = Scorer()
    pen = s.penalties({"os": "exact", "arch": "none", "kernel": "exact", "deps": "exact"},
                      "active")
    assert pen["arch"] == 0.10


# ── 关键判定：乘性 vs 加性 ────────────────────────────────────────────
def test_multiplicative_penalty_beats_additive():
    """高相似度但不适用 应输给 低相似度但适用。

    若只做加法（不乘惩罚），前者必然排第一，这正是要避免的错误排序。
    """
    s = Scorer()

    hot_but_incompatible = Candidate(memory={"id": "hot", "verify_rate": 1.0, "status": "active"})
    s.apply(hot_but_incompatible, sim=1.0,
            levels={"os": "major", "arch": "exact", "kernel": "exact", "deps": "exact"},
            has_dep_requirement=False)

    cold_but_compatible = Candidate(memory={"id": "cold", "verify_rate": 0.5, "status": "active"})
    s.apply(cold_but_compatible, sim=0.5,
            levels={"os": "exact", "arch": "exact", "kernel": "exact", "deps": "exact"},
            has_dep_requirement=False)

    # 加法部分：不适用项确实更高
    add_hot = 0.4 * 1.0 + 0.3 * 1.0 + 0.2 * 1.0 + 0.1 * hot_but_incompatible.freshness
    add_cold = 0.4 * 0.5 + 0.3 * 1.0 + 0.2 * 0.5 + 0.1 * cold_but_compatible.freshness
    assert add_hot > add_cold

    # 乘性之后排序反转
    assert hot_but_incompatible.score < cold_but_compatible.score


def test_arch_mismatch_falls_behind_everything():
    s = Scorer()
    bad = Candidate(memory={"id": "bad", "verify_rate": 1.0, "status": "active"})
    s.apply(bad, sim=1.0,
            levels={"os": "exact", "arch": "none", "kernel": "exact", "deps": "exact"},
            has_dep_requirement=False)
    assert bad.score <= 0.1 + 1e-9


def test_score_is_reproducible():
    s = Scorer()
    levels = {"os": "minor", "arch": "exact", "kernel": "exact", "deps": "exact"}
    a = Candidate(memory={"id": "x", "verify_rate": 0.6, "status": "active"})
    b = Candidate(memory={"id": "x", "verify_rate": 0.6, "status": "active"})
    assert s.apply(a, 0.7, levels, True).score == s.apply(b, 0.7, levels, True).score


def test_explain_mentions_factors_and_penalty():
    s = Scorer()
    c = Candidate(memory={"id": "x", "verify_rate": 1.0, "status": "stale"})
    s.apply(c, 0.9, {"os": "exact", "arch": "exact", "kernel": "exact", "deps": "exact"}, False)
    text = s.explain(c)
    assert "语义" in text and "环境" in text and "惩罚" in text and "stale" in text
