"""命令知识库 REST 路由（前端 CmdKbView 用）

挂载在 /api/cmd_kb 前缀下；通过 FastAPI app include_router 接入。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from cmd_kb.store import CmdStore
from cmd_kb.search import CmdSearcher
from cmd_kb.validator import CmdValidator
from utils.logger import get_logger

logger = get_logger("api.cmd_kb")

# 单例（首次访问时惰性创建；后续复用）
_store: Optional[CmdStore] = None
_searcher: Optional[CmdSearcher] = None
_validator: Optional[CmdValidator] = None


def _get_store() -> CmdStore:
    global _store
    if _store is None:
        _store = CmdStore()
    return _store


def _get_searcher() -> CmdSearcher:
    global _searcher
    if _searcher is None:
        _searcher = CmdSearcher(_get_store())
    return _searcher


def _get_validator() -> CmdValidator:
    global _validator
    if _validator is None:
        _validator = CmdValidator(_get_store())
    return _validator


# ---------- Pydantic models ----------


class CmdListResponse(BaseModel):
    commands: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int


class CmdSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = 10
    category: Optional[str] = None
    risk: Optional[str] = None


class CmdSearchHit(BaseModel):
    name: str
    category: str
    desc: str
    risk: str
    type: str
    score: float
    matched_by: List[str] = Field(default_factory=list)


class CmdSearchResponse(BaseModel):
    results: List[CmdSearchHit]
    total: int


class CmdValidateRequest(BaseModel):
    cmdline: str = Field(..., min_length=1)


class CmdValidationIssue(BaseModel):
    severity: str
    kind: str
    message: str
    command: str
    arg: Optional[str] = None


class CmdValidationResponse(BaseModel):
    cmdline: str
    valid: bool
    commands: List[str]
    unknown_commands: List[str]
    issues: List[CmdValidationIssue] = Field(default_factory=list)


# ---------- Router ----------


def build_cmd_kb_router() -> APIRouter:
    router = APIRouter(prefix="/api/cmd_kb", tags=["cmd_kb"])

    @router.get("/commands", response_model=CmdListResponse)
    async def list_commands(
        category: Optional[str] = Query(None),
        risk: Optional[str] = Query(None),
        search: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> CmdListResponse:
        store = _get_store()
        try:
            entries = store.all()
            if category:
                entries = [e for e in entries if e.category == category]
            if risk:
                entries = [e for e in entries if e.risk == risk]
            if search:
                q = search.lower()
                entries = [e for e in entries if q in e.name.lower() or q in (e.desc or "").lower()]
            total = len(entries)
            page = entries[offset: offset + limit]
            return CmdListResponse(
                commands=[e.to_dict() for e in page],
                total=total,
                limit=limit,
                offset=offset,
            )
        except Exception as e:
            logger.exception("list_commands failed")
            raise HTTPException(status_code=500, detail=f"cmd_kb list failed: {e}")

    @router.get("/commands/{name}")
    async def get_command(name: str) -> Dict[str, Any]:
        store = _get_store()
        e = store.get(name)
        if e is None:
            raise HTTPException(status_code=404, detail=f"command not found: {name}")
        return e.to_dict()

    @router.post("/search", response_model=CmdSearchResponse)
    async def search(req: CmdSearchRequest) -> CmdSearchResponse:
        try:
            searcher = _get_searcher()
            raw = searcher.search(req.query, top_k=req.top_k)
            # CmdSearcher.search 返回的格式按实现不同，统一适配
            hits: List[CmdSearchHit] = []
            for item in raw or []:
                if isinstance(item, dict):
                    hits.append(CmdSearchHit(
                        name=item.get("name", ""),
                        category=item.get("category", "未分类"),
                        desc=item.get("desc", ""),
                        risk=item.get("risk", "low"),
                        type=item.get("type", "external"),
                        score=float(item.get("score", 0.0)),
                        matched_by=list(item.get("matched_by", []) or []),
                    ))
                elif hasattr(item, "entry") and hasattr(item, "score"):
                    # SearchResult dataclass（cmd_kb/search.py）
                    e = item.entry
                    hits.append(CmdSearchHit(
                        name=getattr(e, "name", ""),
                        category=getattr(e, "category", "未分类"),
                        desc=getattr(e, "desc", ""),
                        risk=getattr(e, "risk", "low"),
                        type=getattr(e, "type", "external"),
                        score=float(getattr(item, "score", 0.0)),
                        matched_by=list(getattr(item, "matched_by", []) or []),
                    ))
                elif isinstance(item, tuple) and len(item) >= 1:
                    # tuple 形式兜底 (entry, score[, matched_by])
                    e = item[0]
                    score = item[1] if len(item) > 1 else 0.0
                    hits.append(CmdSearchHit(
                        name=getattr(e, "name", ""),
                        category=getattr(e, "category", "未分类"),
                        desc=getattr(e, "desc", ""),
                        risk=getattr(e, "risk", "low"),
                        type=getattr(e, "type", "external"),
                        score=float(score),
                    ))
            return CmdSearchResponse(results=hits, total=len(hits))
        except Exception as e:
            logger.exception("search failed")
            raise HTTPException(status_code=500, detail=f"cmd_kb search failed: {e}")

    @router.post("/validate", response_model=CmdValidationResponse)
    async def validate(req: CmdValidateRequest) -> CmdValidationResponse:
        try:
            v = _get_validator()
            result = v.validate(req.cmdline)
            # ValidationResult 字段按实现适配
            commands = list(getattr(result, "commands", []) or [])
            issues = []
            for i in (getattr(result, "issues", []) or []):
                issues.append(CmdValidationIssue(
                    severity=getattr(i, "severity", "warn"),
                    kind=getattr(i, "kind", "unknown"),
                    message=getattr(i, "message", ""),
                    command=getattr(i, "command", ""),
                    arg=getattr(i, "arg", None),
                ))
            unknown = list(getattr(result, "unknown_commands", []) or [])
            return CmdValidationResponse(
                cmdline=req.cmdline,
                valid=bool(getattr(result, "valid", len(issues) == 0)),
                commands=commands,
                unknown_commands=unknown,
                issues=issues,
            )
        except Exception as e:
            logger.exception("validate failed")
            raise HTTPException(status_code=500, detail=f"cmd_kb validate failed: {e}")

    @router.get("/stats")
    async def stats() -> Dict[str, Any]:
        try:
            store = _get_store()
            entries = store.all()
            by_risk: Dict[str, int] = {}
            by_category: Dict[str, int] = {}
            by_source: Dict[str, int] = {}
            for e in entries:
                by_risk[e.risk] = by_risk.get(e.risk, 0) + 1
                by_category[e.category] = by_category.get(e.category, 0) + 1
                by_source[e.source] = by_source.get(e.source, 0) + 1
            return {
                "total": len(entries),
                "by_risk": by_risk,
                "by_category": by_category,
                "by_source": by_source,
            }
        except Exception as e:
            logger.exception("stats failed")
            raise HTTPException(status_code=500, detail=f"cmd_kb stats failed: {e}")

    return router