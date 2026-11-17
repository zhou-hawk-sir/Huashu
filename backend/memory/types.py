"""记忆层数据类型定义

与 V2.0 既有代码风格保持一致：dataclass + 中文注释 + 无第三方依赖。
本模块只定义结构，不含任何 IO 与业务逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── 四类记忆 ──────────────────────────────────────────────────────────
KIND_WORKING = "working"        # 当前会话临时上下文，会话结束失效
KIND_EPISODIC = "episodic"      # 一次完整故障处置记录
KIND_SEMANTIC = "semantic"      # 沉淀的事实，如「11434 是 ollama 端口」
KIND_PROCEDURAL = "procedural"  # 可复用的处置步骤序列

ALL_KINDS = (KIND_WORKING, KIND_EPISODIC, KIND_SEMANTIC, KIND_PROCEDURAL)

# 参与检索的类型：工作记忆仅同会话可见，不进入全局召回
SEARCHABLE_KINDS = (KIND_EPISODIC, KIND_SEMANTIC, KIND_PROCEDURAL)

# ── 五个状态 ──────────────────────────────────────────────────────────
STATUS_ACTIVE = "active"            # 正常可用
STATUS_STALE = "stale"              # 过期，降权但保留
STATUS_CONFLICT = "conflict"        # 与其他经验冲突，待裁决
STATUS_QUARANTINED = "quarantined"  # 低信任隔离区，不参与正常排序
STATUS_DELETED = "deleted"          # 已删除，仅留墓碑

ALL_STATUSES = (
    STATUS_ACTIVE,
    STATUS_STALE,
    STATUS_CONFLICT,
    STATUS_QUARANTINED,
    STATUS_DELETED,
)

# ── 环境匹配等级 ──────────────────────────────────────────────────────
MATCH_EXACT = "exact"
MATCH_MINOR = "minor"
MATCH_MAJOR = "major"
MATCH_NONE = "none"

# ── 过期触发类型 ──────────────────────────────────────────────────────
EXPIRE_TIME = "time"              # 超过 N 日未验证
EXPIRE_ENV_CHANGED = "env"        # 环境变更事件
EXPIRE_REVERIFY_FAIL = "reverify"  # 重新验证失败


@dataclass(frozen=True)
class EnvFingerprint:
    """环境指纹：记忆的适用边界。

    六字段缺一不可。架构与 OS 版本是硬约束，内核与依赖是软约束。
    """

    os_id: str                      # kylin / ubuntu / debian
    os_version: str                 # v11 / 22.04
    os_sp: str                      # sp3 / 空串
    kernel: str                     # 4.19.90-...
    arch: str                       # x86_64 / loongarch64 / aarch64
    deps: Dict[str, str] = field(default_factory=dict)  # {"nginx": "1.24"}
    captured_at: str = ""
    verify_result: str = "unknown"  # pass | fail | unknown

    def to_dict(self) -> Dict[str, Any]:
        return {
            "os_id": self.os_id,
            "os_version": self.os_version,
            "os_sp": self.os_sp,
            "kernel": self.kernel,
            "arch": self.arch,
            "deps": dict(self.deps),
            "captured_at": self.captured_at,
            "verify_result": self.verify_result,
        }

    def key(self) -> str:
        """指纹的紧凑表示，用于日志与断言比对。"""
        return f"{self.os_id}-{self.os_version}-{self.os_sp}-{self.arch}-{self.kernel}"


@dataclass
class MemoryRecord:
    """一条记忆。与 memory.db 的 memories 表一一对应。"""

    id: str
    kind: str
    title: str
    symptom: str
    diagnosis: str = ""
    solution: str = ""
    conditions: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_ACTIVE
    version: int = 1
    success_count: int = 0
    fail_count: int = 0
    last_verified_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "symptom": self.symptom,
            "diagnosis": self.diagnosis,
            "solution": self.solution,
            "conditions": dict(self.conditions),
            "provenance": dict(self.provenance),
            "status": self.status,
            "version": self.version,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "last_verified_at": self.last_verified_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @property
    def verify_rate(self) -> float:
        """历史验证成功率。无验证记录时返回 0.5 的中性值。"""
        total = self.success_count + self.fail_count
        if total == 0:
            return 0.5
        return self.success_count / total


@dataclass
class Candidate:
    """检索候选。打分中间态与最终排序都挂在这里。"""

    memory: Dict[str, Any]
    sim: float = 0.0
    env_score: float = 0.0
    success: float = 0.0
    freshness: float = 0.0
    penalties: Dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    excluded: bool = False
    exclude_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory.get("id"),
            "score": round(self.score, 6),
            "sim": round(self.sim, 6),
            "env_score": round(self.env_score, 6),
            "success": round(self.success, 6),
            "freshness": round(self.freshness, 6),
            "penalties": {k: round(v, 6) for k, v in self.penalties.items()},
            "excluded": self.excluded,
            "exclude_reason": self.exclude_reason,
        }


@dataclass
class GuardResult:
    """边界断言的判定结果。

    四项断言任一失败即 allowed=False，reason 记录失败项名称。
    """

    allowed: bool
    reason: str = ""
    assertions: Dict[str, bool] = field(default_factory=dict)

    @classmethod
    def ok(cls, assertions: Optional[Dict[str, bool]] = None) -> "GuardResult":
        return cls(allowed=True, reason="", assertions=assertions or {})

    @classmethod
    def deny(cls, reason: str, assertions: Optional[Dict[str, bool]] = None) -> "GuardResult":
        return cls(allowed=False, reason=reason, assertions=assertions or {})


@dataclass
class ScoreParams:
    """四因子权重与乘性惩罚系数。

    系数由预实验标定，此处为初值，不写入文档结论。
    """

    alpha: float = 0.40   # Sim 语义相似度
    beta: float = 0.30    # Env 环境匹配度
    gamma: float = 0.20   # Success 历史成功率
    delta: float = 0.10   # Freshness 时间新鲜度

    # 乘性惩罚：不适用程度越高，折扣越狠
    penalty_os: float = 0.30
    penalty_arch: float = 0.10
    penalty_kernel: float = 0.70
    penalty_dep: float = 0.70
    penalty_stale: float = 0.50

    # 新鲜度半衰期（天）
    freshness_half_life_days: float = 90.0

    def weight_sum(self) -> float:
        return self.alpha + self.beta + self.gamma + self.delta
