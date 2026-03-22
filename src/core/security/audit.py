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
from datetime import datetime, timedelta, timezone
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
    hmac TEXT NOT NULL,
    prev_chain_hash TEXT NOT NULL DEFAULT '',
    chain_hash TEXT NOT NULL DEFAULT '',
    chain_version INTEGER NOT NULL DEFAULT 1
)
"""

CREATE_TOKEN_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool_name);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_event_time ON audit_log(event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_chain_hash ON audit_log(chain_hash);
CREATE INDEX IF NOT EXISTS idx_token_timestamp ON token_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_token_model ON token_usage(model);
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
            await db.execute(CREATE_TOKEN_TABLE_SQL)
            await self._ensure_chain_columns(db)
            await db.executescript(CREATE_INDEX_SQL)
            await db.commit()
        self._initialized = True

    @staticmethod
    async def _ensure_chain_columns(db: aiosqlite.Connection) -> None:
        """Backfill chain columns for databases created before Phase 6."""
        cols: set[str] = set()
        async with db.execute("PRAGMA table_info(audit_log)") as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            try:
                cols.add(str(row[1]))
            except Exception:
                continue

        if "prev_chain_hash" not in cols:
            await db.execute(
                "ALTER TABLE audit_log ADD COLUMN prev_chain_hash TEXT NOT NULL DEFAULT ''"
            )
        if "chain_hash" not in cols:
            await db.execute(
                "ALTER TABLE audit_log ADD COLUMN chain_hash TEXT NOT NULL DEFAULT ''"
            )
        if "chain_version" not in cols:
            await db.execute(
                "ALTER TABLE audit_log ADD COLUMN chain_version INTEGER NOT NULL DEFAULT 1"
            )

    @staticmethod
    def _build_hmac_payload(
        *,
        timestamp: str,
        event_type: str,
        session_id: str,
        tool_name: str,
        params_str: str,
        approval_status: str,
    ) -> str:
        return (
            f"{timestamp}|{event_type}|{session_id}|{tool_name}|"
            f"{params_str}|{approval_status}"
        )

    @staticmethod
    def _compute_chain_hash(
        *,
        prev_chain_hash: str,
        timestamp: str,
        event_type: str,
        session_id: str,
        source: str,
        tool_name: str,
        params_str: str,
        result_summary: str,
        approval_status: str,
        risk_level: str,
        entry_hmac: str,
        chain_version: int = 1,
    ) -> str:
        payload = (
            f"v{int(chain_version)}|{prev_chain_hash}|{timestamp}|{event_type}|"
            f"{session_id}|{source}|{tool_name}|{params_str}|{result_summary}|"
            f"{approval_status}|{risk_level}|{entry_hmac}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

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
        hmac_data = self._build_hmac_payload(
            timestamp=ts,
            event_type=event_type,
            session_id=session_id,
            tool_name=tool_name,
            params_str=params_str,
            approval_status=approval_status,
        )
        entry_hmac = compute_hmac(hmac_data)

        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_chain_columns(db)
            prev_chain_hash = ""
            async with db.execute(
                "SELECT chain_hash FROM audit_log ORDER BY id DESC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                if row and len(row) > 0:
                    prev_chain_hash = str(row[0] or "")
            chain_hash = self._compute_chain_hash(
                prev_chain_hash=prev_chain_hash,
                timestamp=ts,
                event_type=event_type,
                session_id=session_id,
                source=source,
                tool_name=tool_name,
                params_str=params_str,
                result_summary=result_safe,
                approval_status=approval_status,
                risk_level=risk_level,
                entry_hmac=entry_hmac,
                chain_version=1,
            )
            await db.execute(
                """INSERT INTO audit_log
                   (timestamp, event_type, session_id, source, tool_name,
                    parameters, result_summary, approval_status, risk_level, hmac,
                    prev_chain_hash, chain_hash, chain_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, event_type, session_id, source, tool_name,
                 params_str, result_safe, approval_status, risk_level, entry_hmac,
                 prev_chain_hash, chain_hash, 1),
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

    async def log_token_usage(
        self,
        session_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        """Log token usage from an LLM call."""
        await self._ensure_db()
        ts = datetime.now(timezone.utc).isoformat()

        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """INSERT INTO token_usage
                       (timestamp, session_id, model, prompt_tokens, completion_tokens, total_tokens)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (ts, session_id, model, prompt_tokens, completion_tokens, total_tokens),
                )
                await db.commit()
        except Exception as e:
            logger.debug("token_log_failed", error=str(e))

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

    async def query_gateway_logs(self, limit: int = 120) -> list[dict[str, Any]]:
        """Query persisted Lite Gateway routing logs (newest first)."""
        await self._ensure_db()
        max_rows = max(1, int(limit or 120))

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT timestamp, tool_name, parameters, result_summary
                   FROM audit_log
                   WHERE event_type = 'security:gateway_route'
                   ORDER BY id DESC
                   LIMIT ?""",
                (max_rows,),
            ) as cursor:
                rows = await cursor.fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            params_raw = row["parameters"] if isinstance(row, dict) else row["parameters"]
            params: dict[str, Any]
            try:
                params = json.loads(params_raw or "{}")
                if not isinstance(params, dict):
                    params = {}
            except Exception:
                params = {}

            out.append(
                {
                    "timestamp": row["timestamp"],
                    "tool_name": params.get("tool") or row["tool_name"] or "",
                    "target": params.get("target", ""),
                    "host": params.get("host", ""),
                    "route": params.get("route", ""),
                    "reason": params.get("reason", ""),
                    "proxy": params.get("proxy", ""),
                    "summary": row["result_summary"] or "",
                }
            )
        return out

    async def query_capability_token_denials(
        self,
        days: int = 7,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Summarize capability-token denial events for security dashboard."""
        await self._ensure_db()

        window_days = max(1, int(days or 7))
        max_rows = max(1, min(5000, int(limit or 200)))
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=window_days)).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT timestamp, session_id, source, tool_name, parameters, result_summary
                   FROM audit_log
                   WHERE event_type = 'security:capability_token_denied'
                     AND timestamp >= ?
                   ORDER BY id DESC
                   LIMIT ?""",
                (since, max_rows),
            ) as cursor:
                rows = await cursor.fetchall()

        reason_map: dict[str, dict[str, Any]] = {}
        tool_counts: dict[str, int] = {}
        daily_counts: dict[str, int] = {}
        recent: list[dict[str, Any]] = []

        for row in rows:
            timestamp = str(row["timestamp"] or "")
            tool_name = str(row["tool_name"] or "").strip() or "-"
            summary = str(row["result_summary"] or "").strip()
            session_id = str(row["session_id"] or "").strip()
            source = str(row["source"] or "").strip()

            params: dict[str, Any] = {}
            try:
                parsed = json.loads(row["parameters"] or "{}")
                if isinstance(parsed, dict):
                    params = parsed
            except Exception:
                params = {}

            reason = str(params.get("reason") or "").strip()
            if not reason:
                marker = "Capability token invalid:"
                if summary.lower().startswith(marker.lower()):
                    reason = summary[len(marker):].strip()
                else:
                    reason = summary or "unknown"
            reason_key = reason.lower()
            bucket = reason_map.setdefault(
                reason_key,
                {"reason": reason, "count": 0},
            )
            bucket["count"] = int(bucket.get("count", 0) or 0) + 1

            tool_counts[tool_name] = int(tool_counts.get(tool_name, 0) or 0) + 1

            day = timestamp[:10] if len(timestamp) >= 10 else ""
            if day:
                daily_counts[day] = int(daily_counts.get(day, 0) or 0) + 1

            if len(recent) < 30:
                recent.append(
                    {
                        "timestamp": timestamp,
                        "tool_name": tool_name,
                        "reason": reason,
                        "session_id": session_id,
                        "source": source,
                    }
                )

        trend: list[dict[str, Any]] = []
        for offset in range(window_days - 1, -1, -1):
            day = (now - timedelta(days=offset)).date().isoformat()
            trend.append({"date": day, "count": int(daily_counts.get(day, 0) or 0)})

        top_reasons = sorted(
            reason_map.values(),
            key=lambda x: (-int(x.get("count", 0) or 0), str(x.get("reason", "")).lower()),
        )[:10]
        top_tools = [
            {"tool": name, "count": count}
            for name, count in sorted(
                tool_counts.items(),
                key=lambda x: (-int(x[1]), str(x[0]).lower()),
            )[:10]
        ]

        return {
            "days": window_days,
            "total_denied": len(rows),
            "unique_reasons": len(reason_map),
            "unique_tools": len(tool_counts),
            "top_reasons": top_reasons,
            "top_tools": top_tools,
            "daily_counts": trend,
            "recent": recent,
        }

    async def query_data_firewall_events(
        self,
        days: int = 7,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Summarize Data Firewall sanitization events for security dashboard."""
        await self._ensure_db()

        window_days = max(1, int(days or 7))
        max_rows = max(1, min(5000, int(limit or 500)))
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=window_days)).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT timestamp, tool_name, parameters
                   FROM audit_log
                   WHERE event_type = 'security:data_firewall_sanitized'
                     AND timestamp >= ?
                   ORDER BY id DESC
                   LIMIT ?""",
                (since, max_rows),
            ) as cursor:
                rows = await cursor.fetchall()

        by_tool: dict[str, int] = {}
        daily: dict[str, int] = {}
        injection_lines = 0
        command_lines = 0
        base64_chunks = 0
        truncated_count = 0
        recent: list[dict[str, Any]] = []

        for row in rows:
            tool_name = str(row["tool_name"] or "").strip() or "-"
            ts = str(row["timestamp"] or "")
            by_tool[tool_name] = int(by_tool.get(tool_name, 0) or 0) + 1
            day = ts[:10] if len(ts) >= 10 else ""
            if day:
                daily[day] = int(daily.get(day, 0) or 0) + 1

            params: dict[str, Any] = {}
            try:
                parsed = json.loads(row["parameters"] or "{}")
                if isinstance(parsed, dict):
                    params = parsed
            except Exception:
                params = {}

            injection_lines += int(params.get("prompt_injection_lines", 0) or 0)
            command_lines += int(params.get("command_like_lines_removed", 0) or 0)
            base64_chunks += int(params.get("base64_chunks_removed", 0) or 0)
            if bool(params.get("truncated", False)):
                truncated_count += 1

            if len(recent) < 30:
                recent.append(
                    {
                        "timestamp": ts,
                        "tool_name": tool_name,
                        "prompt_injection_lines": int(params.get("prompt_injection_lines", 0) or 0),
                        "command_like_lines_removed": int(params.get("command_like_lines_removed", 0) or 0),
                        "base64_chunks_removed": int(params.get("base64_chunks_removed", 0) or 0),
                        "truncated": bool(params.get("truncated", False)),
                    }
                )

        daily_counts: list[dict[str, Any]] = []
        for offset in range(window_days - 1, -1, -1):
            day = (now - timedelta(days=offset)).date().isoformat()
            daily_counts.append({"date": day, "count": int(daily.get(day, 0) or 0)})

        top_tools = [
            {"tool": name, "count": count}
            for name, count in sorted(
                by_tool.items(),
                key=lambda x: (-int(x[1]), str(x[0]).lower()),
            )[:10]
        ]

        return {
            "days": window_days,
            "total_events": len(rows),
            "top_tools": top_tools,
            "daily_counts": daily_counts,
            "removed_prompt_injection_lines": injection_lines,
            "removed_command_like_lines": command_lines,
            "removed_base64_chunks": base64_chunks,
            "truncated_events": truncated_count,
            "recent": recent,
        }

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
            await self._ensure_chain_columns(db)
            async with db.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ) as cursor:
                latest_rows = await cursor.fetchall()

            if not latest_rows:
                return 0, 0

            rows = list(reversed(latest_rows))
            first_id = int(rows[0]["id"])
            baseline_prev_chain = ""
            async with db.execute(
                "SELECT chain_hash FROM audit_log WHERE id < ? ORDER BY id DESC LIMIT 1",
                (first_id,),
            ) as cursor:
                prev = await cursor.fetchone()
                if prev and len(prev) > 0:
                    baseline_prev_chain = str(prev[0] or "")

        total = 0
        tampered = 0
        running_prev_chain = baseline_prev_chain

        for row in rows:
            total += 1
            row_dict = dict(row)
            row_tampered = False
            hmac_data = self._build_hmac_payload(
                timestamp=str(row_dict.get("timestamp", "")),
                event_type=str(row_dict.get("event_type", "")),
                session_id=str(row_dict.get("session_id", "")),
                tool_name=str(row_dict.get("tool_name", "")),
                params_str=str(row_dict.get("parameters", "")),
                approval_status=str(row_dict.get("approval_status", "")),
            )
            expected = compute_hmac(hmac_data)
            if expected != row_dict["hmac"]:
                row_tampered = True
                logger.warning(
                    "audit_tamper_detected",
                    entry_id=row_dict["id"],
                    reason="hmac_mismatch",
                )

            row_prev_chain = str(row_dict.get("prev_chain_hash", "") or "")
            if row_prev_chain != running_prev_chain:
                row_tampered = True
                logger.warning(
                    "audit_tamper_detected",
                    entry_id=row_dict["id"],
                    reason="prev_chain_mismatch",
                )

            expected_chain = self._compute_chain_hash(
                prev_chain_hash=row_prev_chain,
                timestamp=str(row_dict.get("timestamp", "")),
                event_type=str(row_dict.get("event_type", "")),
                session_id=str(row_dict.get("session_id", "")),
                source=str(row_dict.get("source", "")),
                tool_name=str(row_dict.get("tool_name", "")),
                params_str=str(row_dict.get("parameters", "")),
                result_summary=str(row_dict.get("result_summary", "")),
                approval_status=str(row_dict.get("approval_status", "")),
                risk_level=str(row_dict.get("risk_level", "")),
                entry_hmac=str(row_dict.get("hmac", "")),
                chain_version=int(row_dict.get("chain_version", 1) or 1),
            )
            row_chain = str(row_dict.get("chain_hash", "") or "")
            if expected_chain != row_chain:
                row_tampered = True
                logger.warning(
                    "audit_tamper_detected",
                    entry_id=row_dict["id"],
                    reason="chain_hash_mismatch",
                )

            if row_tampered:
                tampered += 1
            running_prev_chain = row_chain or expected_chain

        return total, tampered
