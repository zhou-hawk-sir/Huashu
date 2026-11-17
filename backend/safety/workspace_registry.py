"""会话级 workspace 注册表

进程内 Dict[str, str] + RLock。热路径查询用。
单进程够用；分布式部署时把 dict 换成 Redis（已留接口）。
"""
from __future__ import annotations

import threading
from typing import Dict, Optional


class WorkspaceRegistry:
    """session_id → workspace_root 映射（realpath 后的规范化路径）。"""

    def __init__(self) -> None:
        self._store: Dict[str, str] = {}
        self._lock = threading.RLock()

    def set(self, session_id: str, root: str) -> None:
        with self._lock:
            self._store[session_id] = root

    def get(self, session_id: str) -> Optional[str]:
        with self._lock:
            return self._store.get(session_id)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

    def list(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._store)