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
SCHEMA_VERSION = 2

# 增量迁移（目标版本号 → SQL 语句）；新库直接按 schema.sql 建到最新版本。
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: ("ALTER TABLE messages ADD COLUMN mentions TEXT NOT NULL DEFAULT '[]'",),
}


def now_iso() -> str:
    """当前 UTC 时间（ISO8601，秒精度）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """打开（必要时创建）数据库，返回启用 WAL 的连接。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
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
