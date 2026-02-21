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

    async def get_daily_stats(self, date: str | None = None) -> dict[str, Any]:
        """Get aggregated statistics for a given date (default: today).

        Returns dict with keys:
            total_events, tool_calls, approved, denied, blocked,
            risk_distribution, top_tools, security_events
        """
        await self._ensure_db()

        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        stats: dict[str, Any] = {
            "date": date,
            "total_events": 0,
            "tool_calls": 0,
            "approved": 0,
            "denied": 0,
            "risk_distribution": {"low": 0, "medium": 0, "high": 0, "critical": 0},
            "top_tools": [],
            "security_events": 0,
            "hourly_activity": [0] * 24,
        }

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            # Total events for the date
            async with db.execute(
                "SELECT COUNT(*) as cnt FROM audit_log WHERE timestamp LIKE ?",
                (f"{date}%",),
            ) as cursor:
                row = await cursor.fetchone()
                stats["total_events"] = row["cnt"] if row else 0

            # Tool calls breakdown
            async with db.execute(
                """SELECT approval_status, COUNT(*) as cnt FROM audit_log
                   WHERE timestamp LIKE ? AND event_type = 'tool_execution'
                   GROUP BY approval_status""",
                (f"{date}%",),
            ) as cursor:
                async for row in cursor:
                    status = row["approval_status"]
                    count = row["cnt"]
                    stats["tool_calls"] += count
                    if status == "approved":
                        stats["approved"] = count
                    elif status == "denied":
                        stats["denied"] = count

            # Risk distribution
            async with db.execute(
                """SELECT risk_level, COUNT(*) as cnt FROM audit_log
                   WHERE timestamp LIKE ? AND risk_level != ''
                   GROUP BY risk_level""",
                (f"{date}%",),
            ) as cursor:
                async for row in cursor:
                    level = row["risk_level"].lower()
                    if level in stats["risk_distribution"]:
                        stats["risk_distribution"][level] = row["cnt"]

            # Top tools
            async with db.execute(
                """SELECT tool_name, COUNT(*) as cnt FROM audit_log
                   WHERE timestamp LIKE ? AND tool_name != ''
                   GROUP BY tool_name ORDER BY cnt DESC LIMIT 10""",
                (f"{date}%",),
            ) as cursor:
                stats["top_tools"] = [
                    {"tool": row["tool_name"], "count": row["cnt"]}
                    async for row in cursor
                ]

            # Security events
            async with db.execute(
                """SELECT COUNT(*) as cnt FROM audit_log
                   WHERE timestamp LIKE ? AND event_type LIKE 'security:%'""",
                (f"{date}%",),
            ) as cursor:
                row = await cursor.fetchone()
                stats["security_events"] = row["cnt"] if row else 0

            # Hourly activity
            async with db.execute(
                """SELECT timestamp FROM audit_log WHERE timestamp LIKE ?""",
                (f"{date}%",),
            ) as cursor:
                async for row in cursor:
                    try:
                        ts = row["timestamp"]
                        hour = int(ts[11:13])  # ISO format: YYYY-MM-DDTHH:...
                        if 0 <= hour < 24:
                            stats["hourly_activity"][hour] += 1
                    except (ValueError, IndexError):
                        pass

        return stats

    async def get_blocked_count(self, days: int = 7) -> dict[str, Any]:
        """Get count of blocked/denied operations over the last N days.

        Returns dict with daily_counts, total_blocked, total_approved.
        """
        await self._ensure_db()

        result: dict[str, Any] = {
            "days": days,
            "daily_counts": [],
            "total_blocked": 0,
            "total_approved": 0,
        }

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            # Get daily approved/denied counts
            async with db.execute(
                """SELECT
                       SUBSTR(timestamp, 1, 10) as day,
                       approval_status,
                       COUNT(*) as cnt
                   FROM audit_log
                   WHERE event_type = 'tool_execution'
                   GROUP BY day, approval_status
                   ORDER BY day DESC
                   LIMIT ?""",
                (days * 2,),  # at most 2 statuses per day
            ) as cursor:
                day_data: dict[str, dict[str, int]] = {}
                async for row in cursor:
                    day = row["day"]
                    if day not in day_data:
                        day_data[day] = {"approved": 0, "denied": 0}
                    status = row["approval_status"]
                    if status in ("approved", "denied"):
                        day_data[day][status] = row["cnt"]

                for day, counts in sorted(day_data.items()):
                    result["daily_counts"].append({
                        "date": day,
                        "approved": counts["approved"],
                        "denied": counts["denied"],
                    })
                    result["total_blocked"] += counts["denied"]
                    result["total_approved"] += counts["approved"]

        return result

    async def get_security_score(self) -> dict[str, Any]:
        """Calculate a security posture score based on configuration and activity.

        Returns dict with score (0-100), factors, recommendations.
        """
        await self._ensure_db()

        score = 100
        factors: list[dict[str, Any]] = []
        recommendations: list[str] = []

        # Factor 1: Check integrity of recent entries
        total, tampered = await self.verify_integrity(50)
        if tampered > 0:
            score -= 30
            factors.append({"name": "integrity", "status": "warning",
                            "detail": f"{tampered}/{total} entries have invalid HMAC"})
            recommendations.append("Audit log integrity compromised - investigate immediately")
        else:
            factors.append({"name": "integrity", "status": "ok",
                            "detail": f"All {total} recent entries verified"})

        # Factor 2: Check denied operations ratio (high denied = suspicious activity)
        blocked = await self.get_blocked_count(7)
        total_ops = blocked["total_approved"] + blocked["total_blocked"]
        if total_ops > 0:
            deny_ratio = blocked["total_blocked"] / total_ops
            if deny_ratio > 0.3:
                score -= 10
                factors.append({"name": "deny_ratio", "status": "warning",
                                "detail": f"{deny_ratio:.0%} operations denied in last 7 days"})
                recommendations.append("High denial rate - review blocked operations")
            else:
                factors.append({"name": "deny_ratio", "status": "ok",
                                "detail": f"{deny_ratio:.0%} operations denied"})

        # Factor 3: Check for high-risk operations
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stats = await self.get_daily_stats(today)
        high_risk = stats["risk_distribution"].get("high", 0) + stats["risk_distribution"].get("critical", 0)
        if high_risk > 10:
            score -= 10
            factors.append({"name": "high_risk_ops", "status": "warning",
                            "detail": f"{high_risk} high/critical operations today"})
        else:
            factors.append({"name": "high_risk_ops", "status": "ok",
                            "detail": f"{high_risk} high/critical operations today"})

        return {
            "score": max(0, min(100, score)),
            "grade": "A" if score >= 90 else "B" if score >= 70 else "C" if score >= 50 else "D",
            "factors": factors,
            "recommendations": recommendations,
        }

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
