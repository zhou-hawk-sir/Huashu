"""记忆库建表与迁移

独立于 audit.db：审计表是只增不改的证据链，记忆要频繁更新与删除，
混在一起会破坏审计表的写入假设。两库通过 session_id 关联。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger("memory.schema")

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _ROOT / "data" / "memory.db"

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- 1. 记忆主表
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL,
    symptom         TEXT NOT NULL,
    diagnosis       TEXT,
    solution        TEXT,
    conditions      TEXT,
    provenance      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    version         INTEGER NOT NULL DEFAULT 1,
    success_count   INTEGER NOT NULL DEFAULT 0,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    last_verified_at TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_kind_status ON memories(kind, status);
CREATE INDEX IF NOT EXISTS idx_mem_verify ON memories(last_verified_at);

-- 2. 环境指纹（一对一）
CREATE TABLE IF NOT EXISTS memory_env (
    memory_id       TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    os_id           TEXT NOT NULL,
    os_version      TEXT NOT NULL,
    os_sp           TEXT NOT NULL,
    kernel          TEXT NOT NULL,
    arch            TEXT NOT NULL,
    deps            TEXT,
    captured_at     TEXT NOT NULL,
    verify_result   TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_env_arch ON memory_env(arch, os_sp);

-- 3. 版本快照（更新即留档）
CREATE TABLE IF NOT EXISTS memory_versions (
    memory_id       TEXT NOT NULL,
    version         INTEGER NOT NULL,
    snapshot        TEXT NOT NULL,
    changed_at      TEXT NOT NULL,
    change_note     TEXT,
    PRIMARY KEY (memory_id, version)
);

-- 4. 冲突关系
CREATE TABLE IF NOT EXISTS memory_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_a        TEXT NOT NULL,
    memory_b        TEXT NOT NULL,
    dimension       TEXT NOT NULL,
    resolution      TEXT,
    detected_at     TEXT NOT NULL,
    UNIQUE(memory_a, memory_b, dimension)
);
CREATE INDEX IF NOT EXISTS idx_conflict_a ON memory_conflicts(memory_a);

-- 5. 记忆事件（供审计与回放）
CREATE TABLE IF NOT EXISTS memory_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       TEXT,
    session_id      TEXT,
    event           TEXT NOT NULL,
    detail          TEXT,
    ts              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evt_session ON memory_events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_evt_memory ON memory_events(memory_id, ts);

-- 6. 删除墓碑（可验证删除）
CREATE TABLE IF NOT EXISTS memory_tombstones (
    memory_id       TEXT PRIMARY KEY,
    deleted_at      TEXT NOT NULL,
    scope           TEXT NOT NULL,
    residual_check  TEXT
);

-- 7. 元信息（记录 schema 版本，供后续迁移）
CREATE TABLE IF NOT EXISTS memory_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
"""


def default_db_path() -> Path:
    """默认库路径。可通过 settings.yaml 的 memory.db_path 覆盖。"""
    try:
        from utils.config import get_setting

        configured = get_setting("memory.db_path", "")
        if configured:
            p = Path(configured)
            return p if p.is_absolute() else _ROOT / p
    except Exception:  # 配置缺失时退回默认值，不阻断启动
        pass
    return _DEFAULT_DB


def connect(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    """打开连接并设置合理的 pragma。

    WAL 模式让读写并发不互相阻塞；外键约束保证删除记忆时指纹级联清理。
    """
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> int:
    """建表并写入 schema 版本。幂等，可重复调用。"""
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO memory_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    else:
        current = int(row["value"])
        if current != SCHEMA_VERSION:
            logger.info("memory schema 版本 %s -> %s", current, SCHEMA_VERSION)
            conn.execute(
                "UPDATE memory_meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
    conn.commit()
    return SCHEMA_VERSION


def get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def table_names(conn: sqlite3.Connection) -> list[str]:
    """列出所有表名，供自检与测试使用。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]
