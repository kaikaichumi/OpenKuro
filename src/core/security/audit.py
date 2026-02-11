"""Audit log: immutable, append-only security audit trail.

Records all security-relevant events with HMAC integrity verification
and automatic sensitive data redaction.

Stored in SQLite for efficient querying.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import structlog

from src.config import get_kuro_home

logger = structlog.get_logger()

# HMAC key derived from machine-specific data
# Not cryptographically secure against determined attacker,
# but detects casual log tampering
_HMAC_KEY: bytes | None = None


def _get_hmac_key() -> bytes:
    """Get or derive HMAC key for audit log integrity."""
    global _HMAC_KEY
    if _HMAC_KEY is None:
        # Derive from username + hostname as a simple machine fingerprint
        seed = f"{os.getlogin()}@{os.uname().nodename if hasattr(os, 'uname') else 'windows'}"
        _HMAC_KEY = hashlib.sha256(seed.encode()).digest()
    return _HMAC_KEY


def compute_hmac(data: str) -> str:
    """Compute HMAC-SHA256 for a data string."""
    return hmac.new(_get_hmac_key(), data.encode(), hashlib.sha256).hexdigest()[:16]


# Patterns to redact
REDACT_PATTERNS = [
    "api_key", "api-key", "apikey", "password", "passwd",
    "secret", "token", "credential", "auth_token",
    "access_key", "private_key",
]


def redact_sensitive(data: Any) -> Any:
    """Recursively redact sensitive values from data structures."""
    if isinstance(data, dict):
        return {
            k: "***" if any(p in k.lower() for p in REDACT_PATTERNS) else redact_sensitive(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact_sensitive(item) for item in data]
    if isinstance(data, str) and len(data) > 20:
        # Check if the string looks like an API key (long alphanumeric)
        if data.startswith(("sk-", "pk-", "api-", "token-")):
            return data[:6] + "***"
    return data


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    session_id TEXT,
    source TEXT,
    tool_name TEXT,
    parameters TEXT,
    result_summary TEXT,
    approval_status TEXT,
    risk_level TEXT,
    hmac TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool_name);
"""


class AuditLog:
    """Immutable, append-only audit trail with HMAC integrity."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or str(get_kuro_home() / "audit.db")
        self._initialized = False

    async def _ensure_db(self) -> None:
        """Create the database and tables if needed."""
        if self._initialized:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(CREATE_TABLE_SQL)
            await db.executescript(CREATE_INDEX_SQL)
            await db.commit()
        self._initialized = True

    async def log(
        self,
        event_type: str,
        session_id: str = "",
        source: str = "",
        tool_name: str = "",
        parameters: dict[str, Any] | None = None,
        result_summary: str = "",
        approval_status: str = "",
        risk_level: str = "",
    ) -> None:
        """Append an audit event to the log."""
        await self._ensure_db()

        ts = datetime.now(timezone.utc).isoformat()
        params_str = json.dumps(
            redact_sensitive(parameters or {}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result_safe = result_summary[:500]  # Truncate

        # Build HMAC over the entry data
        hmac_data = f"{ts}|{event_type}|{session_id}|{tool_name}|{params_str}|{approval_status}"
        entry_hmac = compute_hmac(hmac_data)

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO audit_log
                   (timestamp, event_type, session_id, source, tool_name,
                    parameters, result_summary, approval_status, risk_level, hmac)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, event_type, session_id, source, tool_name,
                 params_str, result_safe, approval_status, risk_level, entry_hmac),
            )
            await db.commit()

    async def log_tool_execution(
        self,
        session_id: str,
        source: str,
        tool_name: str,
        parameters: dict[str, Any],
        approved: bool,
        risk_level: str,
        result_summary: str = "",
    ) -> None:
        """Convenience: log a tool execution event."""
        await self.log(
            event_type="tool_execution",
            session_id=session_id,
            source=source,
            tool_name=tool_name,
            parameters=parameters,
            approval_status="approved" if approved else "denied",
            risk_level=risk_level,
            result_summary=result_summary,
        )

    async def log_security_event(
        self,
        event_type: str,
        session_id: str = "",
        details: str = "",
    ) -> None:
        """Convenience: log a security-related event."""
        await self.log(
            event_type=f"security:{event_type}",
            session_id=session_id,
            result_summary=details,
        )

    async def query_recent(
        self,
        limit: int = 50,
        session_id: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query recent audit entries."""
        await self._ensure_db()

        query = "SELECT * FROM audit_log"
        conditions = []
        params: list[Any] = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def verify_integrity(self, limit: int = 100) -> tuple[int, int]:
        """Verify HMAC integrity of recent entries.

        Returns (total_checked, tampered_count).
        """
        await self._ensure_db()

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ) as cursor:
                rows = await cursor.fetchall()

        total = 0
        tampered = 0

        for row in rows:
            total += 1
            row_dict = dict(row)
            hmac_data = (
                f"{row_dict['timestamp']}|{row_dict['event_type']}|"
                f"{row_dict['session_id']}|{row_dict['tool_name']}|"
                f"{row_dict['parameters']}|{row_dict['approval_status']}"
            )
            expected = compute_hmac(hmac_data)
            if expected != row_dict["hmac"]:
                tampered += 1
                logger.warning(
                    "audit_tamper_detected",
                    entry_id=row_dict["id"],
                )

        return total, tampered
