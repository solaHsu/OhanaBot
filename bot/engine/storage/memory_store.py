"""记忆系统数据访问：五张记忆表 + 消息表。

设计依据 analysis/proposals/memory_plan_v0.2.md：
- 索引 = 会话 × 人；写操作均走事务；
- 基本画像整段覆盖；称呼（群昵称历史 / 绰号）与条目只增不改；
- 日常记录按段追加（每会话每满 200 条消息压缩一段），消息游标记录压缩进度。
"""

from __future__ import annotations

import json
import sqlite3

from bot.engine.storage.db import now_iso, transaction

# 基本画像的固定维度（顺序即快照渲染顺序）
BASIC_FIELDS = ("personality", "speech", "interests", "preferences", "relations", "other")

NAME_GROUP_NICKNAME = "group_nickname"
NAME_ALIAS = "alias"


class MemoryStore:
    """记忆相关表的读写封装。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------- 会话 ----------

    def ensure_conversation(self, platform: str, conversation_id: str, title: str | None = None) -> None:
        """登记会话；title 非空时刷新群名缓存。"""
        with transaction(self.conn) as c:
            c.execute(
                """
                INSERT INTO conversations (platform, conversation_id, title, cursor, updated_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT (platform, conversation_id) DO UPDATE SET
                    title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE conversations.title END,
                    updated_at = excluded.updated_at
                """,
                (platform, conversation_id, title or "", now_iso()),
            )

    def list_conversations(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM conversations ORDER BY platform, conversation_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def cursor(self, platform: str, conversation_id: str) -> int:
        """已压缩到的消息位置（messages.id）；会话不存在时返回 0。"""
        row = self.conn.execute(
            "SELECT cursor FROM conversations WHERE platform = ? AND conversation_id = ?",
            (platform, conversation_id),
        ).fetchone()
        return int(row["cursor"]) if row else 0

    def advance_cursor(self, platform: str, conversation_id: str, message_id: int) -> None:
        """推进游标（只前进，不回退）。"""
        with transaction(self.conn) as c:
            c.execute(
                """
                UPDATE conversations SET cursor = MAX(cursor, ?), updated_at = ?
                WHERE platform = ? AND conversation_id = ?
                """,
                (message_id, now_iso(), platform, conversation_id),
            )

    # ---------- 消息 ----------

    def add_message(
        self,
        platform: str,
        conversation_id: str,
        message_id: str,
        user_id: str,
        display_name: str,
        text: str,
        sent_at: str,
        mentions: list[str] | None = None,
    ) -> int:
        """写入一条消息；重复消息（同会话同平台消息 ID）返回已有行 id。"""
        with transaction(self.conn) as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO messages
                    (platform, conversation_id, message_id, user_id, display_name, mentions, text, sent_at, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    conversation_id,
                    message_id,
                    user_id,
                    display_name or "",
                    json.dumps(mentions or [], ensure_ascii=False),
                    text,
                    sent_at,
                    now_iso(),
                ),
            )
            if cur.rowcount == 0:
                row = c.execute(
                    "SELECT id FROM messages WHERE platform = ? AND conversation_id = ? AND message_id = ?",
                    (platform, conversation_id, message_id),
                ).fetchone()
                return int(row["id"])
            return int(cur.lastrowid)

    def count_pending_messages(self, platform: str, conversation_id: str) -> int:
        """游标之后未压缩的消息数（满 200 触发更新任务）。"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE platform = ? AND conversation_id = ? AND id > ?",
            (platform, conversation_id, self.cursor(platform, conversation_id)),
        ).fetchone()
        return int(row[0])

    def fetch_message_batch(self, platform: str, conversation_id: str, limit: int = 200) -> list[dict]:
        """取游标之后的一批消息（按顺序），即更新任务的输入；mentions 解析为列表。"""
        rows = self.conn.execute(
            """
            SELECT id, message_id, user_id, display_name, mentions, text, sent_at
            FROM messages
            WHERE platform = ? AND conversation_id = ? AND id > ?
            ORDER BY id LIMIT ?
            """,
            (platform, conversation_id, self.cursor(platform, conversation_id), limit),
        ).fetchall()
        batch = []
        for row in rows:
            item = dict(row)
            item["mentions"] = json.loads(item["mentions"] or "[]")
            batch.append(item)
        return batch

    # ---------- 人物 ----------

    def upsert_person_basic(self, platform: str, conversation_id: str, user_id: str, basic: dict) -> None:
        """基本画像整段覆盖（只保留固定维度，多余字段忽略）。"""
        payload = _basic_json(basic)
        with transaction(self.conn) as c:
            c.execute(
                """
                INSERT INTO persons (platform, conversation_id, user_id, basic, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (platform, conversation_id, user_id) DO UPDATE SET
                    basic = excluded.basic, updated_at = excluded.updated_at
                """,
                (platform, conversation_id, user_id, payload, now_iso()),
            )

    def get_person(self, platform: str, conversation_id: str, user_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM persons WHERE platform = ? AND conversation_id = ? AND user_id = ?",
            (platform, conversation_id, user_id),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["basic"] = json.loads(data["basic"])
        return data

    def list_user_ids(self, platform: str, conversation_id: str) -> list[str]:
        """该会话下出现过资料的人（persons / names / entries 的并集）。"""
        rows = self.conn.execute(
            """
            SELECT user_id FROM persons WHERE platform = ? AND conversation_id = ?
            UNION SELECT user_id FROM names WHERE platform = ? AND conversation_id = ?
            UNION SELECT user_id FROM entries WHERE platform = ? AND conversation_id = ?
            ORDER BY user_id
            """,
            (platform, conversation_id, platform, conversation_id, platform, conversation_id),
        ).fetchall()
        return [r["user_id"] for r in rows]

    # ---------- 称呼 ----------

    def record_group_nickname(
        self, platform: str, conversation_id: str, user_id: str, text: str, seen_at: str
    ) -> bool:
        """群昵称历史（程序记录）：与最新一条相同则只刷新 last_seen，否则追加新行。"""
        with transaction(self.conn) as c:
            return _record_nickname(c, platform, conversation_id, user_id, text, seen_at)

    def refresh_nicknames(self, platform: str, conversation_id: str, up_to_id: int) -> int:
        """把游标到 up_to_id 之间消息里的群昵称变更记入历史（单事务），返回新增行数。"""
        rows = self.conn.execute(
            """
            SELECT user_id, display_name, sent_at FROM messages
            WHERE platform = ? AND conversation_id = ? AND id > ? AND id <= ? AND display_name <> ''
            ORDER BY id
            """,
            (platform, conversation_id, self.cursor(platform, conversation_id), up_to_id),
        ).fetchall()
        changed = 0
        with transaction(self.conn) as c:
            for row in rows:
                if _record_nickname(
                    c, platform, conversation_id, row["user_id"], row["display_name"], row["sent_at"]
                ):
                    changed += 1
        return changed

    def add_alias(self, platform: str, conversation_id: str, user_id: str, text: str, seen_at: str) -> bool:
        """绰号（LLM 识别）：已存在则只刷新 last_seen，否则追加。"""
        with transaction(self.conn) as c:
            row = c.execute(
                """
                SELECT id FROM names
                WHERE platform = ? AND conversation_id = ? AND user_id = ? AND kind = ? AND text = ?
                """,
                (platform, conversation_id, user_id, NAME_ALIAS, text),
            ).fetchone()
            if row is not None:
                c.execute("UPDATE names SET last_seen = ? WHERE id = ?", (seen_at, row["id"]))
                return False
            c.execute(
                """
                INSERT INTO names (platform, conversation_id, user_id, kind, text, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, conversation_id, user_id, NAME_ALIAS, text, seen_at, seen_at),
            )
            return True

    def list_names(
        self, platform: str, conversation_id: str, user_id: str, kind: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM names WHERE platform = ? AND conversation_id = ? AND user_id = ?"
        params: list = [platform, conversation_id, user_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---------- 条目 ----------

    def add_entry(self, platform: str, conversation_id: str, user_id: str, text: str) -> bool:
        """追加条目；完全相同的文本直接跳过（只增不改）。"""
        with transaction(self.conn) as c:
            row = c.execute(
                """
                SELECT id FROM entries
                WHERE platform = ? AND conversation_id = ? AND user_id = ? AND text = ?
                """,
                (platform, conversation_id, user_id, text),
            ).fetchone()
            if row is not None:
                return False
            c.execute(
                """
                INSERT INTO entries (platform, conversation_id, user_id, text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (platform, conversation_id, user_id, text, now_iso()),
            )
            return True

    def list_entries(self, platform: str, conversation_id: str, user_id: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT * FROM entries
            WHERE platform = ? AND conversation_id = ? AND user_id = ?
            ORDER BY id
            """,
            (platform, conversation_id, user_id),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 日常记录 ----------

    def append_log(self, platform: str, conversation_id: str, msg_from: int, msg_to: int, text: str) -> int:
        """追加一段日常记录，返回段号（seq）。"""
        with transaction(self.conn) as c:
            row = c.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM logs WHERE platform = ? AND conversation_id = ?",
                (platform, conversation_id),
            ).fetchone()
            seq = int(row["next_seq"])
            c.execute(
                """
                INSERT INTO logs (platform, conversation_id, seq, msg_from, msg_to, text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, conversation_id, seq, msg_from, msg_to, text, now_iso()),
            )
            return seq

    def list_logs(self, platform: str, conversation_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM logs WHERE platform = ? AND conversation_id = ? ORDER BY seq",
            (platform, conversation_id),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 更新任务（单事务批量写入） ----------

    def apply_update(
        self,
        platform: str,
        conversation_id: str,
        profiles: list[dict],
        log_text: str,
        msg_from: int,
        msg_to: int,
        seen_at: str,
    ) -> dict:
        """一次更新任务的全部写入：画像覆盖、绰号/条目追加、记录段追加、游标前移。

        单事务提交；失败整体回滚（游标不前进，可安全重跑）。
        返回 {"aliases_added", "entries_added", "log_seq"}。
        """
        aliases_added = 0
        entries_added = 0
        timestamp = now_iso()
        with transaction(self.conn) as c:
            for profile in profiles:
                user_id = str(profile.get("user_id", "")).strip()
                if not user_id:
                    continue
                basic = profile.get("basic")
                if isinstance(basic, dict):
                    c.execute(
                        """
                        INSERT INTO persons (platform, conversation_id, user_id, basic, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT (platform, conversation_id, user_id) DO UPDATE SET
                            basic = excluded.basic, updated_at = excluded.updated_at
                        """,
                        (platform, conversation_id, user_id, _basic_json(basic), timestamp),
                    )
                for alias in profile.get("aliases") or []:
                    alias = str(alias).strip()
                    if not alias:
                        continue
                    row = c.execute(
                        """
                        SELECT id FROM names
                        WHERE platform = ? AND conversation_id = ? AND user_id = ? AND kind = ? AND text = ?
                        """,
                        (platform, conversation_id, user_id, NAME_ALIAS, alias),
                    ).fetchone()
                    if row is not None:
                        c.execute("UPDATE names SET last_seen = ? WHERE id = ?", (seen_at, row["id"]))
                        continue
                    c.execute(
                        """
                        INSERT INTO names (platform, conversation_id, user_id, kind, text, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (platform, conversation_id, user_id, NAME_ALIAS, alias, seen_at, seen_at),
                    )
                    aliases_added += 1
                for text in profile.get("entries") or []:
                    text = str(text).strip()
                    if not text:
                        continue
                    row = c.execute(
                        """
                        SELECT id FROM entries
                        WHERE platform = ? AND conversation_id = ? AND user_id = ? AND text = ?
                        """,
                        (platform, conversation_id, user_id, text),
                    ).fetchone()
                    if row is not None:
                        continue
                    c.execute(
                        """
                        INSERT INTO entries (platform, conversation_id, user_id, text, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (platform, conversation_id, user_id, text, timestamp),
                    )
                    entries_added += 1
            seq_row = c.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM logs WHERE platform = ? AND conversation_id = ?",
                (platform, conversation_id),
            ).fetchone()
            log_seq = int(seq_row["next_seq"])
            c.execute(
                """
                INSERT INTO logs (platform, conversation_id, seq, msg_from, msg_to, text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, conversation_id, log_seq, msg_from, msg_to, log_text, timestamp),
            )
            c.execute(
                """
                UPDATE conversations SET cursor = MAX(cursor, ?), updated_at = ?
                WHERE platform = ? AND conversation_id = ?
                """,
                (msg_to, timestamp, platform, conversation_id),
            )
        return {"aliases_added": aliases_added, "entries_added": entries_added, "log_seq": log_seq}


def _basic_json(basic: dict) -> str:
    """基本画像 JSON（仅保留固定维度）。"""
    return json.dumps({k: str(basic.get(k, "")) for k in BASIC_FIELDS}, ensure_ascii=False)


def _record_nickname(
    conn: sqlite3.Connection,
    platform: str,
    conversation_id: str,
    user_id: str,
    text: str,
    seen_at: str,
) -> bool:
    """群昵称变更检测（需在事务内调用）：相同刷新 last_seen，否则追加新行。"""
    latest = conn.execute(
        """
        SELECT id, text FROM names
        WHERE platform = ? AND conversation_id = ? AND user_id = ? AND kind = ?
        ORDER BY id DESC LIMIT 1
        """,
        (platform, conversation_id, user_id, NAME_GROUP_NICKNAME),
    ).fetchone()
    if latest is not None and latest["text"] == text:
        conn.execute("UPDATE names SET last_seen = ? WHERE id = ?", (seen_at, latest["id"]))
        return False
    conn.execute(
        """
        INSERT INTO names (platform, conversation_id, user_id, kind, text, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (platform, conversation_id, user_id, NAME_GROUP_NICKNAME, text, seen_at, seen_at),
    )
    return True
