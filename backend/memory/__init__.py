"""记忆层（V5.4）

对外入口为 MemoryService；本包只提供能力，不改变 V2.0 安全链的任何语义。

模块职责：
    types        数据类型（记忆记录、环境指纹、候选、断言结果、评分参数）
    schema       建表与迁移
    store        CRUD、版本快照、墓碑、事件
    fingerprint  环境采集与四维匹配
    tokenizer    分词与同义词归一
    scorer       BM25 与四因子打分
    retriever    两步检索编排（硬性检查 → 打分 → 惩罚 → 排序）
    arbitration  四级冲突仲裁（禁止自动择一）
    expiry       三类过期与状态机
    writer       写入网关（来源识别、指令扫描、隔离区）
    guard        四项边界断言
    service      对 Agent 主循环的门面
    bench        Huashu-MemBench 执行器
"""
from .types import (
    ALL_KINDS,
    ALL_STATUSES,
    KIND_EPISODIC,
    KIND_PROCEDURAL,
    KIND_SEMANTIC,
    KIND_WORKING,
    STATUS_ACTIVE,
    STATUS_CONFLICT,
    STATUS_DELETED,
    STATUS_QUARANTINED,
    STATUS_STALE,
    Candidate,
    EnvFingerprint,
    GuardResult,
    MemoryRecord,
    ScoreParams,
)
from .arbitration import Arbiter, ArbitrationResult, ConflictGroup
from .expiry import ExpiryConfig, ExpiryManager, ExpiryReport
from .guard import MemoryGuard
from .retriever import RetrievalResult, Retriever
from .service import MemoryConfig, MemoryService, RuntimeFlags
from .store import MemoryStore
from .writer import MemoryWriter, WriteRequest, WriteResult, scan_instructions

__all__ = [
    "ALL_KINDS",
    "ALL_STATUSES",
    "KIND_EPISODIC",
    "KIND_PROCEDURAL",
    "KIND_SEMANTIC",
    "KIND_WORKING",
    "STATUS_ACTIVE",
    "STATUS_CONFLICT",
    "STATUS_DELETED",
    "STATUS_QUARANTINED",
    "STATUS_STALE",
    "Arbiter",
    "ArbitrationResult",
    "Candidate",
    "ConflictGroup",
    "EnvFingerprint",
    "ExpiryConfig",
    "ExpiryManager",
    "ExpiryReport",
    "GuardResult",
    "MemoryConfig",
    "MemoryGuard",
    "MemoryRecord",
    "MemoryService",
    "MemoryStore",
    "MemoryWriter",
    "RetrievalResult",
    "Retriever",
    "RuntimeFlags",
    "ScoreParams",
    "WriteRequest",
    "WriteResult",
    "scan_instructions",
]
