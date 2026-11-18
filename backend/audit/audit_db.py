"""SQLite 审计数据库"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.config import get_setting
from utils.logger import get_logger
from utils.redactor import redact
from .tracer import TraceStep

logger = get_logger("audit.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    ts TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    session_id TEXT NOT NULL,
    step TEXT,
    actor TEXT,
    input TEXT,
    output TEXT,
    rationale TEXT,
    user_confirmed INTEGER,
    ts TEXT
);

CREATE TABLE IF NOT EXISTS tool_invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    trace_id TEXT,
    tool_name TEXT,
    args TEXT,
    risk_level TEXT,
    decision TEXT,
    result TEXT,
    duration_ms INTEGER,
    ts TEXT
);

CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_invocations(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, ts);
"""


class AuditDB:
    """SQLite 审计数据库（线程安全）。"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            db_path = get_setting("audit.sqlite_path", "./data/audit.db")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # ---- sessions ----

    def upsert_session(self, session_id: str, user_id: str = "", metadata: Optional[dict] = None) -> None:
        now = datetime.utcnow().isoformat() + "Z"
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE sessions SET updated_at=?, metadata=? WHERE id=?",
                    (now, json.dumps(metadata or {}, ensure_ascii=False), session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO sessions(id, user_id, created_at, updated_at, metadata) VALUES (?,?,?,?,?)",
                    (
                        session_id,
                        user_id,
                        now,
                        now,
                        json.dumps(metadata or {}, ensure_ascii=False),
                    ),
                )
            conn.commit()

    def list_sessions(self) -> List[Dict[str, Any]]:
        """列出所有 session，updated_at 取 max(session.updated_at, 最新消息 ts)。

        按 updated_at 降序返回（最近活跃的在前）。
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id AS id,
                       s.user_id AS user_id,
                       s.created_at AS created_at,
                       s.metadata AS metadata,
                       MAX(
                           COALESCE(s.updated_at, ''),
                           COALESCE((SELECT MAX(m.ts) FROM messages m
                                     WHERE m.session_id = s.id), '')
                       ) AS updated_at
                FROM sessions s
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """返回单个 session 摘要（含 updated_at，与 list_sessions 同格式）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.id AS id,
                       s.user_id AS user_id,
                       s.created_at AS created_at,
                       s.metadata AS metadata,
                       MAX(
                           COALESCE(s.updated_at, ''),
                           COALESCE((SELECT MAX(m.ts) FROM messages m
                                     WHERE m.session_id = s.id), '')
                       ) AS updated_at
                FROM sessions s
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            return dict(row) if row and row["id"] is not None else None

    def delete_session(self, session_id: str) -> bool:
        """删除一个会话及其所有关联数据（messages / traces / tool_invocations）。

        返回 True 表示删除了至少一行；False 表示会话不存在。
        """
        with self._lock, self._connect() as conn:
            # 关联表级联清理
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM traces WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM tool_invocations WHERE session_id = ?", (session_id,))
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return cur.rowcount > 0

    # ---- messages ----
    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: Any = None,
        tool_call_id: str = "",
    ) -> int:
        now = datetime.utcnow().isoformat() + "Z"
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages(session_id, role, content, tool_calls, tool_call_id, ts) "
                "VALUES (?,?,?,?,?,?)",
                (
                    session_id,
                    role,
                    content,
                    json.dumps(tool_calls or [], ensure_ascii=False) if tool_calls else None,
                    tool_call_id,
                    now,
                ),
            )
            conn.commit()
            return cur.lastrowid

    def list_messages(self, session_id: str, limit: int = 60) -> List[Dict[str, Any]]:
        """列出 session 的最近消息（默认最近 60 条 = 30 轮 user+assistant）。

        一轮 = 1 条 user + 1 条 assistant + 若干 tool result，所以 30 轮约 60-120 条。
        按 id DESC 取最近的 limit 条，再反转为时序正序返回。
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    # ---- traces ----

    def add_trace(self, step: TraceStep) -> Optional[int]:
        """写入一条追踪节点，返回自增主键。

        返回值供 Tracer 回填到 step.node_id：记忆的 provenance 存下这个主键，
        就能从一条记忆反查到产生它的决策节点。
        """
        # H2: 双保险——即便上游忘了脱敏，落库前再过一遍 redact
        safe_input = redact(step.input) if step.input else step.input
        safe_output = redact(step.output) if step.output else step.output
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO traces(trace_id, session_id, step, actor, input, output, "
                "rationale, user_confirmed, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    step.trace_id,
                    step.session_id,
                    step.step.value,
                    step.actor.value,
                    json.dumps(safe_input, ensure_ascii=False),
                    json.dumps(safe_output, ensure_ascii=False),
                    step.decision_rationale,
                    1 if step.user_confirmed else (0 if step.user_confirmed is False else None),
                    step.ts,
                ),
            )
            conn.commit()
            return int(cur.lastrowid) if cur.lastrowid is not None else None

    def list_traces(self, session_id: str) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM traces WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_traces_by_trace_id(self, trace_id: str) -> List[Dict[str, Any]]:
        """按 trace_id 取整条决策链（同一次用户请求内的全部节点）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM traces WHERE trace_id=? ORDER BY id",
                (trace_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- tool invocations ----

    def add_tool_invocation(
        self,
        session_id: str,
        trace_id: str,
        tool_name: str,
        args: Dict[str, Any],
        risk_level: str,
        decision: str,
        result: Any,
        duration_ms: int,
    ) -> None:
        now = datetime.utcnow().isoformat() + "Z"
        # H2: 工具调用的 args/result 直接是原始 dict（不经 tracer），落库前过 redact
        safe_args = redact(args) if args else args
        safe_result = redact(result) if result is not None else result
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO tool_invocations(session_id, trace_id, tool_name, args, risk_level, "
                "decision, result, duration_ms, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    trace_id,
                    tool_name,
                    json.dumps(safe_args, ensure_ascii=False),
                    risk_level,
                    decision,
                    json.dumps(safe_result, ensure_ascii=False) if safe_result is not None else None,
                    duration_ms,
                    now,
                ),
            )
            conn.commit()

    def list_tool_invocations(self, session_id: str) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_invocations WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]