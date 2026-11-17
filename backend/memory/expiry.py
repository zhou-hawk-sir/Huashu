"""三类过期触发与状态机

设计要点：
1. 过期只降权，不删除。删掉的记忆无法解释「为什么当时那么建议」，追责时会断链。
2. 三类触发各自独立可测：时间阈值、环境变更、重新验证失败。
3. 状态流转集中在一张表里，任何非法跳转直接拒绝，避免出现「从 deleted 复活」。

与安全链的关系：本模块只改记忆自身状态，不参与任何 allow/deny 判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

from .types import (
    EXPIRE_ENV_CHANGED,
    EXPIRE_REVERIFY_FAIL,
    EXPIRE_TIME,
    MATCH_EXACT,
    MATCH_MINOR,
    STATUS_ACTIVE,
    STATUS_CONFLICT,
    STATUS_DELETED,
    STATUS_QUARANTINED,
    STATUS_STALE,
    EnvFingerprint,
    MemoryRecord,
)

logger = get_logger("memory.expiry")

# 状态机：只允许表内的跳转
TRANSITIONS: Dict[str, set] = {
    STATUS_ACTIVE: {STATUS_STALE, STATUS_CONFLICT, STATUS_QUARANTINED, STATUS_DELETED},
    STATUS_STALE: {STATUS_ACTIVE, STATUS_CONFLICT, STATUS_DELETED},
    STATUS_CONFLICT: {STATUS_ACTIVE, STATUS_STALE, STATUS_DELETED},
    STATUS_QUARANTINED: {STATUS_ACTIVE, STATUS_DELETED},
    STATUS_DELETED: set(),
}


def can_transition(src: str, dst: str) -> bool:
    """判断状态跳转是否合法。同状态视为幂等，返回 True。"""
    if src == dst:
        return True
    return dst in TRANSITIONS.get(src, set())


@dataclass
class ExpiryConfig:
    """过期策略参数。全部来自配置，不写死在代码里。"""

    stale_after_days: float = 180.0      # 超过 N 日未验证 → stale
    env_change_stales: bool = True       # 架构或 OS 主版本变更 → stale
    reverify_fail_stales: bool = True    # 重验失败 → stale


@dataclass
class ExpiryDecision:
    """一次过期判定的结论。trigger/action 为空表示不处理。"""

    memory_id: str
    trigger: str = ""
    action: str = ""
    reason: str = ""

    @property
    def hit(self) -> bool:
        return bool(self.trigger)


@dataclass
class ExpiryReport:
    """一次批量巡检的结果，供界面与实验记录使用。"""

    scanned: int = 0
    time: List[str] = field(default_factory=list)
    env: List[str] = field(default_factory=list)
    reverify: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scanned": self.scanned,
            "time": list(self.time),
            "env": list(self.env),
            "reverify": list(self.reverify),
            "skipped": list(self.skipped),
        }

    @property
    def changed(self) -> int:
        return len(self.time) + len(self.env) + len(self.reverify)


def _parse_ts(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def days_since(text: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """距某时间戳的天数。解析失败返回 None，由调用方决定如何降级。"""
    ts = _parse_ts(text)
    if ts is None:
        return None
    cur = now or datetime.now(timezone.utc)
    return max(0.0, (cur - ts).total_seconds() / 86400.0)


class ExpiryManager:
    """三类过期判定与状态机执行器。"""

    def __init__(
        self,
        store: Any,
        config: Optional[ExpiryConfig] = None,
        now: Optional[datetime] = None,
    ) -> None:
        self.store = store
        self.config = config or ExpiryConfig()
        self._now = now

    # ── 时钟（测试可注入固定时间）────────────────────────────────────
    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def set_now(self, now: Optional[datetime]) -> None:
        self._now = now

    # ── 单条判定 ─────────────────────────────────────────────────────
    def check_time(self, rec: MemoryRecord) -> ExpiryDecision:
        """时间触发：以最近验证时间为准，无验证时间则退回创建时间。"""
        base = rec.last_verified_at or rec.created_at
        age = days_since(base, self.now())
        if age is None:
            return ExpiryDecision(rec.id)
        if age > self.config.stale_after_days:
            return ExpiryDecision(
                memory_id=rec.id,
                trigger=EXPIRE_TIME,
                action=STATUS_STALE,
                reason=f"已有 {age:.0f} 日未验证（阈值 {self.config.stale_after_days:.0f} 日）",
            )
        return ExpiryDecision(rec.id)

    def check_env(
        self, rec: MemoryRecord, current: Optional[EnvFingerprint]
    ) -> ExpiryDecision:
        """环境变更触发：架构变化或 OS 主版本变化视为「适用边界已失效」。

        只判这两项，不因内核小版本变化就整条置为过期——那样会误伤大量仍可用的经验。
        """
        if not self.config.env_change_stales or current is None:
            return ExpiryDecision(rec.id)
        stored = self.store.get_env(rec.id)
        if stored is None:
            return ExpiryDecision(rec.id)

        if stored.arch and current.arch and stored.arch != current.arch:
            return ExpiryDecision(
                memory_id=rec.id,
                trigger=EXPIRE_ENV_CHANGED,
                action=STATUS_STALE,
                reason=f"运行环境架构由 {stored.arch} 变为 {current.arch}",
            )
        if stored.os_id and current.os_id and stored.os_id != current.os_id:
            return ExpiryDecision(
                memory_id=rec.id,
                trigger=EXPIRE_ENV_CHANGED,
                action=STATUS_STALE,
                reason=f"操作系统由 {stored.os_id} 变为 {current.os_id}",
            )
        if (
            stored.os_version
            and current.os_version
            and stored.os_version != current.os_version
        ):
            return ExpiryDecision(
                memory_id=rec.id,
                trigger=EXPIRE_ENV_CHANGED,
                action=STATUS_STALE,
                reason=f"OS 主版本由 {stored.os_version} 变为 {current.os_version}",
            )
        return ExpiryDecision(rec.id)

    def check_reverify(self, memory_id: str, ok: bool) -> ExpiryDecision:
        """重新验证结果触发。成功则复活，失败则降级为 stale。"""
        if ok:
            return ExpiryDecision(
                memory_id=memory_id,
                trigger="",
                action=STATUS_ACTIVE,
                reason="重新验证通过",
            )
        if not self.config.reverify_fail_stales:
            return ExpiryDecision(memory_id)
        return ExpiryDecision(
            memory_id=memory_id,
            trigger=EXPIRE_REVERIFY_FAIL,
            action=STATUS_STALE,
            reason="重新验证未通过，降级为待复核",
        )

    # ── 状态流转 ─────────────────────────────────────────────────────
    def apply(self, decision: ExpiryDecision) -> bool:
        """执行一条过期判定。返回是否真的改了状态。"""
        if not decision.hit or not decision.action:
            return False
        rec = self.store.get(decision.memory_id)
        if rec is None:
            return False
        if rec.status == STATUS_DELETED:
            return False
        if rec.status == decision.action:
            return False
        if not can_transition(rec.status, decision.action):
            logger.warning(
                "非法状态跳转已拒绝: %s %s -> %s",
                decision.memory_id,
                rec.status,
                decision.action,
            )
            return False

        self.store.set_status(decision.memory_id, decision.action, decision.reason)
        self.store.add_event(
            "expire",
            memory_id=decision.memory_id,
            detail={"trigger": decision.trigger, "reason": decision.reason},
        )
        return True

    def revive(self, memory_id: str, reason: str = "重新验证通过") -> bool:
        """把 stale / conflict 恢复为 active。"""
        rec = self.store.get(memory_id)
        if rec is None or rec.status == STATUS_ACTIVE:
            return False
        if not can_transition(rec.status, STATUS_ACTIVE):
            return False
        self.store.set_status(memory_id, STATUS_ACTIVE, reason)
        return True

    def record_verify(self, memory_id: str, ok: bool) -> ExpiryDecision:
        """回写一次验证结果，并按结果决定是否降级/复活。"""
        decision = self.check_reverify(memory_id, ok)
        try:
            self.store.record_verify(memory_id, ok)
        except KeyError:
            return decision
        if ok:
            self.revive(memory_id, "重新验证通过，恢复为可用")
        else:
            self.apply(decision)
        return decision

    # ── 批量巡检 ─────────────────────────────────────────────────────
    def sweep(
        self,
        current: Optional[EnvFingerprint] = None,
        kinds: Optional[List[str]] = None,
    ) -> ExpiryReport:
        """对全库做一轮巡检。只处理 active，避免反复改写同一批记录。"""
        report = ExpiryReport()
        records = self.store.list(limit=100000)
        for rec in records:
            if kinds and rec.kind not in kinds:
                continue
            if rec.status != STATUS_ACTIVE:
                report.skipped.append(rec.id)
                continue
            report.scanned += 1

            d_time = self.check_time(rec)
            if d_time.hit:
                if self.apply(d_time):
                    report.time.append(rec.id)
                continue

            d_env = self.check_env(rec, current)
            if d_env.hit:
                if self.apply(d_env):
                    report.env.append(rec.id)

        if report.changed:
            logger.info(
                "过期巡检：扫描 %d 条，时间触发 %d，环境触发 %d",
                report.scanned,
                len(report.time),
                len(report.env),
            )
        return report


def stale_hint(records: List[MemoryRecord], now: Optional[datetime] = None) -> str:
    """过期提示文案。检索结果里带过期项时追加，界面与注入块共用。"""
    stale = [r for r in records if r.status == STATUS_STALE]
    if not stale:
        return ""
    names = "、".join(r.title[:18] for r in stale[:3])
    more = f" 等 {len(stale)} 条" if len(stale) > 3 else ""
    return f"其中 {names}{more} 已过期（长期未验证或环境已变更），结论仅供参考，执行前请重新核实。"
