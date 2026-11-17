"""记忆层（V5.4 新建）

对外入口为 MemoryService；本包只提供能力，不改变 V2.0 安全链的任何语义。

模块职责：
    types        数据类型（记忆记录、环境指纹、候选、断言结果、评分参数）
    schema       建表与迁移
    store        CRUD、版本快照、墓碑、事件
    fingerprint  环境采集与匹配（后续）
    tokenizer    分词与同义词归一（后续）
    scorer       BM25 与四因子打分（后续）
    retriever    两步检索编排（后续）
    arbitration  四级冲突仲裁（后续）
    expiry       三类过期与状态机（后续）
    writer       写入四道关与隔离区（后续）
    guard        四项边界断言（后续）
    service      对 Agent 主循环的门面（后续）
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
    "Candidate",
    "EnvFingerprint",
    "GuardResult",
    "MemoryRecord",
    "ScoreParams",
]
