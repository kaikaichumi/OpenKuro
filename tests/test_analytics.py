"""Tests for the analytics and security dashboard features."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.security.audit import AuditLog


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary audit database."""
    return str(tmp_path / "test_audit.db")


@pytest.fixture
def temp_log_dir(tmp_path):
    """Create a temporary action log directory with sample data."""
    log_dir = tmp_path / "action_logs"
    log_dir.mkdir()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = log_dir / f"actions-{today}.jsonl"

    entries = [
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sid": "test-session-1",
            "type": "tool_call",
            "tool": "file_read",
            "params": {"path": "/test"},
            "status": "ok",
            "duration_ms": 50,
        },
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sid": "test-session-1",
            "type": "tool_call",
            "tool": "file_read",
            "params": {"path": "/test2"},
            "status": "ok",
            "duration_ms": 30,
        },
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sid": "test-session-2",
            "type": "tool_call",
            "tool": "shell_execute",
            "params": {"command": "ls"},
            "status": "ok",
            "duration_ms": 200,
        },
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sid": "test-session-2",
            "type": "tool_call",
            "tool": "shell_execute",
            "params": {"command": "pwd"},
            "status": "error",
            "error": "Permission denied",
            "duration_ms": 10,
        },
    ]

    with open(log_file, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    return log_dir


# === AuditLog Dashboard Tests ===


class TestAuditDashboard:
    """Test new audit dashboard methods."""

    @pytest.mark.asyncio
    async def test_get_daily_stats_empty(self, temp_db):
        """Test daily stats with no data."""
        audit = AuditLog(db_path=temp_db)
        stats = await audit.get_daily_stats()

        assert stats["total_events"] == 0
        assert stats["tool_calls"] == 0
        assert stats["approved"] == 0
        assert stats["denied"] == 0
        assert stats["security_events"] == 0
        assert len(stats["hourly_activity"]) == 24

    @pytest.mark.asyncio
    async def test_get_daily_stats_with_data(self, temp_db):
        """Test daily stats after logging some events."""
        audit = AuditLog(db_path=temp_db)

        # Log some tool executions
        await audit.log_tool_execution(
            session_id="s1",
            source="anthropic/claude-sonnet-4.5",
            tool_name="file_read",
            parameters={"path": "/test"},
            approved=True,
            risk_level="low",
        )
        await audit.log_tool_execution(
            session_id="s1",
            source="anthropic/claude-sonnet-4.5",
            tool_name="shell_execute",
            parameters={"command": "ls"},
            approved=False,
            risk_level="high",
        )
        await audit.log_security_event("sandbox_violation", "s1", "Blocked path")

        stats = await audit.get_daily_stats()

        assert stats["total_events"] == 3
        assert stats["tool_calls"] == 2
        assert stats["approved"] == 1
        assert stats["denied"] == 1
        assert stats["security_events"] == 1

    @pytest.mark.asyncio
    async def test_get_blocked_count(self, temp_db):
        """Test blocked operations count."""
        audit = AuditLog(db_path=temp_db)

        for _ in range(3):
            await audit.log_tool_execution(
                session_id="s1", source="test", tool_name="file_read",
                parameters={}, approved=True, risk_level="low",
            )
        for _ in range(2):
            await audit.log_tool_execution(
                session_id="s1", source="test", tool_name="shell_execute",
                parameters={}, approved=False, risk_level="high",
            )

        blocked = await audit.get_blocked_count(7)

        assert blocked["total_approved"] == 3
        assert blocked["total_blocked"] == 2
        assert len(blocked["daily_counts"]) > 0

    @pytest.mark.asyncio
    async def test_get_security_score(self, temp_db):
        """Test security score calculation."""
        audit = AuditLog(db_path=temp_db)

        # Log some normal operations
        await audit.log_tool_execution(
            session_id="s1", source="test", tool_name="file_read",
            parameters={}, approved=True, risk_level="low",
        )

        score = await audit.get_security_score()

        assert "score" in score
        assert "grade" in score
        assert "factors" in score
        assert 0 <= score["score"] <= 100
        assert score["grade"] in ("A", "B", "C", "D")

    @pytest.mark.asyncio
    async def test_risk_distribution(self, temp_db):
        """Test risk level distribution in daily stats."""
        audit = AuditLog(db_path=temp_db)

        # Log events with different risk levels
        for level in ["low", "low", "low", "medium", "high"]:
            await audit.log_tool_execution(
                session_id="s1", source="test", tool_name=f"tool_{level}",
                parameters={}, approved=True, risk_level=level,
            )

        stats = await audit.get_daily_stats()

        assert stats["risk_distribution"]["low"] == 3
        assert stats["risk_distribution"]["medium"] == 1
        assert stats["risk_distribution"]["high"] == 1
        assert stats["risk_distribution"]["critical"] == 0


# === UsageAnalyzer Tests ===


class TestUsageAnalyzer:
    """Test usage analytics from action logs."""

    @pytest.mark.asyncio
    async def test_usage_summary(self, temp_log_dir):
        """Test usage summary from action logs."""
        from src.core.analytics import UsageAnalyzer

        analyzer = UsageAnalyzer(log_dir=temp_log_dir)
        summary = await analyzer.get_usage_summary(30)

        assert summary["total_calls"] == 4
        assert summary["unique_sessions"] == 2
        assert summary["error_count"] == 1
        assert summary["error_rate"] == 25.0

        assert "file_read" in summary["tool_counts"]
        assert summary["tool_counts"]["file_read"] == 2
        assert summary["tool_counts"]["shell_execute"] == 2

    @pytest.mark.asyncio
    async def test_usage_summary_empty(self, tmp_path):
        """Test usage summary with empty log directory."""
        from src.core.analytics import UsageAnalyzer

        log_dir = tmp_path / "empty_logs"
        log_dir.mkdir()

        analyzer = UsageAnalyzer(log_dir=log_dir)
        summary = await analyzer.get_usage_summary(30)

        assert summary["total_calls"] == 0
        assert summary["unique_sessions"] == 0
        assert summary["error_rate"] == 0

    @pytest.mark.asyncio
    async def test_avg_durations(self, temp_log_dir):
        """Test average duration calculation."""
        from src.core.analytics import UsageAnalyzer

        analyzer = UsageAnalyzer(log_dir=temp_log_dir)
        summary = await analyzer.get_usage_summary(30)

        assert "file_read" in summary["avg_duration_ms"]
        assert summary["avg_duration_ms"]["file_read"] == 40  # (50+30)/2


# === CostEstimator Tests ===


class TestCostEstimator:
    """Test cost estimation."""

    @pytest.mark.asyncio
    async def test_cost_estimation(self, temp_db):
        """Test cost estimation with token usage data."""
        from src.core.analytics import CostEstimator

        # Populate token_usage with real token data
        audit = AuditLog(db_path=temp_db)
        for _ in range(10):
            await audit.log_token_usage(
                session_id="s1",
                model="anthropic/claude-sonnet-4.5",
                prompt_tokens=500,
                completion_tokens=200,
                total_tokens=700,
            )

        estimator = CostEstimator(db_path=temp_db)
        costs = await estimator.estimate_costs(30)

        assert costs["total_estimated_cost_usd"] > 0
        assert costs["total_tokens"] == 7000
        assert "anthropic/claude-sonnet-4.5" in costs["by_model"]
        model_info = costs["by_model"]["anthropic/claude-sonnet-4.5"]
        assert model_info["calls"] == 10
        assert model_info["prompt_tokens"] == 5000
        assert model_info["completion_tokens"] == 2000
        assert model_info["has_pricing"] is True
        assert model_info["pricing"] is not None

    @pytest.mark.asyncio
    async def test_cost_estimation_unknown_model(self, temp_db):
        """Test cost estimation for a model not in pricing table."""
        from src.core.analytics import CostEstimator

        audit = AuditLog(db_path=temp_db)
        await audit.log_token_usage(
            session_id="s1",
            model="custom/unknown-model",
            prompt_tokens=1000,
            completion_tokens=500,
            total_tokens=1500,
        )

        estimator = CostEstimator(db_path=temp_db)
        costs = await estimator.estimate_costs(30)

        assert costs["total_tokens"] == 1500
        model_info = costs["by_model"]["custom/unknown-model"]
        assert model_info["has_pricing"] is False
        assert model_info["estimated_cost_usd"] is None
        assert model_info["pricing"] is None
        assert model_info["total_tokens"] == 1500

    @pytest.mark.asyncio
    async def test_cost_estimation_empty(self, temp_db):
        """Test cost estimation with no data."""
        from src.core.analytics import CostEstimator

        audit = AuditLog(db_path=temp_db)
        await audit._ensure_db()

        estimator = CostEstimator(db_path=temp_db)
        costs = await estimator.estimate_costs(30)

        assert costs["total_estimated_cost_usd"] == 0


# === SmartAdvisor Tests ===


class TestSmartAdvisor:
    """Test smart suggestion engine."""

    @pytest.mark.asyncio
    async def test_suggestions_with_no_data(self, tmp_path):
        """Test that advisor works with empty data."""
        from src.core.analytics import SmartAdvisor

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        db_path = str(tmp_path / "audit.db")

        # Initialize the audit db
        audit = AuditLog(db_path=db_path)
        await audit._ensure_db()

        advisor = SmartAdvisor(db_path=db_path, log_dir=log_dir)
        result = await advisor.get_suggestions()

        assert "suggestions" in result
        assert "summary" in result
        assert len(result["suggestions"]) > 0

    @pytest.mark.asyncio
    async def test_suggestions_structure(self, tmp_path):
        """Test that suggestions have required fields."""
        from src.core.analytics import SmartAdvisor

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        db_path = str(tmp_path / "audit.db")

        audit = AuditLog(db_path=db_path)
        await audit._ensure_db()

        advisor = SmartAdvisor(db_path=db_path, log_dir=log_dir)
        result = await advisor.get_suggestions()

        for s in result["suggestions"]:
            assert "category" in s
            assert "priority" in s
            assert "title" in s
            assert "detail" in s
