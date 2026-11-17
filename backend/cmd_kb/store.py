"""命令知识库存储

三层：
1. 预置 JSON（cmd_db.json）
2. 运行时采集（compgen -c）
3. SQLite 持久化（合并层）

内存维护：name → CmdEntry 字典
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.config import get_setting
from utils.logger import get_logger

logger = get_logger("cmd_kb.store")


@dataclass
class CmdEntry:
    """单条命令的完整元数据。"""
    name: str
    category: str = "未分类"
    desc: str = ""
    risk: str = "low"            # low | medium | high
    type: str = "external"       # builtin | external
    common_args: List[str] = field(default_factory=list)
    dangerous_args: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)
    source: str = "preset"       # preset | runtime | manual

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_text(self) -> str:
        """生成供 LLM/Embedding 的文本。"""
        parts = [self.name, self.category, self.desc]
        if self.common_args:
            parts.append("常用参数：" + " ".join(self.common_args))
        if self.dangerous_args:
            parts.append("危险参数：" + " ".join(self.dangerous_args))
        if self.examples:
            parts.append("示例：" + "; ".join(self.examples))
        return "。".join(parts)


class CmdStore:
    """命令库存储。"""

    def __init__(self, preset_path: Optional[str] = None, sqlite_path: Optional[str] = None) -> None:
        if preset_path is None:
            preset_path = get_setting("cmd_kb.preset_path", "./cmd_kb/data/cmd_db.json")
        if sqlite_path is None:
            sqlite_path = get_setting("cmd_kb.sqlite_path", "./data/cmd_kb.db")

        self.preset_path = Path(preset_path)
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._entries: Dict[str, CmdEntry] = {}
        self._init_schema()
        self._load_all()

    def _init_schema(self) -> None:
        with self._lock:
            conn = sqlite3.connect(str(self.sqlite_path))
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS commands (
                    name TEXT PRIMARY KEY,
                    category TEXT,
                    desc TEXT,
                    risk TEXT,
                    type TEXT,
                    common_args TEXT,
                    dangerous_args TEXT,
                    examples TEXT,
                    source TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_cmd_category ON commands(category);
                CREATE INDEX IF NOT EXISTS idx_cmd_risk ON commands(risk);
            """)
            conn.commit()
            conn.close()

    def _load_all(self) -> None:
        """加载顺序：预置 → SQLite 增量（运行时采集的）→ 后者覆盖前者同名。"""
        self._load_preset()
        self._load_sqlite()

    def _load_preset(self) -> None:
        if not self.preset_path.exists():
            logger.warning(f"preset not found: {self.preset_path}")
            return
        try:
            raw = json.loads(self.preset_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"failed to load preset: {e}")
            return
        for item in raw:
            entry = CmdEntry(
                name=item["name"],
                category=item.get("category", "未分类"),
                desc=item.get("desc", ""),
                risk=item.get("risk", "low"),
                type=item.get("type", "external"),
                common_args=item.get("common_args", []),
                dangerous_args=item.get("dangerous_args", []),
                examples=item.get("examples", []),
                source="preset",
            )
            self._entries[entry.name] = entry
        logger.info(f"loaded {len(raw)} preset commands")

    def _load_sqlite(self) -> None:
        if not self.sqlite_path.exists():
            return
        try:
            conn = sqlite3.connect(str(self.sqlite_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM commands WHERE source != 'preset' OR source IS NULL").fetchall()
            for r in rows:
                entry = CmdEntry(
                    name=r["name"],
                    category=r["category"] or "未分类",
                    desc=r["desc"] or "",
                    risk=r["risk"] or "low",
                    type=r["type"] or "external",
                    common_args=json.loads(r["common_args"] or "[]"),
                    dangerous_args=json.loads(r["dangerous_args"] or "[]"),
                    examples=json.loads(r["examples"] or "[]"),
                    source=r["source"] or "manual",
                )
                self._entries[entry.name] = entry
            conn.close()
        except Exception as e:
            logger.error(f"failed to load sqlite: {e}")

    # ---- CRUD ----

    def get(self, name: str) -> Optional[CmdEntry]:
        return self._entries.get(name)

    def has(self, name: str) -> bool:
        return name in self._entries

    def all(self) -> List[CmdEntry]:
        return list(self._entries.values())

    def all_names(self) -> List[str]:
        return list(self._entries.keys())

    def by_category(self, category: str) -> List[CmdEntry]:
        return [e for e in self._entries.values() if e.category == category]

    def by_risk(self, risk: str) -> List[CmdEntry]:
        return [e for e in self._entries.values() if e.risk == risk]

    def upsert(self, entry: CmdEntry, persist: bool = True) -> None:
        """插入或更新。persist=True 时同步到 SQLite。"""
        self._entries[entry.name] = entry
        if persist:
            self._persist(entry)

    def upsert_many(self, entries: List[CmdEntry], persist: bool = True) -> int:
        n = 0
        for e in entries:
            if not self.has(e.name):
                self.upsert(e, persist=persist)
                n += 1
            elif e.source != "preset":
                # 运行时采集的允许覆盖预设
                self.upsert(e, persist=persist)
                n += 1
        return n

    def _persist(self, entry: CmdEntry) -> None:
        with self._lock:
            try:
                conn = sqlite3.connect(str(self.sqlite_path))
                conn.execute("""
                    INSERT INTO commands(name, category, desc, risk, type, common_args,
                                         dangerous_args, examples, source, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)
                    ON CONFLICT(name) DO UPDATE SET
                        category=excluded.category,
                        desc=excluded.desc,
                        risk=excluded.risk,
                        type=excluded.type,
                        common_args=excluded.common_args,
                        dangerous_args=excluded.dangerous_args,
                        examples=excluded.examples,
                        source=excluded.source,
                        updated_at=CURRENT_TIMESTAMP
                """, (
                    entry.name, entry.category, entry.desc, entry.risk, entry.type,
                    json.dumps(entry.common_args, ensure_ascii=False),
                    json.dumps(entry.dangerous_args, ensure_ascii=False),
                    json.dumps(entry.examples, ensure_ascii=False),
                    entry.source,
                ))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"persist {entry.name} failed: {e}")

    def stats(self) -> Dict[str, Any]:
        by_cat: Dict[str, int] = {}
        by_risk: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        for e in self._entries.values():
            by_cat[e.category] = by_cat.get(e.category, 0) + 1
            by_risk[e.risk] = by_risk.get(e.risk, 0) + 1
            by_source[e.source] = by_source.get(e.source, 0) + 1
        return {
            "total": len(self._entries),
            "by_category": by_cat,
            "by_risk": by_risk,
            "by_source": by_source,
        }


_default_store: Optional[CmdStore] = None


def get_default_store() -> CmdStore:
    """获取全局默认 store（懒加载）。"""
    global _default_store
    if _default_store is None:
        _default_store = CmdStore()
    return _default_store


def reset_default_store() -> None:
    """测试用：重置全局 store。"""
    global _default_store
    _default_store = None