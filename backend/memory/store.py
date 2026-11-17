"""记忆存储层：CRUD、版本快照、墓碑、事件

约定：
- 所有时间字段为 UTC ISO 字符串，秒精度。
- conditions / provenance / detail 等字段以 JSON 文本落库，读出时还原。
- 更新采用「先留档旧值、再升版本」的顺序，任何一次修改都可回溯。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

from .schema import connect, init_db
from .types import (
    ALL_KINDS,
    ALL_STATUSES,
    STATUS_ACTIVE,
    STATUS_DELETED,
    EnvFingerprint,
    MemoryRecord,
)

logger = get_logger("memory.store")

ID_PREFIX = "mem_"


def now_iso() -> str:
    """统一时间格式：UTC、秒精度、带 Z 偏移。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    """生成 26 位十六进制记忆编号。"""
    return ID_PREFIX + secrets.token_hex(13)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _loads(text: Optional[str], default: Any) -> Any:
    """JSON 反序列化。损坏数据不抛异常，返回默认值并记日志。"""
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("记忆字段 JSON 解析失败，已按默认值处理")
        return default


class MemoryStore:
    """memory.db 的唯一读写入口。"""

    def __init__(self, db_path: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> None:
        self.conn = conn or connect(db_path)
        init_db(self.conn)

    # ── 写入 ──────────────────────────────────────────────────────────
    def add(self, rec: MemoryRecord) -> str:
        """新增一条记忆，返回其编号。"""
        if rec.kind not in ALL_KINDS:
            raise ValueError(f"未知记忆类型: {rec.kind}")
        if rec.status not in ALL_STATUSES:
            raise ValueError(f"未知状态: {rec.status}")

        ts = now_iso()
        rec.id = rec.id or new_id()
        rec.created_at = rec.created_at or ts
        rec.updated_at = ts
        rec.version = max(1, rec.version)

        self.conn.execute(
            """INSERT INTO memories
               (id, kind, title, symptom, diagnosis, solution, conditions, provenance,
                status, version, success_count, fail_count, last_verified_at,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.id,
                rec.kind,
                rec.title,
                rec.symptom,
                rec.diagnosis,
                rec.solution,
                _dumps(rec.conditions),
                _dumps(rec.provenance),
                rec.status,
                rec.version,
                rec.success_count,
                rec.fail_count,
                rec.last_verified_at,
                rec.created_at,
                rec.updated_at,
            ),
        )
        self._snapshot(rec.id, rec.version, rec.to_dict(), "创建")
        self.conn.commit()
        self.add_event("write", memory_id=rec.id, detail={"kind": rec.kind})
        return rec.id

    def update(self, memory_id: str, changes: Dict[str, Any], note: str = "") -> int:
        """更新字段并升版本。返回新版本号。

        changes 只接受白名单字段，避免误改 id / created_at 这类不可变项。
        """
        rec = self.get(memory_id)
        if rec is None:
            raise KeyError(f"记忆不存在: {memory_id}")

        updatable = {
            "title", "symptom", "diagnosis", "solution",
            "conditions", "provenance", "status",
            "success_count", "fail_count", "last_verified_at",
        }
        unknown = set(changes) - updatable
        if unknown:
            raise ValueError(f"不可更新字段: {sorted(unknown)}")

        # 先留档当前版本，再升版本写入
        self._snapshot(rec.id, rec.version, rec.to_dict(), note or "更新")

        new_version = rec.version + 1
        sets: List[str] = []
        params: List[Any] = []
        for key, val in changes.items():
            if key in ("conditions", "provenance"):
                sets.append(f"{key}=?")
                params.append(_dumps(val))
            else:
                sets.append(f"{key}=?")
                params.append(val)
        sets.append("version=?")
        params.append(new_version)
        sets.append("updated_at=?")
        params.append(now_iso())
        params.append(memory_id)

        self.conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()

        # 新版本同样留档，保证版本链连续可回溯（v1, v2, ...）
        fresh = self.get(memory_id)
        if fresh is not None:
            self._snapshot(memory_id, new_version, fresh.to_dict(), note or "更新")
            self.conn.commit()
        return new_version

    def set_status(self, memory_id: str, status: str, note: str = "") -> int:
        """状态流转。所有变更都留档，便于回放。"""
        if status not in ALL_STATUSES:
            raise ValueError(f"未知状态: {status}")
        version = self.update(memory_id, {"status": status}, note or f"状态转为 {status}")
        self.add_event("expire" if status != STATUS_ACTIVE else "write",
                       memory_id=memory_id, detail={"status": status})
        return version

    def record_verify(self, memory_id: str, ok: bool) -> int:
        """记录一次验证结果，刷新成功率与最近验证时间。"""
        rec = self.get(memory_id)
        if rec is None:
            raise KeyError(f"记忆不存在: {memory_id}")
        changes = {
            "success_count": rec.success_count + (1 if ok else 0),
            "fail_count": rec.fail_count + (0 if ok else 1),
            "last_verified_at": now_iso(),
        }
        return self.update(memory_id, changes, "验证结果回写")

    # ── 读取 ──────────────────────────────────────────────────────────
    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._to_record(row) if row else None

    def list(
        self,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[MemoryRecord]:
        sql = "SELECT * FROM memories WHERE 1=1"
        params: List[Any] = []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.conn.execute(sql, params).fetchall()
        return [self._to_record(r) for r in rows]

    def count(self, kind: Optional[str] = None, status: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM memories WHERE 1=1"
        params: List[Any] = []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if status:
            sql += " AND status=?"
            params.append(status)
        return int(self.conn.execute(sql, params).fetchone()["c"])

    # ── 环境指纹 ──────────────────────────────────────────────────────
    def put_env(self, memory_id: str, fp: EnvFingerprint) -> None:
        self.conn.execute(
            """INSERT INTO memory_env
               (memory_id, os_id, os_version, os_sp, kernel, arch, deps, captured_at, verify_result)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(memory_id) DO UPDATE SET
                 os_id=excluded.os_id, os_version=excluded.os_version,
                 os_sp=excluded.os_sp, kernel=excluded.kernel,
                 arch=excluded.arch, deps=excluded.deps,
                 captured_at=excluded.captured_at, verify_result=excluded.verify_result""",
            (
                memory_id,
                fp.os_id,
                fp.os_version,
                fp.os_sp,
                fp.kernel,
                fp.arch,
                _dumps(fp.deps),
                fp.captured_at or now_iso(),
                fp.verify_result,
            ),
        )
        self.conn.commit()

    def get_env(self, memory_id: str) -> Optional[EnvFingerprint]:
        row = self.conn.execute(
            "SELECT * FROM memory_env WHERE memory_id=?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        return EnvFingerprint(
            os_id=row["os_id"],
            os_version=row["os_version"],
            os_sp=row["os_sp"],
            kernel=row["kernel"],
            arch=row["arch"],
            deps=_loads(row["deps"], {}),
            captured_at=row["captured_at"],
            verify_result=row["verify_result"],
        )

    def all_envs(self) -> Dict[str, EnvFingerprint]:
        """批量读取指纹，供检索时一次性装载，避免 N+1 查询。"""
        rows = self.conn.execute("SELECT * FROM memory_env").fetchall()
        out: Dict[str, EnvFingerprint] = {}
        for row in rows:
            out[row["memory_id"]] = EnvFingerprint(
                os_id=row["os_id"],
                os_version=row["os_version"],
                os_sp=row["os_sp"],
                kernel=row["kernel"],
                arch=row["arch"],
                deps=_loads(row["deps"], {}),
                captured_at=row["captured_at"],
                verify_result=row["verify_result"],
            )
        return out

    # ── 版本 ──────────────────────────────────────────────────────────
    def _snapshot(self, memory_id: str, version: int, data: Dict[str, Any], note: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO memory_versions
               (memory_id, version, snapshot, changed_at, change_note)
               VALUES (?,?,?,?,?)""",
            (memory_id, version, _dumps(data), now_iso(), note),
        )

    def versions(self, memory_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM memory_versions WHERE memory_id=? ORDER BY version",
            (memory_id,),
        ).fetchall()
        return [
            {
                "version": r["version"],
                "changed_at": r["changed_at"],
                "change_note": r["change_note"],
                "snapshot": _loads(r["snapshot"], {}),
            }
            for r in rows
        ]

    # ── 事件 ──────────────────────────────────────────────────────────
    def add_event(
        self,
        event: str,
        memory_id: Optional[str] = None,
        session_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO memory_events(memory_id, session_id, event, detail, ts) VALUES (?,?,?,?,?)",
            (memory_id, session_id, event, _dumps(detail or {}), now_iso()),
        )
        self.conn.commit()

    def events(
        self,
        session_id: Optional[str] = None,
        memory_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM memory_events WHERE 1=1"
        params: List[Any] = []
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        if memory_id:
            sql += " AND memory_id=?"
            params.append(memory_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"],
                "memory_id": r["memory_id"],
                "session_id": r["session_id"],
                "event": r["event"],
                "detail": _loads(r["detail"], {}),
                "ts": r["ts"],
            }
            for r in rows
        ]

    # ── 冲突 ──────────────────────────────────────────────────────────
    def add_conflict(
        self,
        memory_a: str,
        memory_b: str,
        dimension: str,
        resolution: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO memory_conflicts(memory_a, memory_b, dimension, resolution, detected_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(memory_a, memory_b, dimension) DO UPDATE SET
                 resolution=excluded.resolution, detected_at=excluded.detected_at""",
            (memory_a, memory_b, dimension, resolution, now_iso()),
        )
        self.conn.commit()

    def conflicts(self, memory_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM memory_conflicts"
        params: List[Any] = []
        if memory_id:
            sql += " WHERE memory_a=? OR memory_b=?"
            params.extend([memory_id, memory_id])
        sql += " ORDER BY detected_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── 删除 ──────────────────────────────────────────────────────────
    def delete(self, memory_id: str, scope: str = "storage", residual: Optional[str] = None) -> None:
        """软删除：置为 deleted 并写墓碑。记录仍在库内，可审计。"""
        self.update(memory_id, {"status": STATUS_DELETED}, "删除")
        self.conn.execute(
            """INSERT INTO memory_tombstones(memory_id, deleted_at, scope, residual_check)
               VALUES (?,?,?,?)
               ON CONFLICT(memory_id) DO UPDATE SET
                 deleted_at=excluded.deleted_at, scope=excluded.scope,
                 residual_check=excluded.residual_check""",
            (memory_id, now_iso(), scope, residual),
        )
        self.conn.commit()
        self.add_event("delete", memory_id=memory_id, detail={"scope": scope, "residual": residual})

    def purge(self, memory_id: str) -> bool:
        """物理删除记忆与指纹，保留墓碑作为删除证据。"""
        cur = self.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def tombstones(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM memory_tombstones ORDER BY deleted_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 统计 ──────────────────────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        by_kind = {
            r["kind"]: r["c"]
            for r in self.conn.execute(
                "SELECT kind, COUNT(*) AS c FROM memories GROUP BY kind"
            ).fetchall()
        }
        by_status = {
            r["status"]: r["c"]
            for r in self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM memories GROUP BY status"
            ).fetchall()
        }
        return {
            "total": self.count(),
            "by_kind": by_kind,
            "by_status": by_status,
            "envs": self.conn.execute("SELECT COUNT(*) AS c FROM memory_env").fetchone()["c"],
            "events": self.conn.execute("SELECT COUNT(*) AS c FROM memory_events").fetchone()["c"],
        }

    # ── 内部 ──────────────────────────────────────────────────────────
    @staticmethod
    def _to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            symptom=row["symptom"],
            diagnosis=row["diagnosis"] or "",
            solution=row["solution"] or "",
            conditions=_loads(row["conditions"], {}),
            provenance=_loads(row["provenance"], {}),
            status=row["status"],
            version=row["version"],
            success_count=row["success_count"],
            fail_count=row["fail_count"],
            last_verified_at=row["last_verified_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
