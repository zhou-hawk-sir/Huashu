"""记忆层 REST 接口

给「记忆管理页」(`/web/memory/`) 用，也与对外演示脚本共用。
设计原则：
1. 只读接口全部容错，记忆库坏了返回 ok=False 而不是 500；
2. 写操作（状态流转、验证、删除、手写）都留事件，不静默改；
3. 路径上静态段声明在动态段之前，避免 /stats 被 /{memory_id} 吃掉。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from utils.logger import get_logger

logger = get_logger("api.memory")


class StatusBody(BaseModel):
    status: str = Field(..., description="active|stale|conflict|quarantined|deleted")
    note: str = ""


class VerifyBody(BaseModel):
    ok: bool = True


class WriteBody(BaseModel):
    kind: str = "episodic"
    title: str
    symptom: str
    diagnosis: str = ""
    solution: str = ""
    source: str = "manual"


def build_memory_router(service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/memory", tags=["memory"])

    def _svc():
        if service is None:
            raise HTTPException(status_code=503, detail="记忆层未启用")
        return service

    # ── 概览 ─────────────────────────────────────────────────────────
    @router.get("/stats")
    async def stats() -> Dict[str, Any]:
        try:
            return _svc().stats()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @router.get("/env")
    async def env() -> Dict[str, Any]:
        try:
            return _svc().env_info()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 列表与检索 ───────────────────────────────────────────────────
    @router.get("/list")
    async def list_memories(
        kind: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        q: str = "",
    ) -> Dict[str, Any]:
        try:
            return _svc().list(kind=kind, status=status, limit=limit, offset=offset, q=q)
        except Exception as e:
            return {"ok": False, "error": str(e), "items": []}

    @router.get("/search")
    async def search(q: str, k: int = 5, session_id: str = "") -> Dict[str, Any]:
        try:
            return _svc().search(q, k=k, session_id=session_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @router.get("/conflicts")
    async def conflicts() -> Dict[str, Any]:
        try:
            return _svc().conflicts()
        except Exception as e:
            return {"ok": False, "error": str(e), "items": []}

    @router.get("/events")
    async def events(session_id: str = "", limit: int = 100) -> Dict[str, Any]:
        try:
            return _svc().events(session_id=session_id, limit=limit)
        except Exception as e:
            return {"ok": False, "error": str(e), "items": []}

    # ── 治理动作 ─────────────────────────────────────────────────────
    @router.post("/sweep")
    async def sweep() -> Dict[str, Any]:
        try:
            report = _svc().sweep()
            return {"ok": True, **report.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @router.post("/write")
    async def write(body: WriteBody) -> Dict[str, Any]:
        from memory.writer import WriteRequest

        try:
            result = _svc().write(
                WriteRequest(
                    kind=body.kind,
                    title=body.title,
                    symptom=body.symptom,
                    diagnosis=body.diagnosis,
                    solution=body.solution,
                    source=body.source,
                    provenance={"source_type": body.source},
                )
            )
            if result is None:
                return {"ok": False, "error": "写入被拒绝或记忆层未启用"}
            return {"ok": True, **result.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 单条 ─────────────────────────────────────────────────────────
    @router.get("/{memory_id}")
    async def detail(memory_id: str) -> Dict[str, Any]:
        try:
            return _svc().get(memory_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @router.post("/{memory_id}/status")
    async def set_status(memory_id: str, body: StatusBody) -> Dict[str, Any]:
        try:
            version = _svc().set_status(memory_id, body.status, body.note)
            return {"ok": True, "version": version}
        except KeyError:
            return {"ok": False, "error": "记忆不存在"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @router.post("/{memory_id}/verify")
    async def verify(memory_id: str, body: VerifyBody) -> Dict[str, Any]:
        try:
            return {"ok": True, **_svc().verify(memory_id, body.ok)}
        except KeyError:
            return {"ok": False, "error": "记忆不存在"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @router.delete("/{memory_id}")
    async def delete(memory_id: str, scope: str = "all") -> Dict[str, Any]:
        try:
            return _svc().delete(memory_id, scope=scope)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return router
