"""Storage contract and a standalone SQLite implementation."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Conversation:
    id: str
    participants: tuple[str, ...]
    parent_id: str | None = None
    parent_message_id: str | None = None


@dataclass(frozen=True)
class Message:
    id: str
    conversation_id: str
    sender: str
    role: str
    text: str
    in_reply_to: str | None = None


class ConversationStore(Protocol):
    def create(self, conversation: Conversation) -> None: ...

    def get(self, conversation_id: str) -> Conversation | None: ...

    def append(self, message: Message) -> None: ...

    def history(self, conversation_id: str) -> tuple[Message, ...]: ...


class SQLiteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS chat_conversations (
                id TEXT PRIMARY KEY,
                participants TEXT NOT NULL,
                parent_id TEXT REFERENCES chat_conversations(id),
                parent_message_id TEXT REFERENCES chat_messages(id)
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                sequence INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                conversation_id TEXT NOT NULL REFERENCES chat_conversations(id),
                sender TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                in_reply_to TEXT REFERENCES chat_messages(id)
            );
            CREATE INDEX IF NOT EXISTS chat_history
                ON chat_messages(conversation_id, sequence);
        """)

    def close(self) -> None:
        self._conn.close()

    def create(self, conversation: Conversation) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO chat_conversations VALUES (?, ?, ?, ?)",
                (conversation.id, json.dumps(conversation.participants),
                 conversation.parent_id, conversation.parent_message_id),
            )

    def get(self, conversation_id: str) -> Conversation | None:
        row = self._conn.execute(
            "SELECT * FROM chat_conversations WHERE id = ?", (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return Conversation(row["id"], tuple(json.loads(row["participants"])),
                            row["parent_id"], row["parent_message_id"])

    def append(self, message: Message) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO chat_messages
                   (id, conversation_id, sender, role, text, in_reply_to)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (message.id, message.conversation_id, message.sender,
                 message.role, message.text, message.in_reply_to),
            )

    def history(self, conversation_id: str) -> tuple[Message, ...]:
        rows = self._conn.execute(
            """SELECT id, conversation_id, sender, role, text, in_reply_to
               FROM chat_messages WHERE conversation_id = ? ORDER BY sequence""",
            (conversation_id,),
        )
        return tuple(Message(**dict(row)) for row in rows)
