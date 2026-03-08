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


async def handle_diagnose_command(max_chars: int = 1900) -> str:
    """Handle !diagnose / /diagnose — quick system health check.

    Runs a lightweight diagnostic scan without LLM round-trip.
    For full self-repair, use the diagnose_and_repair tool via chat.
    """
    from src.tools.analytics.diagnostic_tools import _read_recent_entries

    try:
        lines = ["\U0001f3e5 System Diagnostic Report\n"]

        # 1. Recent errors
        errors = await _read_recent_entries(
            entry_type="tool_call",
            status_filter="error",
            limit=20,
        )
        if errors:
            tool_counts: dict[str, int] = {}
            for e in errors:
                t = e.get("tool", "?")
                tool_counts[t] = tool_counts.get(t, 0) + 1

            lines.append(f"\u26a0 {len(errors)} recent errors:")
            for t, c in sorted(tool_counts.items(), key=lambda x: -x[1])[:5]:
                lines.append(f"  {t}: {c} error(s)")
        else:
            lines.append("\u2705 No recent errors")

        # 2. Performance stats
        entries = await _read_recent_entries(
            entry_type="tool_call",
            limit=50,
        )
        if entries:
            total_dur = sum(e.get("duration_ms", 0) for e in entries)
            error_count = sum(1 for e in entries if e.get("status") != "ok")
            lines.append(f"\n\U0001f4ca Performance ({len(entries)} recent calls):")
            lines.append(f"  Total time: {total_dur / 1000:.1f}s")
            lines.append(f"  Error rate: {error_count * 100 // max(len(entries), 1)}%")

            # Find slowest tool
            tool_stats: dict[str, list[int]] = {}
            for e in entries:
                tool = e.get("tool", "?")
                tool_stats.setdefault(tool, []).append(e.get("duration_ms", 0))
            slowest = max(tool_stats.items(), key=lambda x: sum(x[1]) / len(x[1]))
            avg_ms = sum(slowest[1]) // len(slowest[1])
            lines.append(f"  Slowest: {slowest[0]} (avg {avg_ms}ms)")

        # 3. Diagnostics config status
        try:
            from src.config import load_config
            config = load_config()
            diag = config.diagnostics
            lines.append(f"\n\u2699 Diagnostics config:")
            lines.append(f"  Enabled: {diag.enabled}")
            lines.append(f"  Repair model: {diag.repair_model}")
            lines.append(f"  Auto-diagnose: {diag.auto_diagnose_on_error}")
            lines.append(f"  Include in agents: {diag.include_in_agents}")
        except Exception:
            lines.append(f"\n\u2699 Diagnostics config: default")

        result = "\n".join(lines)
        return result[:max_chars] if len(result) > max_chars else result

    except Exception as e:
        logger.warning("diagnose_command_error", error=str(e))
        return f"\u274c Failed to run diagnostics: {e}"
