"""四级仲裁单元测试

判定点只有一条：**四级规则用尽就不能选**。
自动择一率为 0 是硬性指标，测试必须能把它钉住。
"""
from __future__ import annotations

import pytest

from memory.arbitration import (
    LEVEL_ENV,
    LEVEL_RECENT,
    LEVEL_SUCCESS,
    LEVEL_TIE,
    Arbiter,
)
from memory.types import Candidate


def cand(mid, levels, success=0, fail=0, verified="2026-11-10T00:00:00+00:00", solution="s"):
    return Candidate(
        memory={
            "id": mid,
            "title": f"经验 {mid}",
            "symptom": "nginx 启动失败，80 端口被占用",
            "solution": solution,
            "success_count": success,
            "fail_count": fail,
            "last_verified_at": verified,
            "levels": levels,
            "status": "active",
        }
    )


EXACT = {"os": "exact", "arch": "exact", "kernel": "exact", "deps": "exact"}
MINOR = {"os": "minor", "arch": "exact", "kernel": "minor", "deps": "exact"}


def test_level1_env_decides():
    a = cand("a", EXACT, success=1)
    b = cand("b", MINOR, success=9)
    r = Arbiter().arbitrate([a, b])
    assert r.level == LEVEL_ENV
    assert r.winner is a
    assert r.auto_pick is False


def test_level2_verify_count_decides():
    a = cand("a", EXACT, success=5, fail=0)
    b = cand("b", EXACT, success=1, fail=0)
    r = Arbiter().arbitrate([a, b])
    assert r.level == LEVEL_SUCCESS
    assert r.winner is a


def test_level3_recent_decides():
    a = cand("a", EXACT, success=3, verified="2026-11-15T00:00:00+00:00")
    b = cand("b", EXACT, success=3, verified="2026-01-01T00:00:00+00:00")
    r = Arbiter().arbitrate([a, b])
    assert r.level == LEVEL_RECENT
    assert r.winner is a


def test_level4_tie_never_picks():
    a = cand("a", EXACT, success=3, verified="2026-11-15T00:00:00+00:00")
    b = cand("b", EXACT, success=3, verified="2026-11-15T00:00:00+00:00")
    r = Arbiter().arbitrate([a, b])
    assert r.level == LEVEL_TIE
    assert r.winner is None
    assert r.tied is True
    assert r.auto_pick is False


def test_single_candidate_is_not_a_conflict():
    a = cand("a", EXACT, success=1)
    r = Arbiter().arbitrate([a])
    assert r.winner is a
    assert r.auto_pick is False


def test_detect_divergent_solutions():
    a = cand("a", EXACT, solution="kill 占用进程后重启 nginx")
    b = cand("b", EXACT, solution="修改监听端口为 8080 并 reload")
    groups = Arbiter().detect([a, b])
    assert len(groups) == 1
    assert groups[0].memory_ids == ["a", "b"]
    assert groups[0].unresolved is True


def test_same_solution_is_not_conflict():
    a = cand("a", EXACT, solution="kill 占用进程后重启 nginx")
    b = cand("b", EXACT, solution="kill 占用进程后重启 nginx")
    assert Arbiter().detect([a, b]) == []


def test_conflict_marks_candidates():
    a = cand("a", EXACT, solution="kill 占用进程后重启 nginx")
    b = cand("b", EXACT, solution="修改监听端口为 8080 并 reload")
    ranked, conflicts = Arbiter().apply([a, b], persist=False)
    assert conflicts
    assert ranked[0].memory["conflict_with"]
    assert ranked[0].memory["conflict_dimension"] == "unresolved"


def test_auto_pick_rate_is_zero_over_many_ties():
    """构造 20 组严格并列的样本，自动择一必须一次都不发生。"""
    arb = Arbiter()
    picks = 0
    for i in range(20):
        group = [
            cand(f"a{i}", EXACT, success=2, verified="2026-11-15T00:00:00+00:00"),
            cand(f"b{i}", EXACT, success=2, verified="2026-11-15T00:00:00+00:00"),
        ]
        r = arb.arbitrate(group)
        picks += 1 if r.auto_pick else 0
    assert picks == 0
