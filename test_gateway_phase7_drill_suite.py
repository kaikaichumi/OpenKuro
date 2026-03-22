"""Phase 7 drill suite tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import KuroConfig
from src.core.security.gateway_drill import run_gateway_phase7_drill_suite


def _init_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
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
                hmac TEXT,
                prev_chain_hash TEXT,
                chain_hash TEXT,
                chain_version INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                session_id TEXT,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_audit(
    db_path: Path,
    *,
    timestamp: datetime,
    event_type: str,
    tool_name: str,
    parameters: dict,
    result_summary: str,
    approval_status: str = "approved",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO audit_log (
                timestamp, event_type, session_id, source, tool_name,
                parameters, result_summary, approval_status, risk_level,
                hmac, prev_chain_hash, chain_hash, chain_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp.isoformat(),
                event_type,
                "s1",
                "test",
                tool_name,
                json.dumps(parameters, ensure_ascii=False),
                result_summary,
                approval_status,
                "low",
                "h",
                "",
                "",
                1,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_tokens(db_path: Path, *, timestamp: datetime, total: int) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO token_usage (
                timestamp, session_id, model, prompt_tokens, completion_tokens, total_tokens
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp.isoformat(),
                "s1",
                "gemini/gemini-3-flash-preview",
                max(0, total // 2),
                max(0, total - (total // 2)),
                total,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _build_cfg() -> KuroConfig:
    cfg = KuroConfig()
    cfg.egress_policy.gateway_enabled = True
    cfg.egress_policy.gateway_mode = "enforce"
    cfg.egress_policy.gateway_proxy_url = "http://127.0.0.1:8080"
    cfg.egress_policy.gateway_rollout_percent = 100
    cfg.egress_policy.gateway_rollout_seed = "test-seed"
    return cfg


def test_gateway_phase7_drill_suite_passes_with_healthy_data(tmp_path: Path) -> None:
    db_path = tmp_path / "audit.db"
    _init_db(db_path)
    cfg = _build_cfg()
    now = datetime.now(timezone.utc)

    # Phase 1 shadow sample requirement + gateway route presence.
    for i in range(60):
        _insert_audit(
            db_path,
            timestamp=now - timedelta(hours=2, minutes=i),
            event_type="security:gateway_route",
            tool_name="web_browse",
            parameters={
                "tool": "web_browse",
                "route": "shadow",
                "reason": "shadow_mode_candidate",
                "host": "example.com",
                "target": "https://example.com/",
                "proxy": "http://127.0.0.1:8080",
            },
            result_summary="route=shadow",
        )
    _insert_audit(
        db_path,
        timestamp=now - timedelta(hours=1),
        event_type="security:gateway_route",
        tool_name="web_browse",
        parameters={
            "tool": "web_browse",
            "route": "gateway",
            "reason": "routed_via_gateway",
            "host": "example.com",
            "target": "https://example.com/",
            "proxy": "http://127.0.0.1:8080",
        },
        result_summary="route=gateway",
    )

    # Current window network tool calls (healthy).
    for i in range(25):
        _insert_audit(
            db_path,
            timestamp=now - timedelta(hours=3, minutes=i),
            event_type="tool_execution",
            tool_name="web_browse",
            parameters={"url": "https://example.com/page"},
            result_summary="ok (12ms)",
            approval_status="approved",
        )

    # Previous window network tool calls for latency/token baseline.
    for i in range(20):
        _insert_audit(
            db_path,
            timestamp=now - timedelta(days=10, minutes=i),
            event_type="tool_execution",
            tool_name="web_browse",
            parameters={"url": "https://example.com/page"},
            result_summary="ok (11ms)",
            approval_status="approved",
        )

    _insert_tokens(db_path, timestamp=now - timedelta(days=2), total=1100)
    _insert_tokens(db_path, timestamp=now - timedelta(days=10), total=1000)

    summary = run_gateway_phase7_drill_suite(
        db_path=db_path,
        days=7,
        require_enforce_mode=True,
        min_rollout_percent=100,
        min_peak_hour_calls=20,
        max_peak_hour_deny_rate=0.10,
        max_missing_proxy_route_events=0,
        max_invalid_route_events=0,
        max_direct_ratio_when_full_rollout=0.20,
        incident_deny_rate_threshold=0.10,
        config=cfg,
    )

    assert summary["status"] == "ok"
    assert summary["passed"] is True
    sections = {s["name"]: s for s in summary.get("sections", [])}
    assert sections["baseline"]["passed"] is True
    assert sections["regression"]["passed"] is True
    assert sections["load"]["passed"] is True
    assert sections["incident"]["passed"] is True


def test_gateway_phase7_drill_suite_returns_error_when_db_missing(tmp_path: Path) -> None:
    missing_db = tmp_path / "missing.db"
    summary = run_gateway_phase7_drill_suite(
        db_path=missing_db,
        days=7,
        config=_build_cfg(),
    )
    assert summary["status"] == "error"
    assert summary["passed"] is False
    assert summary["error"] == "audit_db_missing"

