"""Conversation history: SQLite-backed persistent conversation storage.

Stores complete conversation sessions for later retrieval and search.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import structlog

from src.config import get_kuro_home
from src.core.types import Message, Role, Session

logger = structlog.get_logger()

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    adapter TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    summary TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    name TEXT,
    tool_call_id TEXT,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
"""


class ConversationHistory:
    """Persistent conversation history stored in SQLite."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or str(get_kuro_home() / "history.db")
        self._initialized = False

    async def _ensure_db(self) -> None:
        """Initialize database tables if needed."""
        if self._initialized:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(CREATE_TABLES_SQL)
            await db.commit()
        self._initialized = True

    async def save_session(self, session: Session) -> None:
        """Save or update a session and its messages."""
        await self._ensure_db()

        now = datetime.now(timezone.utc).isoformat()
        # Filter out system messages for storage
        user_messages = [m for m in session.messages if m.role != Role.SYSTEM]

        async with aiosqlite.connect(self._db_path) as db:
            # Upsert session
            await db.execute(
                """INSERT INTO sessions (id, adapter, user_id, created_at, updated_at, message_count)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     updated_at = ?,
                     message_count = ?""",
                (session.id, session.adapter, session.user_id,
                 session.created_at.isoformat(), now, len(user_messages),
                 now, len(user_messages)),
            )

            # Delete existing messages for this session (full replace)
            await db.execute("DELETE FROM messages WHERE session_id = ?", (session.id,))

            # Insert all messages
            for msg in user_messages:
                await db.execute(
                    """INSERT INTO messages (session_id, role, content, name, tool_call_id, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (session.id, msg.role.value, msg.content or "",
                     msg.name, msg.tool_call_id,
                     msg.timestamp.isoformat()),
                )

            await db.commit()

    async def load_session(self, session_id: str) -> Session | None:
        """Load a session and its messages from the database."""
        await self._ensure_db()

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            # Load session metadata
            async with db.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                row_dict = dict(row)

            # Load messages
            async with db.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ) as cursor:
                msg_rows = await cursor.fetchall()

        session = Session(
            id=row_dict["id"],
            adapter=row_dict["adapter"],
            user_id=row_dict["user_id"],
            created_at=datetime.fromisoformat(row_dict["created_at"]),
        )

        for mr in msg_rows:
            mr_dict = dict(mr)
            msg = Message(
                role=Role(mr_dict["role"]),
                content=mr_dict["content"],
                name=mr_dict["name"],
                tool_call_id=mr_dict["tool_call_id"],
                timestamp=datetime.fromisoformat(mr_dict["timestamp"]),
            )
            session.messages.append(msg)

        return session

    async def list_sessions(
        self,
        limit: int = 20,
        adapter: str | None = None,
    ) -> list[dict[str, Any]]:
        """List recent sessions."""
        await self._ensure_db()

        query = "SELECT * FROM sessions"
        params: list[Any] = []

        if adapter:
            query += " WHERE adapter = ?"
            params.append(adapter)

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def search_messages(
        self,
        query_text: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Simple text search across conversation messages."""
        await self._ensure_db()

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT m.*, s.adapter, s.user_id
                   FROM messages m
                   JOIN sessions s ON m.session_id = s.id
                   WHERE m.content LIKE ?
                   ORDER BY m.id DESC
                   LIMIT ?""",
                (f"%{query_text}%", limit),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages."""
        await self._ensure_db()

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            result = await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await db.commit()
            return result.rowcount > 0
