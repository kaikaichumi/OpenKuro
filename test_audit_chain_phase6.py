"""Phase 6 tests: tamper-evident audit chain."""

from __future__ import annotations

import asyncio
import sqlite3

import aiosqlite

from src.core.security.audit import AuditLog


def test_audit_chain_detects_result_tampering(tmp_path) -> None:
    db_path = tmp_path / "audit_chain.db"
    audit = AuditLog(str(db_path))

    async def _run() -> None:
        await audit.log(
            event_type="tool_execution",
            session_id="s-1",
            source="discord",
            tool_name="web_browse",
            parameters={"url": "https://example.com"},
            result_summary="ok",
            approval_status="approved",
            risk_level="medium",
        )
        await audit.log(
            event_type="tool_execution",
            session_id="s-1",
            source="discord",
            tool_name="web_browse",
            parameters={"url": "https://example.org"},
            result_summary="ok2",
            approval_status="approved",
            risk_level="medium",
        )
        total, tampered = await audit.verify_integrity(100)
        assert total >= 2
        assert tampered == 0

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "UPDATE audit_log SET result_summary = ? WHERE id = ?",
                ("tampered-summary", 1),
            )
            await db.commit()

        total2, tampered2 = await audit.verify_integrity(100)
        assert total2 >= 2
        assert tampered2 >= 1

    asyncio.run(_run())


def test_audit_chain_columns_auto_migrate_for_legacy_db(tmp_path) -> None:
    db_path = tmp_path / "legacy_audit.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE audit_log (
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
        )
        conn.commit()

    audit = AuditLog(str(db_path))

    async def _run() -> None:
        await audit.log(
            event_type="security:test",
            session_id="s-2",
            source="web",
            tool_name="shell_execute",
            parameters={"command": "echo test"},
            result_summary="ok",
            approval_status="approved",
            risk_level="high",
        )

        async with aiosqlite.connect(str(db_path)) as db:
            async with db.execute("PRAGMA table_info(audit_log)") as cursor:
                rows = await cursor.fetchall()
        columns = {str(row[1]) for row in rows}
        assert "prev_chain_hash" in columns
        assert "chain_hash" in columns
        assert "chain_version" in columns

    asyncio.run(_run())
