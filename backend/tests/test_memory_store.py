"""记忆存储层单元测试

覆盖：写入、读取、版本快照、验证回写、环境指纹、墓碑、事件、统计、坏数据容错。
全部为纯逻辑测试，不依赖 Linux 环境，可在任意平台执行。
"""
from __future__ import annotations

import pytest

from memory.schema import connect, get_schema_version, init_db, table_names
from memory.store import MemoryStore
from memory.types import (
    STATUS_DELETED,
    STATUS_STALE,
    EnvFingerprint,
    MemoryRecord,
)


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "memory.db"
    return MemoryStore(str(db))


def make_record(**kw) -> MemoryRecord:
    base = dict(
        id="",
        kind="episodic",
        title="ollama 服务起不来",
        symptom="11434 端口无响应，systemctl status 显示 failed",
        diagnosis="旧进程占用端口，重启前未清理",
        solution="ss -lntp 定位占用进程，确认后重启 ollama",
        conditions={"needs_confirm": True, "target": "/usr/bin/ollama"},
        provenance={"source": "session", "session_id": "s-001", "confidence": "high"},
    )
    base.update(kw)
    return MemoryRecord(**base)


# ── 建表 ──────────────────────────────────────────────────────────────
def test_schema_creates_all_tables(tmp_path):
    conn = connect(tmp_path / "m.db")
    init_db(conn)
    names = table_names(conn)
    for t in ("memories", "memory_env", "memory_versions",
              "memory_conflicts", "memory_events", "memory_tombstones"):
        assert t in names
    assert get_schema_version(conn) == 1


def test_init_db_is_idempotent(tmp_path):
    conn = connect(tmp_path / "m.db")
    init_db(conn)
    init_db(conn)
    assert get_schema_version(conn) == 1


# ── 写入与读取 ────────────────────────────────────────────────────────
def test_add_assigns_id_and_timestamps(store):
    mid = store.add(make_record())
    assert mid.startswith("mem_")
    assert len(mid) == 4 + 26

    rec = store.get(mid)
    assert rec is not None
    assert rec.title == "ollama 服务起不来"
    assert rec.version == 1
    assert rec.created_at and rec.updated_at
    assert rec.conditions["target"] == "/usr/bin/ollama"


def test_add_rejects_unknown_kind(store):
    with pytest.raises(ValueError):
        store.add(make_record(kind="unknown"))


def test_get_missing_returns_none(store):
    assert store.get("mem_not_exist") is None


def test_list_filters_by_kind_and_status(store):
    store.add(make_record(kind="episodic", title="A"))
    store.add(make_record(kind="semantic", title="B"))
    mid = store.add(make_record(kind="episodic", title="C"))
    store.set_status(mid, STATUS_STALE)

    assert store.count(kind="episodic") == 2
    assert store.count(kind="semantic") == 1
    assert store.count(status=STATUS_STALE) == 1
    assert len(store.list(kind="episodic")) == 2


# ── 版本 ──────────────────────────────────────────────────────────────
def test_update_bumps_version_and_keeps_snapshot(store):
    mid = store.add(make_record())
    v2 = store.update(mid, {"solution": "先 kill 旧进程再重启 ollama"}, "修正方案")

    assert v2 == 2
    rec = store.get(mid)
    assert rec.version == 2
    assert "kill" in rec.solution

    snaps = store.versions(mid)
    assert [s["version"] for s in snaps] == [1, 2]
    assert snaps[0]["snapshot"]["solution"].startswith("ss -lntp")


def test_update_rejects_unknown_field(store):
    mid = store.add(make_record())
    with pytest.raises(ValueError):
        store.update(mid, {"id": "hacked"})
    with pytest.raises(ValueError):
        store.update(mid, {"created_at": "2020-01-01"})


def test_update_missing_record_raises(store):
    with pytest.raises(KeyError):
        store.update("mem_none", {"title": "x"})


# ── 验证回写 ──────────────────────────────────────────────────────────
def test_record_verify_accumulates_counts(store):
    mid = store.add(make_record())
    store.record_verify(mid, ok=True)
    store.record_verify(mid, ok=True)
    store.record_verify(mid, ok=False)

    rec = store.get(mid)
    assert rec.success_count == 2
    assert rec.fail_count == 1
    assert rec.last_verified_at is not None
    assert rec.verify_rate == pytest.approx(2 / 3)


def test_verify_rate_defaults_to_half(store):
    mid = store.add(make_record())
    assert store.get(mid).verify_rate == 0.5


# ── 环境指纹 ──────────────────────────────────────────────────────────
def test_env_roundtrip(store):
    mid = store.add(make_record())
    fp = EnvFingerprint(
        os_id="kylin", os_version="v11", os_sp="sp3",
        kernel="4.19.90-25.10.v2101", arch="x86_64",
        deps={"nginx": "1.24", "python": "3.11"},
        captured_at="2026-11-17T03:00:00+00:00",
        verify_result="pass",
    )
    store.put_env(mid, fp)

    got = store.get_env(mid)
    assert got is not None
    assert got.arch == "x86_64"
    assert got.deps["nginx"] == "1.24"
    assert got.key() == "kylin-v11-sp3-x86_64-4.19.90-25.10.v2101"


def test_env_upsert_overwrites(store):
    mid = store.add(make_record())
    fp1 = EnvFingerprint("kylin", "v11", "sp2", "4.19.90", "x86_64", {}, "t1", "unknown")
    fp2 = EnvFingerprint("kylin", "v11", "sp3", "4.19.90", "x86_64", {}, "t2", "pass")
    store.put_env(mid, fp1)
    store.put_env(mid, fp2)
    assert store.get_env(mid).os_sp == "sp3"
    assert len(store.all_envs()) == 1


def test_env_cascade_deleted_with_memory(store):
    mid = store.add(make_record())
    store.put_env(mid, EnvFingerprint("kylin", "v11", "sp3", "4.19", "x86_64", {}, "t", "pass"))
    store.purge(mid)
    assert store.get_env(mid) is None


# ── 删除与墓碑 ────────────────────────────────────────────────────────
def test_soft_delete_marks_status_and_tombstone(store):
    mid = store.add(make_record())
    store.delete(mid, scope="storage", residual="none")

    assert store.get(mid).status == STATUS_DELETED
    tombs = store.tombstones()
    assert len(tombs) == 1
    assert tombs[0]["memory_id"] == mid
    assert tombs[0]["residual_check"] == "none"


def test_purge_removes_row_but_keeps_tombstone(store):
    mid = store.add(make_record())
    store.delete(mid)
    assert store.purge(mid) is True
    assert store.get(mid) is None
    assert len(store.tombstones()) == 1


# ── 事件与统计 ────────────────────────────────────────────────────────
def test_events_recorded_on_write_and_delete(store):
    mid = store.add(make_record())
    store.delete(mid)
    evts = store.events(memory_id=mid)
    kinds = {e["event"] for e in evts}
    assert "write" in kinds
    assert "delete" in kinds


def test_stats_counts(store):
    store.add(make_record(kind="episodic"))
    store.add(make_record(kind="semantic"))
    s = store.stats()
    assert s["total"] == 2
    assert s["by_kind"]["episodic"] == 1
    assert s["by_kind"]["semantic"] == 1
    assert s["events"] >= 2


# ── 坏数据容错 ────────────────────────────────────────────────────────
def test_corrupted_json_does_not_crash(store):
    mid = store.add(make_record())
    store.conn.execute("UPDATE memories SET conditions='{bad json' WHERE id=?", (mid,))
    store.conn.commit()

    rec = store.get(mid)
    assert rec is not None
    assert rec.conditions == {}
