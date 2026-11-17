"""过期与状态机单元测试

三类触发各一条，外加两条边界：
- 过期只降权不删除；
- deleted 是终态，不能被任何路径复活。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory.expiry import ExpiryConfig, ExpiryManager, can_transition, days_since
from memory.schema import connect
from memory.store import MemoryStore
from memory.types import (
    EXPIRE_ENV_CHANGED,
    EXPIRE_REVERIFY_FAIL,
    EXPIRE_TIME,
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_STALE,
    EnvFingerprint,
    MemoryRecord,
)

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


def fp(arch="x86_64", os_id="kylin", version="v11"):
    return EnvFingerprint(
        os_id=os_id, os_version=version, os_sp="sp3", kernel="4.19.90-25.10",
        arch=arch, deps={"nginx": "1.24"}, captured_at="2026-01-01T00:00:00+00:00",
    )


def add(store, verified_days_ago=1, status=STATUS_ACTIVE):
    ts = (NOW - timedelta(days=verified_days_ago)).isoformat()
    rec = MemoryRecord(
        id="", kind="episodic", title="样例", symptom="服务起不来",
        solution="重启", status=status, last_verified_at=ts, created_at=ts,
    )
    return store.add(rec)


@pytest.fixture()
def store():
    s = MemoryStore(conn=connect(":memory:"))
    return s


def test_days_since_handles_bad_input():
    assert days_since("not-a-date", NOW) is None
    assert days_since(None, NOW) is None
    assert days_since((NOW - timedelta(days=3)).isoformat(), NOW) == pytest.approx(3.0)


def test_time_trigger_marks_stale_not_deleted(store):
    mid = add(store, verified_days_ago=400)
    store.put_env(mid, fp())
    mgr = ExpiryManager(store, ExpiryConfig(stale_after_days=180), now=NOW)
    report = mgr.sweep(fp())
    assert report.time == [mid]
    rec = store.get(mid)
    assert rec is not None
    assert rec.status == STATUS_STALE
    assert store.get(mid) is not None  # 降权不删除


def test_time_trigger_respects_threshold(store):
    mid = add(store, verified_days_ago=10)
    store.put_env(mid, fp())
    mgr = ExpiryManager(store, ExpiryConfig(stale_after_days=180), now=NOW)
    assert mgr.sweep(fp()).changed == 0
    assert store.get(mid).status == STATUS_ACTIVE


def test_env_trigger_on_arch_change(store):
    mid = add(store, verified_days_ago=1)
    store.put_env(mid, fp(arch="loongarch64"))
    mgr = ExpiryManager(store, now=NOW)
    report = mgr.sweep(fp(arch="x86_64"))
    assert report.env == [mid]
    assert store.get(mid).status == STATUS_STALE


def test_env_trigger_on_major_version_change(store):
    mid = add(store, verified_days_ago=1)
    store.put_env(mid, fp(version="v10"))
    mgr = ExpiryManager(store, now=NOW)
    assert mgr.sweep(fp(version="v11")).env == [mid]


def test_env_trigger_ignores_kernel_patch(store):
    mid = add(store, verified_days_ago=1)
    store.put_env(mid, fp())
    changed = fp()
    changed = EnvFingerprint(
        changed.os_id, changed.os_version, changed.os_sp,
        "4.19.90-26.1", changed.arch, changed.deps, changed.captured_at,
    )
    mgr = ExpiryManager(store, now=NOW)
    assert mgr.sweep(changed).changed == 0


def test_reverify_fail_marks_stale(store):
    mid = add(store, verified_days_ago=1)
    mgr = ExpiryManager(store, now=NOW)
    decision = mgr.record_verify(mid, ok=False)
    assert decision.trigger == EXPIRE_REVERIFY_FAIL
    rec = store.get(mid)
    assert rec.status == STATUS_STALE
    assert rec.fail_count == 1


def test_reverify_pass_revives(store):
    mid = add(store, verified_days_ago=400)
    mgr = ExpiryManager(store, now=NOW)
    mgr.sweep(fp())
    assert store.get(mid).status == STATUS_STALE
    mgr.record_verify(mid, ok=True)
    assert store.get(mid).status == STATUS_ACTIVE
    assert store.get(mid).success_count == 1


def test_expire_event_is_recorded(store):
    mid = add(store, verified_days_ago=400)
    mgr = ExpiryManager(store, now=NOW)
    mgr.sweep(fp())
    events = store.events(memory_id=mid)
    assert any(e["event"] == "expire" and e["detail"].get("trigger") == EXPIRE_TIME for e in events)


def test_transition_table():
    assert can_transition(STATUS_ACTIVE, STATUS_STALE)
    assert can_transition(STATUS_STALE, STATUS_ACTIVE)
    assert not can_transition(STATUS_DELETED, STATUS_ACTIVE)
    assert not can_transition(STATUS_ACTIVE, "unknown")
    assert can_transition(STATUS_ACTIVE, STATUS_ACTIVE)


def test_deleted_cannot_be_revived(store):
    mid = add(store, verified_days_ago=400)
    store.delete(mid)
    mgr = ExpiryManager(store, now=NOW)
    assert mgr.revive(mid) is False
    assert store.get(mid).status == STATUS_DELETED


def test_sweep_skips_already_stale(store):
    mid = add(store, verified_days_ago=400, status=STATUS_STALE)
    mgr = ExpiryManager(store, now=NOW)
    report = mgr.sweep(fp())
    assert report.time == []
    assert mid in report.skipped


def test_env_trigger_can_be_disabled(store):
    mid = add(store, verified_days_ago=1)
    store.put_env(mid, fp(arch="loongarch64"))
    mgr = ExpiryManager(store, ExpiryConfig(env_change_stales=False), now=NOW)
    assert mgr.sweep(fp(arch="x86_64")).changed == 0
