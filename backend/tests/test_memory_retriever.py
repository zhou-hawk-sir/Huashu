"""检索编排单元测试

重点验证三件事：
1. 硬性检查前置：语义 0.95 但架构不兼容的样本不能排第一；
2. 乘性惩罚有效：关掉惩罚能复现错误排序，打开能纠正；
3. 同输入两次结果完全一致（可复现）。
"""
from __future__ import annotations

import pytest

from memory.retriever import Retriever
from memory.schema import connect
from memory.service import MemoryConfig, MemoryService
from memory.store import MemoryStore
from memory.types import EnvFingerprint, MemoryRecord, ScoreParams

CUR = EnvFingerprint(
    os_id="kylin", os_version="v11", os_sp="sp3",
    kernel="4.19.90-25.10", arch="x86_64",
    deps={"nginx": "1.24", "python3": "3.11"},
    captured_at="2026-11-17T00:00:00+00:00",
)

LOONG = EnvFingerprint(
    os_id="kylin", os_version="v11", os_sp="sp3",
    kernel="4.19.90-25.10", arch="loongarch64",
    deps={"nginx": "1.24", "python3": "3.11"},
    captured_at="2026-11-17T00:00:00+00:00",
)

OLD_OS = EnvFingerprint(
    os_id="kylin", os_version="v10", os_sp="sp1",
    kernel="4.19.90-24.6", arch="x86_64",
    deps={"nginx": "1.20", "python3": "3.9"},
    captured_at="2026-01-01T00:00:00+00:00",
)


def make_store(rows):
    store = MemoryStore(conn=connect(":memory:"))
    for rec, env, succ in rows:
        rec.success_count = succ
        rec.last_verified_at = "2026-11-15T00:00:00+00:00"
        mid = store.add(rec)
        if env is not None:
            store.put_env(mid, env)
    return store


def rec(title, symptom, solution, kind="episodic", status="active"):
    return MemoryRecord(
        id="", kind=kind, title=title, symptom=symptom,
        diagnosis="", solution=solution, status=status,
    )


@pytest.fixture()
def store_with_arch_conflict():
    return make_store(
        [
            (rec("重装 loongarch 包", "ollama 起不来 11434 端口无响应",
                 "重新下载 loongarch64 包覆盖"), LOONG, 20),
            (rec("重启服务", "ollama 起不来 11434 端口无响应",
                 "systemctl restart ollama"), CUR, 1),
        ]
    )


def test_hard_filter_excludes_incompatible_arch(store_with_arch_conflict):
    r = Retriever(store_with_arch_conflict, k=5)
    res = r.search("ollama 起不来 11434 端口无响应", fp=CUR)
    excluded_ids = [c.memory["id"] for c in res.excluded]
    assert len(excluded_ids) == 1
    assert res.excluded[0].exclude_reason == "CPU 架构不兼容"
    assert all(c.memory["id"] not in excluded_ids for c in res.candidates)
    assert res.candidates[0].memory["title"] == "重启服务"


def test_hard_filter_can_be_disabled_for_ablation(store_with_arch_conflict):
    """关掉硬性检查，不兼容项会重新出现在候选里；打开时它根本进不了排序。

    这是消融实验的判定点：惩罚只能把它压低，不能把它排除。
    """
    r = Retriever(store_with_arch_conflict, k=5)
    on = r.search("ollama 起不来 11434 端口无响应", fp=CUR)
    off = r.search("ollama 起不来 11434 端口无响应", fp=CUR, hard_filter=False)

    bad_id = on.excluded[0].memory["id"]
    assert off.excluded == []                       # 关闭后不再排除
    assert len(off.candidates) == 2                 # 两条都进了排序
    assert bad_id in [c.memory["id"] for c in off.candidates]
    assert bad_id not in on.ids                     # 打开后彻底出局
    assert len(on.excluded) == 1


def test_penalty_moves_incompatible_below_compatible():
    """同环境除 OS 主版本外完全相同：关惩罚时高成功率那条靠前，开惩罚后掉下去。"""
    same_kernel = "4.19.90-25.10"
    deps = {"nginx": "1.24", "python3": "3.11"}
    old = EnvFingerprint(
        os_id="kylin", os_version="v10", os_sp="sp1", kernel=same_kernel,
        arch="x86_64", deps=deps, captured_at="2026-01-01T00:00:00+00:00",
    )
    store = make_store(
        [
            (rec("旧版本安装方式", "ollama 起不来 11434 端口无响应",
                 "旧系统专用安装脚本 install.sh"), old, 30),
            (rec("新版本安装方式", "ollama 起不来 11434 端口无响应",
                 "当前系统安装脚本 setup.sh"), CUR, 2),
        ]
    )
    # CUR 那条成功率压低，让「关惩罚」时确实由旧版本胜出
    for r in store.list():
        if r.title == "新版本安装方式":
            store.update(r.id, {"success_count": 1, "fail_count": 1})

    on = Retriever(store, k=5)
    top_on = on.search("ollama 起不来 11434 端口无响应", fp=CUR).candidates[0]

    p = ScoreParams(penalty_os=1.0, penalty_kernel=1.0, penalty_dep=1.0)
    off = Retriever(store, params=p, k=5)
    top_off = off.search("ollama 起不来 11434 端口无响应", fp=CUR).candidates[0]

    assert top_off.memory["title"] == "旧版本安装方式"
    assert top_on.memory["title"] == "新版本安装方式"
    assert top_on.score < top_off.score


def test_search_is_deterministic():
    store = make_store(
        [
            (rec("A", "磁盘空间不足 /var 写满", "journalctl --vacuum-size=200M"), CUR, 3),
            (rec("B", "磁盘空间不足 /var 写满", "rm -rf /var/log/*.log"), CUR, 3),
            (rec("C", "ollama 起不来", "systemctl restart ollama"), CUR, 1),
        ]
    )
    r = Retriever(store, k=5)
    a = r.search("磁盘空间不足", fp=CUR, detect_conflicts=False)
    b = r.search("磁盘空间不足", fp=CUR, detect_conflicts=False)
    assert a.ids == b.ids
    assert [round(c.score, 6) for c in a.candidates] == [
        round(c.score, 6) for c in b.candidates
    ]


def test_status_filters():
    store = make_store(
        [
            (rec("过期项", "磁盘空间不足", "清日志", status="stale"), CUR, 3),
            (rec("隔离项", "磁盘空间不足", "清日志", status="quarantined"), CUR, 3),
            (rec("删除项", "磁盘空间不足", "清日志", status="deleted"), CUR, 3),
        ]
    )
    r = Retriever(store, k=5)
    res = r.search("磁盘空间不足", fp=CUR, detect_conflicts=False)
    titles = [c.memory["title"] for c in res.candidates]
    assert titles == ["过期项"]
    assert res.candidates[0].penalties.get("stale") == pytest.approx(0.5)
    assert res.stale_note


def test_working_memory_only_visible_in_same_session():
    svc = MemoryService(config=MemoryConfig(db_path=":memory:"), conn=connect(":memory:"))
    svc.writer.write_working("本次会话上下文", "用户正在排查 ollama", session_id="s1")

    hit = svc.retrieve("ollama 排查", session_id="s1")
    assert any(c.memory["kind"] == "working" for c in hit.candidates)

    other = svc.retrieve("ollama 排查", session_id="s2")
    assert not any(c.memory["kind"] == "working" for c in other.candidates)


def test_empty_store_returns_empty_result():
    store = MemoryStore(conn=connect(":memory:"))
    res = Retriever(store).search("任意查询", fp=CUR)
    assert res.candidates == []
    assert res.ids == []
