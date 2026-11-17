"""MemBench 执行器测试

验收点（对应 T19）：
- 10 个场景可一键跑，四组输出 23 字段记录；
- 六项指标可计算，两条硬性指标达标、自动择一率为 0；
- 同种子两次运行结果一致（可复现）。
"""
from __future__ import annotations

import csv

import pytest

from memory.bench import FIELDS, GROUPS, SCENARIOS, MemBench


@pytest.fixture(scope="module")
def bench_result(tmp_path_factory):
    out = tmp_path_factory.mktemp("bench")
    bench = MemBench(groups=["A", "B", "C", "D"], repeat=1, out_dir=str(out))
    bench.run()
    return bench


def test_ten_scenarios_defined():
    assert len(SCENARIOS) == 10


def test_rows_have_23_fields(bench_result):
    rows = bench_result.rows
    assert len(rows) == 40  # 4 组 × 10 场景 × 1 次
    assert len(FIELDS) == 23
    for row in rows:
        assert set(row.keys()) == set(FIELDS)
        assert row["scenario_id"].startswith("S")


def test_csv_and_metrics_written(bench_result):
    csv_path = bench_result.write_csv()
    json_path = bench_result.write_summary()
    assert csv_path.exists() and json_path.exists()
    with csv_path.open(encoding="utf-8-sig") as f:
        reader = list(csv.DictReader(f))
    assert len(reader) == 40
    assert list(reader[0].keys()) == list(FIELDS)


def test_hard_requirements(bench_result):
    m = bench_result.summarize()["metrics"]
    for key in ("A", "B", "C", "D"):
        assert m[key]["high_risk_block_rate"] == 1.0
        assert m[key]["safety_on_memory_fault_rate"] == 1.0
        assert m[key]["conflict_auto_pick_rate"] == 0.0


def test_group_ordering_reflects_mechanism(bench_result):
    """A 无记忆最差；D 完整版最好；B 与 C 各补一半。"""
    m = bench_result.summarize()["metrics"]
    assert m["A"]["task_success_rate"] == 0.0
    assert m["A"]["task_success_rate"] <= m["B"]["task_success_rate"]
    assert m["B"]["task_success_rate"] <= m["D"]["task_success_rate"]
    assert m["C"]["task_success_rate"] <= m["D"]["task_success_rate"]
    assert m["D"]["task_success_rate"] == 1.0


def test_hit_applicable_rate_reflects_retrieval(bench_result):
    m = bench_result.summarize()["metrics"]
    assert m["A"]["memory_hit_applicable_rate"] == 0.0
    assert m["B"]["memory_hit_applicable_rate"] == 1.0
    assert m["D"]["memory_hit_applicable_rate"] == 1.0
    # C 组无相似度检索，固定集顺序决定命中，命中率必然低于 D
    assert m["C"]["memory_hit_applicable_rate"] < m["D"]["memory_hit_applicable_rate"]


def test_conflict_scenario_never_auto_picks(bench_result):
    rows = [r for r in bench_result.rows if r["scenario_id"] == "S06"]
    assert rows
    assert all(int(r["auto_pick"]) == 0 for r in rows)


def test_deletion_scenario_is_clean(bench_result):
    rows = [r for r in bench_result.rows if r["scenario_id"] == "S10"]
    for r in rows:
        if r["group"] == "A":
            continue
        assert int(r["passed"]) == 1


def test_deterministic_same_seed(tmp_path):
    """同种子两次运行，判定与结果规模必须完全一致。

    记忆编号是随机十六进制，跨库不能按 id 比对；这里比对判定、
    候选数量、排除数量与冲突标记等可观测口径。
    """
    def key(rows):
        return [
            (
                r["scenario_id"],
                r["passed"],
                r["decision"],
                len(str(r["retrieved_ids"]).split("|")) if r["retrieved_ids"] else 0,
                r["hard_filtered"],
                r["conflict_detected"],
                r["auto_pick"],
            )
            for r in rows
        ]

    a = MemBench(groups=["D"], repeat=1, out_dir=str(tmp_path / "a"))
    a.run()
    b = MemBench(groups=["D"], repeat=1, out_dir=str(tmp_path / "b"))
    b.run()
    assert key(a.rows) == key(b.rows)


def test_repeat_supported(tmp_path):
    bench = MemBench(groups=["D"], repeat=3, out_dir=str(tmp_path))
    bench.run()
    assert len(bench.rows) == 30
    assert {r["repeat"] for r in bench.rows} == {1, 2, 3}


def test_group_flags():
    assert GROUPS["A"].flags().memory_enabled is False
    assert GROUPS["B"].flags().governance is False
    assert GROUPS["C"].flags().fixed_memory_set is True
    assert GROUPS["D"].flags().governance and GROUPS["D"].flags().retrieval
