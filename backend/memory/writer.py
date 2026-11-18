"""写入网关：来源识别、指令性扫描、低信任隔离区、四类记忆落库

为什么写入侧要单独设网关：
检索侧的过滤只能挡住「已知的坏记忆」，挡不住「刚被写进来的坏记忆」。
日志、网页、文件内容都可能夹带指令，一旦原样落库，下次检索就会把它当成经验。
所以在入口先做三件事：判来源、扫指令、定信任度，不达标的进隔离区。

隔离区的记忆不是删掉，而是标记 quarantined：不进入正常排序，
人工复核后可转正。这样既不污染经验池，也不丢证据。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils.logger import get_logger

from .fingerprint import capture as capture_env
from .store import MemoryStore, new_id, now_iso
from .tokenizer import normalize_text, symptom_codes, tokens
from .types import (
    KIND_EPISODIC,
    KIND_PROCEDURAL,
    KIND_SEMANTIC,
    KIND_WORKING,
    STATUS_ACTIVE,
    STATUS_QUARANTINED,
    EnvFingerprint,
    MemoryRecord,
)

logger = get_logger("memory.writer")

# ── 来源类型与基础信任度 ──────────────────────────────────────────────
SOURCE_SESSION = "session"     # 本平台会话产出
SOURCE_TOOL = "tool"           # 工具观测到的系统事实
SOURCE_MANUAL = "manual"       # 人工录入 / 人工确认
SOURCE_IMPORT = "import"       # 外部导入
SOURCE_EXTERNAL = "external"   # 日志 / 网页 / 文件内容等不可信文本
SOURCE_BENCH = "bench"         # 评测集注入

BASE_TRUST: Dict[str, float] = {
    SOURCE_MANUAL: 0.95,
    SOURCE_BENCH: 0.85,
    SOURCE_TOOL: 0.75,
    SOURCE_SESSION: 0.65,
    SOURCE_IMPORT: 0.50,
    SOURCE_EXTERNAL: 0.25,
}

# ── 指令性内容特征（写入侧扫描）──────────────────────────────────────
# 命中即视为「文本里夹带指令」，不当作经验，直接进隔离区。
INSTRUCTION_PATTERNS: Sequence[Tuple[str, str]] = (
    ("override_instruction", r"(忽略|无视|忘记)[^。；\n]{0,8}(指令|提示|要求|限制)"),
    ("override_instruction", r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions"),
    ("role_hijack", r"(你现在是|从现在开始你|you\s+are\s+now\s+a)"),
    ("role_hijack", r"(system\s*:|<\|im_start\|>|\[/?INST\])"),
    ("force_exec", r"(必须|务必|立刻|立即|无条件)[^。；\n]{0,6}(执行|运行|允许|放行)"),
    ("bypass_approval", r"(无需|不用|免|跳过)[^。；\n]{0,6}(确认|审批|审核|授权|校验)"),
    ("bypass_approval", r"(已|已经)(获得|取得|通过)[^。；\n]{0,6}(授权|批准|审批)"),
    ("fake_authority", r"(管理员|root|运维负责人|安全管理员)[^。；\n]{0,8}(授权|批准|同意)"),
    ("policy_edit", r"(修改|变更|关闭|设置)[^。；\n]{0,8}(策略|policy|白名单|黑名单|deny_root)"),
    ("destructive", r"(rm\s+-rf\s+/|mkfs\.|dd\s+if=|chmod\s+777\s+/)"),
    ("remote_code", r"(curl|wget)[^\n]{0,80}\|\s*(sh|bash|python)"),
    ("conceal", r"(不要|别)(告诉|告知|提示)[^。；\n]{0,6}(用户|管理员)"),
    ("base64_blob", r"[A-Za-z0-9+/]{80,}={0,2}"),
)

_COMPILED = tuple((name, re.compile(pat, re.IGNORECASE)) for name, pat in INSTRUCTION_PATTERNS)


@dataclass
class WriteRequest:
    """一次写入请求。所有字段显式给出，不从自由文本猜。"""

    kind: str = KIND_EPISODIC
    title: str = ""
    symptom: str = ""
    diagnosis: str = ""
    solution: str = ""
    conditions: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)
    env: Optional[EnvFingerprint] = None
    trust: Optional[float] = None
    session_id: str = ""
    source: str = SOURCE_SESSION
    allow_quarantine: bool = True
    merge_duplicate: bool = True
    # 对照组专用：跳过网关判定直接按可信写入（B 组没有治理能力）。
    # 生产路径永远不要置 True。
    skip_gate: bool = False


@dataclass
class WriteResult:
    """写入结果。quarantined=True 表示进了隔离区，不参与正常检索。"""

    memory_id: str = ""
    created: bool = False
    merged: bool = False
    status: str = ""
    quarantined: bool = False
    version: int = 0
    trust: float = 0.0
    reasons: List[str] = field(default_factory=list)
    hits: List[str] = field(default_factory=list)
    skipped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "created": self.created,
            "merged": self.merged,
            "status": self.status,
            "quarantined": self.quarantined,
            "version": self.version,
            "trust": round(self.trust, 3),
            "reasons": list(self.reasons),
            "hits": list(self.hits),
            "skipped": self.skipped,
        }


def scan_instructions(text: str) -> List[str]:
    """扫描文本中的指令性特征，返回命中的特征名（去重、保序）。"""
    if not text:
        return []
    hits: List[str] = []
    for name, rx in _COMPILED:
        if rx.search(text):
            if name not in hits:
                hits.append(name)
    return hits


def jaccard(a: str, b: str) -> float:
    ta = {t for t in tokens(a) if len(t) > 1}
    tb = {t for t in tokens(b) if len(t) > 1}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class MemoryWriter:
    """四类记忆的统一写入口。"""

    def __init__(
        self,
        store: MemoryStore,
        trust_threshold: float = 0.40,
        merge_threshold: float = 0.62,
        scanner=scan_instructions,
    ) -> None:
        self.store = store
        self.trust_threshold = trust_threshold
        self.merge_threshold = merge_threshold
        self.scanner = scanner

    # ── 评估 ─────────────────────────────────────────────────────────
    def assess(self, req: WriteRequest) -> Tuple[float, List[str], List[str]]:
        """算信任度，返回 (trust, reasons, hits)。"""
        reasons: List[str] = []
        base = BASE_TRUST.get(req.source, 0.5)
        trust = req.trust if req.trust is not None else base

        text = " ".join([req.title, req.symptom, req.diagnosis, req.solution])
        hits = self.scanner(text)
        if hits:
            trust -= 0.45
            reasons.append("内容含指令性片段：" + "、".join(hits))

        if req.source == SOURCE_EXTERNAL:
            reasons.append("来源为外部不可信文本（日志/网页/文件）")
        if req.source == SOURCE_IMPORT:
            reasons.append("来源为外部导入，未经本机验证")

        if not req.symptom.strip():
            trust -= 0.10
            reasons.append("缺少故障现象描述")
        if not req.solution.strip():
            trust -= 0.15
            reasons.append("缺少可执行方案，参考价值低")
        if not req.env:
            trust -= 0.05
            reasons.append("未附带环境指纹，适用性无法判定")

        trust = max(0.0, min(1.0, round(trust, 3)))
        return trust, reasons, hits

    # ── 去重合并 ─────────────────────────────────────────────────────
    def find_similar(self, req: WriteRequest) -> Optional[MemoryRecord]:
        """找同症状、同结论的既有记忆，避免同一件事反复入库。"""
        if not req.merge_duplicate:
            return None
        codes = set(symptom_codes(req.symptom))
        try:
            pool = self.store.list(kind=req.kind, limit=2000)
        except Exception:
            return None

        best: Optional[MemoryRecord] = None
        best_sim = 0.0
        for rec in pool:
            if rec.status == STATUS_QUARANTINED:
                continue
            same_code = bool(codes & set(symptom_codes(rec.symptom)))
            text_sim = jaccard(
                f"{req.title} {req.symptom}", f"{rec.title} {rec.symptom}"
            )
            # 光靠「同一个症状码」不够：端口占用与磁盘满都可能带 start_failure，
            # 必须同时看标题与现象的相似度，否则不同服务的故障会被并成一条。
            if text_sim < self.merge_threshold and not (same_code and text_sim >= 0.45):
                continue
            # 只有「同一问题 + 同一结论」才合并。结论不同要各留一条，
            # 交给仲裁去判，不能靠合并把冲突悄悄抹平。
            if req.solution.strip() and rec.solution:
                if jaccard(req.solution, rec.solution) < 0.6:
                    continue
            if text_sim > best_sim:
                best, best_sim = rec, text_sim
        return best

    # ── 写入 ─────────────────────────────────────────────────────────
    def write(self, req: WriteRequest) -> WriteResult:
        result = WriteResult()
        req.session_id = req.session_id or str(req.provenance.get("session_id", ""))
        req.source = req.source or str(req.provenance.get("source_type", SOURCE_SESSION))

        trust, reasons, hits = self.assess(req)
        if req.skip_gate:
            # 对照组：不判来源、不扫指令，全部按可信写入
            trust, reasons, hits = 1.0, [], []
        result.trust = trust
        result.reasons = reasons
        result.hits = hits

        if not req.title.strip() or not req.symptom.strip():
            result.skipped = True
            result.reasons.append("标题或故障现象为空，拒绝写入")
            return result

        quarantined = bool(hits) or trust < self.trust_threshold
        if quarantined and not req.allow_quarantine:
            result.skipped = True
            result.reasons.append("低信任且不允许隔离写入，已丢弃")
            return result

        env = req.env
        if env is None:
            try:
                env = capture_env()
            except Exception as exc:  # 采集失败不阻断写入，指纹留空
                logger.warning("写入时采集环境指纹失败：%s", exc)
                env = None

        # 合并：同一症状同一结论 → 升版本 + 记一次验证成功
        dup = self.find_similar(req) if not quarantined else None
        if dup is not None:
            changes: Dict[str, Any] = {"last_verified_at": now_iso()}
            if req.solution.strip() and req.solution.strip() != dup.solution:
                changes["solution"] = req.solution.strip()
            if req.diagnosis.strip() and req.diagnosis.strip() != dup.diagnosis:
                changes["diagnosis"] = req.diagnosis.strip()
            prov = dict(dup.provenance)
            merged_sessions = list(prov.get("merged_sessions", []))
            if req.session_id and req.session_id not in merged_sessions:
                merged_sessions.append(req.session_id)
            prov["merged_sessions"] = merged_sessions[-20:]
            prov["last_writer"] = req.source
            changes["provenance"] = prov
            changes["success_count"] = dup.success_count + 1

            version = self.store.update(dup.id, changes, "同症状重复出现，合并并刷新验证时间")
            self.store.add_event(
                "write",
                memory_id=dup.id,
                session_id=req.session_id or None,
                detail={"merged": True, "source": req.source, "hits": hits},
            )
            result.memory_id = dup.id
            result.merged = True
            result.version = version
            result.status = dup.status
            result.quarantined = dup.status == STATUS_QUARANTINED
            return result

        status = STATUS_QUARANTINED if quarantined else STATUS_ACTIVE
        rec = MemoryRecord(
            id=new_id(),
            kind=req.kind,
            title=req.title.strip()[:120],
            symptom=normalize_text(req.symptom)[:2000],
            diagnosis=req.diagnosis.strip()[:4000],
            solution=req.solution.strip()[:4000],
            conditions=dict(req.conditions),
            provenance={
                **dict(req.provenance),
                "source_type": req.source,
                "session_id": req.session_id,
                "trust": trust,
                "trust_reasons": reasons,
                "instruction_hits": hits,
                "written_at": now_iso(),
            },
            status=status,
            success_count=1 if status == STATUS_ACTIVE else 0,
            fail_count=0,
            last_verified_at=now_iso() if status == STATUS_ACTIVE else None,
        )
        memory_id = self.store.add(rec)
        if env is not None:
            self.store.put_env(memory_id, env)

        self.store.add_event(
            "write",
            memory_id=memory_id,
            session_id=req.session_id or None,
            detail={
                "kind": req.kind,
                "source": req.source,
                "trust": trust,
                "quarantined": quarantined,
                "hits": hits,
            },
        )

        result.memory_id = memory_id
        result.created = True
        result.status = status
        result.quarantined = quarantined
        result.version = 1
        if quarantined:
            logger.warning(
                "记忆写入隔离区：%s（trust=%.2f, hits=%s）", memory_id, trust, hits
            )
        return result

    # ── 四类便捷入口 ─────────────────────────────────────────────────
    def write_episodic(
        self,
        title: str,
        symptom: str,
        solution: str,
        diagnosis: str = "",
        session_id: str = "",
        conditions: Optional[Dict[str, Any]] = None,
        env: Optional[EnvFingerprint] = None,
        source: str = SOURCE_SESSION,
    ) -> WriteResult:
        return self.write(
            WriteRequest(
                kind=KIND_EPISODIC,
                title=title,
                symptom=symptom,
                diagnosis=diagnosis,
                solution=solution,
                conditions=conditions or {},
                provenance={"source_type": source, "session_id": session_id},
                env=env,
                session_id=session_id,
                source=source,
            )
        )

    def write_semantic(
        self,
        title: str,
        fact: str,
        session_id: str = "",
        env: Optional[EnvFingerprint] = None,
        source: str = SOURCE_MANUAL,
    ) -> WriteResult:
        return self.write(
            WriteRequest(
                kind=KIND_SEMANTIC,
                title=title,
                symptom=title,
                diagnosis="",
                solution=fact,
                provenance={"source_type": source, "session_id": session_id},
                env=env,
                session_id=session_id,
                source=source,
            )
        )

    def write_procedural(
        self,
        title: str,
        symptom: str,
        steps: Sequence[str],
        session_id: str = "",
        env: Optional[EnvFingerprint] = None,
        source: str = SOURCE_SESSION,
    ) -> WriteResult:
        return self.write(
            WriteRequest(
                kind=KIND_PROCEDURAL,
                title=title,
                symptom=symptom,
                solution=" → ".join(str(s) for s in steps),
                conditions={"steps": [str(s) for s in steps]},
                provenance={"source_type": source, "session_id": session_id},
                env=env,
                session_id=session_id,
                source=source,
            )
        )

    def write_working(
        self,
        title: str,
        content: str,
        session_id: str,
        env: Optional[EnvFingerprint] = None,
    ) -> WriteResult:
        """工作记忆：只在本会话内可见，不进全局检索。"""
        return self.write(
            WriteRequest(
                kind=KIND_WORKING,
                title=title,
                symptom=title,
                solution=content,
                provenance={
                    "source_type": SOURCE_SESSION,
                    "session_id": session_id,
                    "ephemeral": True,
                },
                env=env,
                session_id=session_id,
                source=SOURCE_SESSION,
            )
        )

    # ── 从会话结果落库 ───────────────────────────────────────────────
    def observe_outcome(
        self,
        session_id: str,
        task: str,
        outcome: Dict[str, Any],
        env: Optional[EnvFingerprint] = None,
        trace_id: str = "",
        trace_node_id: Optional[int] = None,
    ) -> Optional[WriteResult]:
        """把一次成功处置沉淀成事件记忆。

        只在「真的执行过工具且整体成功」时写：纯问答没有可复用的操作序列，
        写了只会污染经验池。
        """
        if not task or not task.strip():
            return None
        if not outcome.get("success"):
            return None

        tools = [t for t in outcome.get("tools", []) if t.get("success")]
        if not tools:
            return None

        steps: List[str] = []
        for t in tools:
            name = str(t.get("name", ""))
            args = t.get("args") or {}
            if name == "shell.execute":
                steps.append(str(args.get("cmd", ""))[:160])
            elif name:
                kv = " ".join(f"{k}={v}" for k, v in list(args.items())[:3])
                steps.append(f"{name}({kv})"[:160])

        if not steps:
            return None

        title = task.strip().replace("\n", " ")[:28]
        diagnosis = "；".join(
            f"{t.get('name')} 返回{'成功' if t.get('success') else '失败'}" for t in tools
        )
        req = WriteRequest(
            kind=KIND_EPISODIC,
            title=title,
            symptom=task.strip()[:400],
            diagnosis=diagnosis[:1000],
            solution=" → ".join(s for s in steps if s)[:2000],
            conditions={
                "tools": [t.get("name") for t in tools],
                "tool_count": len(tools),
                "answer_digest": str(outcome.get("answer", ""))[:200],
            },
            provenance={
                "source_type": SOURCE_SESSION,
                "session_id": session_id,
                "task": task[:200],
                # 来源决策链：记忆 → 追踪节点可反查，支撑审计回放
                **({"trace_id": trace_id} if trace_id else {}),
                **(
                    {"trace_node_id": int(trace_node_id)}
                    if trace_node_id is not None
                    else {}
                ),
            },
            env=env,
            session_id=session_id,
            source=SOURCE_SESSION,
        )
        return self.write(req)
