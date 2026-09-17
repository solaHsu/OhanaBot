"""记忆系统 SQLite 连接、迁移与事务。

设计依据 analysis/proposals/memory_plan_v0.2.md §5：
SQLite 为权威数据，Markdown 只是导出的只读快照。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path("runtime/ohana.sqlite3")
SCHEMA_VERSION = 4

# 增量迁移（目标版本号 → SQL 语句）；新库直接按 schema.sql 建到最新版本。
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: ("ALTER TABLE messages ADD COLUMN mentions TEXT NOT NULL DEFAULT '[]'",),
    3: (
        """
        CREATE TABLE IF NOT EXISTS knowledge_docs (
            doc_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            source       TEXT NOT NULL DEFAULT '',
            kind         TEXT NOT NULL DEFAULT 'knowledge',
            content      TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            created_at   TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            chunk_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id    INTEGER NOT NULL REFERENCES knowledge_docs (doc_id) ON DELETE CASCADE,
            seq       INTEGER NOT NULL,
            text      TEXT NOT NULL,
            tokens    TEXT NOT NULL DEFAULT '[]'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_doc ON knowledge_chunks (doc_id)",
        """
        CREATE TABLE IF NOT EXISTS knowledge_entities (
            entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            name_key  TEXT NOT NULL UNIQUE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_edges (
            edge_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            src_id    INTEGER NOT NULL REFERENCES knowledge_entities (entity_id) ON DELETE CASCADE,
            dst_id    INTEGER NOT NULL REFERENCES knowledge_entities (entity_id) ON DELETE CASCADE,
            relation  TEXT NOT NULL DEFAULT '',
            doc_id    INTEGER NOT NULL REFERENCES knowledge_docs (doc_id) ON DELETE CASCADE,
            UNIQUE (src_id, dst_id, relation, doc_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_knowledge_edges_src ON knowledge_edges (src_id)",
        """
        CREATE TABLE IF NOT EXISTS knowledge_chunk_entities (
            chunk_id  INTEGER NOT NULL REFERENCES knowledge_chunks (chunk_id) ON DELETE CASCADE,
            entity_id INTEGER NOT NULL REFERENCES knowledge_entities (entity_id) ON DELETE CASCADE,
            PRIMARY KEY (chunk_id, entity_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_entities_entity ON knowledge_chunk_entities (entity_id)",
    ),
    4: (
        """
        CREATE TABLE IF NOT EXISTS knowledge_summaries (
            summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope      TEXT NOT NULL,
            target_key TEXT NOT NULL,
            title      TEXT NOT NULL,
            text       TEXT NOT NULL,
            members    TEXT NOT NULL DEFAULT '[]',
            digest     TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE (scope, target_key)
        )
        """,
    ),
}


def now_iso() -> str:
    """当前 UTC 时间（ISO8601，秒精度）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """打开（必要时创建）数据库，返回启用 WAL 的连接。

    连接为共享对象（check_same_thread=False）：跨线程访问必须由调用方串行化
    （当前写入路径由 ingest.MessageIngestor 的锁保证；健康检查用独立临时连接）。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """按 PRAGMA user_version 迁移：新库应用完整 schema，旧库按版本逐个增量升级。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    try:
        if version == 0:
            schema = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
            conn.executescript(schema)
        else:
            for step in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS.get(step, ()):
                    conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """事务上下文：成功提交，异常回滚。"""
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
