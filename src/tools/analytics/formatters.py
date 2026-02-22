"""Shared dashboard formatters for chat-friendly output.

Used by both LLM tools and adapter quick commands to produce
consistent, compact text suitable for Discord/Telegram/Slack/LINE/Email.
"""

from __future__ import annotations

from typing import Any


def _fmt_tokens(n: int) -> str:
    """Format token count: 1234567 -> '1.2M', 45600 -> '45.6K'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_cost(v: float | None) -> str:
    """Format cost value, or 'N/A' if None."""
    if v is None:
        return "N/A"
    if v == 0:
        return "Free"
    if v < 0.01:
        return f"${v:.4f}"
    return f"${v:.2f}"


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text and add indicator if needed."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...(truncated)"


class DashboardFormatter:
    """Format analytics/security data into chat-friendly text.

    Shared by LLM analytics tools and adapter quick commands.
    All format_* methods accept raw dicts from the analytics classes
    and return a formatted string.
    """

    @staticmethod
    def format_summary(
        usage: dict[str, Any],
        costs: dict[str, Any],
        score: dict[str, Any],
        max_chars: int = 1900,
    ) -> str:
        """Format a combined dashboard overview.

        Args:
            usage: Result from UsageAnalyzer.get_usage_summary()
            costs: Result from CostEstimator.estimate_costs()
            score: Result from AuditLog.get_security_score()
        """
        days = usage.get("period_days", 30)
        total_calls = usage.get("total_calls", 0)
        sessions = usage.get("unique_sessions", 0)
        error_rate = usage.get("error_rate", 0)
        total_cost = costs.get("total_estimated_cost_usd", 0)
        total_tokens = costs.get("total_tokens", 0)
        sec_score = score.get("score", 0)
        sec_grade = score.get("grade", "?")

        lines: list[str] = []
        lines.append(f"\U0001f4ca Dashboard ({days}d)")
        lines.append("\u2500" * 20)

        # Summary stats
        lines.append(
            f"\U0001f527 Tool Calls: {total_calls:,}  |  Sessions: {sessions}"
        )
        lines.append(f"\u274c Error Rate: {error_rate}%")
        lines.append(
            f"\U0001f4b0 Est. Cost: {_fmt_cost(total_cost)}  |  "
            f"Tokens: {_fmt_tokens(total_tokens)}"
        )
        lines.append(f"\U0001f512 Security: {sec_score}/100 ({sec_grade})")

        # Top tools
        most_used = usage.get("most_used", [])
        if most_used:
            lines.append("")
            lines.append("\U0001f4c8 Top Tools")
            for name, count in most_used[:5]:
                lines.append(f"  {name:<22} {count:>5}")

        # Cost by model (top entries)
        by_model = costs.get("by_model", {})
        if by_model:
            # Sort by tokens desc
            sorted_models = sorted(
                by_model.items(), key=lambda x: x[1].get("total_tokens", 0), reverse=True
            )
            lines.append("")
            lines.append("\U0001f4b5 Model Usage (Top 5)")
            for model, info in sorted_models[:5]:
                cost_str = _fmt_cost(info.get("estimated_cost_usd"))
                tokens_str = _fmt_tokens(info.get("total_tokens", 0))
                short_name = model.split("/")[-1] if "/" in model else model
                lines.append(f"  {short_name:<24} {cost_str:>8}  ({tokens_str})")

        return _truncate("\n".join(lines), max_chars)

    @staticmethod
    def format_token_report(
        costs: dict[str, Any],
        pricing: dict[str, Any],
        max_chars: int = 1900,
    ) -> str:
        """Format detailed per-model token/cost breakdown.

        Args:
            costs: Result from CostEstimator.estimate_costs()
            pricing: Result from get_pricing_info()
        """
        days = costs.get("period_days", 30)
        total_cost = costs.get("total_estimated_cost_usd", 0)
        total_tokens = costs.get("total_tokens", 0)

        lines: list[str] = []
        lines.append(f"\U0001fa99 Token Usage Report ({days}d)")
        lines.append("\u2500" * 20)
        lines.append(
            f"Total Tokens: {_fmt_tokens(total_tokens)}  |  "
            f"Est. Cost: {_fmt_cost(total_cost)}"
        )

        # Per-model table
        by_model = costs.get("by_model", {})
        if by_model:
            sorted_models = sorted(
                by_model.items(),
                key=lambda x: x[1].get("total_tokens", 0),
                reverse=True,
            )
            lines.append("")
            # Header
            lines.append(
                f"{'Model':<28} {'Calls':>5}  {'Prompt':>7}  "
                f"{'Compl.':>7}  {'Total':>7}  {'Cost':>8}"
            )
            lines.append("\u2500" * 76)
            for model, info in sorted_models:
                calls = info.get("calls", 0)
                pt = _fmt_tokens(info.get("prompt_tokens", 0))
                ct = _fmt_tokens(info.get("completion_tokens", 0))
                tt = _fmt_tokens(info.get("total_tokens", 0))
                cost_str = _fmt_cost(info.get("estimated_cost_usd"))
                # Shorten model name if too long
                name = model if len(model) <= 28 else model.split("/")[-1]
                lines.append(
                    f"{name:<28} {calls:>5}  {pt:>7}  "
                    f"{ct:>7}  {tt:>7}  {cost_str:>8}"
                )

        # Pricing reference table
        models_pricing = pricing.get("models", {})
        if models_pricing:
            last_updated = pricing.get("last_updated", "unknown")
            lines.append("")
            lines.append(f"\U0001f4cb Pricing (updated: {last_updated})")
            for model, rates in models_pricing.items():
                inp = rates.get("input", 0)
                out = rates.get("output", 0)
                if inp == 0 and out == 0:
                    lines.append(f"  {model:<34} Free")
                else:
                    lines.append(
                        f"  {model:<34} in:${inp}  out:${out}"
                    )

        return _truncate("\n".join(lines), max_chars)

    @staticmethod
    def format_security_report(
        score: dict[str, Any],
        blocked: dict[str, Any],
        daily: dict[str, Any],
        suggestions: list[dict[str, Any]] | None = None,
        max_chars: int = 1900,
    ) -> str:
        """Format security posture report.

        Args:
            score: Result from AuditLog.get_security_score()
            blocked: Result from AuditLog.get_blocked_count()
            daily: Result from AuditLog.get_daily_stats()
            suggestions: Filtered security suggestions from SmartAdvisor
        """
        sec_score = score.get("score", 0)
        sec_grade = score.get("grade", "?")

        lines: list[str] = []
        lines.append("\U0001f512 Security Report")
        lines.append("\u2500" * 20)
        lines.append(f"Score: {sec_score}/100 (Grade {sec_grade})")

        # Factors
        factors = score.get("factors", [])
        for f in factors:
            icon = "\u2705" if f.get("status") == "ok" else "\u26a0\ufe0f"
            lines.append(f"{icon} {f.get('name', '')}: {f.get('detail', '')}")

        # Blocked stats
        total_approved = blocked.get("total_approved", 0)
        total_blocked = blocked.get("total_blocked", 0)
        blocked_days = blocked.get("days", 7)
        lines.append("")
        lines.append(f"\U0001f4ca {blocked_days}d Stats")
        lines.append(
            f"  Approved: {total_approved:,}  |  Denied: {total_blocked:,}"
        )

        # Today's activity
        today_total = daily.get("total_events", 0)
        risk_dist = daily.get("risk_distribution", {})
        if today_total:
            high_risk = risk_dist.get("high", 0) + risk_dist.get("critical", 0)
            lines.append(
                f"  Today: {today_total} events  "
                f"(high-risk: {high_risk})"
            )

        # Peak hour
        hourly = daily.get("hourly_activity", [])
        if hourly and any(hourly):
            peak_hour = hourly.index(max(hourly))
            lines.append(f"  Peak hour: {peak_hour:02d}:00 UTC")

        # Recommendations
        recommendations = score.get("recommendations", [])
        if recommendations:
            lines.append("")
            lines.append("\U0001f4a1 Recommendations")
            for rec in recommendations[:3]:
                lines.append(f"  \u2022 {rec}")

        # Security-related suggestions from SmartAdvisor
        if suggestions:
            sec_suggestions = [
                s for s in suggestions if s.get("category") == "security"
            ]
            if sec_suggestions:
                lines.append("")
                lines.append("\U0001f6e1\ufe0f Advisor")
                for s in sec_suggestions[:3]:
                    icon = s.get("icon", "\U0001f536")
                    lines.append(f"  {icon} {s.get('title', '')}")

        return _truncate("\n".join(lines), max_chars)
