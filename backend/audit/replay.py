"""会话回溯：从 SQLite 重建整条思维链"""
from __future__ import annotations

from typing import Any, Dict, List

from .audit_db import AuditDB


def replay_session(session_id: str, db_path: str = "") -> Dict[str, Any]:
    """回放一个会话的完整链路。

    Returns:
        {
          "session": {...},
          "messages": [...],
          "traces": [...],
          "tool_invocations": [...]
        }
    """
    db = AuditDB(db_path) if db_path else AuditDB()

    traces = db.list_traces(session_id)
    messages = db.list_messages(session_id)
    invocations = db.list_tool_invocations(session_id)

    return {
        "session_id": session_id,
        "messages": messages,
        "traces": traces,
        "tool_invocations": invocations,
    }