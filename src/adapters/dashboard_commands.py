"""Shared dashboard quick-command handlers for all adapters.

These functions are called directly by adapter command handlers
(e.g., Discord !stats, Telegram /stats) to provide instant responses
without an LLM round-trip.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()


async def handle_stats_command(max_chars: int = 1900) -> str:
    """Handle !stats / /stats — dashboard overview."""
    from src.core.analytics import CostEstimator, UsageAnalyzer
    from src.core.security.audit import AuditLog
    from src.tools.analytics.formatters import DashboardFormatter

    try:
        usage = await UsageAnalyzer().get_usage_summary(30)
        costs = await CostEstimator().estimate_costs(30)
        score = await AuditLog().get_security_score()
        return DashboardFormatter.format_summary(usage, costs, score, max_chars)
    except Exception as e:
        logger.warning("stats_command_error", error=str(e))
        return f"\u274c Failed to load stats: {e}"


async def handle_costs_command(max_chars: int = 1900) -> str:
    """Handle !costs / /costs — token usage and cost breakdown."""
    from src.core.analytics import CostEstimator, get_pricing_info
    from src.tools.analytics.formatters import DashboardFormatter

    try:
        costs = await CostEstimator().estimate_costs(30)
        pricing = get_pricing_info()
        return DashboardFormatter.format_token_report(costs, pricing, max_chars)
    except Exception as e:
        logger.warning("costs_command_error", error=str(e))
        return f"\u274c Failed to load costs: {e}"


async def handle_security_command(max_chars: int = 1900) -> str:
    """Handle !security / /security — security posture report."""
    from src.core.analytics import SmartAdvisor
    from src.core.security.audit import AuditLog
    from src.tools.analytics.formatters import DashboardFormatter

    try:
        audit = AuditLog()
        score = await audit.get_security_score()
        blocked = await audit.get_blocked_count(7)
        daily = await audit.get_daily_stats()
        suggestions_data = await SmartAdvisor().get_suggestions()
        suggestions = suggestions_data.get("suggestions", [])
        return DashboardFormatter.format_security_report(
            score, blocked, daily, suggestions, max_chars
        )
    except Exception as e:
        logger.warning("security_command_error", error=str(e))
        return f"\u274c Failed to load security report: {e}"
