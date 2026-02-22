"""LLM-callable analytics tools for dashboard access from any adapter.

These tools let users query usage stats, token costs, and security posture
via natural language in Discord, Telegram, Slack, LINE, Email, or Web chat.
All tools are LOW risk and auto-discovered (no dependency injection needed).
"""

from __future__ import annotations

from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class DashboardSummaryTool(BaseTool):
    """Show a combined dashboard overview: usage, costs, security score."""

    name = "dashboard_summary"
    description = (
        "Get a dashboard overview showing tool usage stats, token costs, "
        "and security score. Use this when the user asks about system status, "
        "usage statistics, or wants a general overview."
    )
    parameters = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Number of days to analyze (default: 30)",
                "minimum": 1,
                "maximum": 90,
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        from src.core.analytics import CostEstimator, UsageAnalyzer
        from src.core.security.audit import AuditLog
        from src.tools.analytics.formatters import DashboardFormatter

        days = params.get("days", 30)
        try:
            usage = await UsageAnalyzer().get_usage_summary(days)
            costs = await CostEstimator().estimate_costs(days)
            score = await AuditLog().get_security_score()
            text = DashboardFormatter.format_summary(usage, costs, score)
            return ToolResult.ok(text)
        except Exception as e:
            return ToolResult.fail(f"Failed to load dashboard: {e}")


class TokenUsageReportTool(BaseTool):
    """Show detailed per-model token usage and cost breakdown."""

    name = "token_usage_report"
    description = (
        "Get a detailed token usage report showing per-model breakdown "
        "of prompt tokens, completion tokens, costs, and the pricing table. "
        "Use this when the user asks about token consumption, model costs, "
        "or pricing rates."
    )
    parameters = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Number of days to analyze (default: 30)",
                "minimum": 1,
                "maximum": 90,
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        from src.core.analytics import CostEstimator, get_pricing_info
        from src.tools.analytics.formatters import DashboardFormatter

        days = params.get("days", 30)
        try:
            costs = await CostEstimator().estimate_costs(days)
            pricing = get_pricing_info()
            text = DashboardFormatter.format_token_report(costs, pricing)
            return ToolResult.ok(text)
        except Exception as e:
            return ToolResult.fail(f"Failed to load token report: {e}")


class SecurityReportTool(BaseTool):
    """Show security posture: score, grade, blocked operations, recommendations."""

    name = "security_report"
    description = (
        "Get a security report showing the security score/grade, "
        "integrity status, blocked operations, and recommendations. "
        "Use this when the user asks about security, blocked actions, "
        "or safety posture."
    )
    parameters = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Days for blocked operation stats (default: 7)",
                "minimum": 1,
                "maximum": 90,
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        from src.core.analytics import SmartAdvisor
        from src.core.security.audit import AuditLog
        from src.tools.analytics.formatters import DashboardFormatter

        days = params.get("days", 7)
        try:
            audit = AuditLog()
            score = await audit.get_security_score()
            blocked = await audit.get_blocked_count(days)
            daily = await audit.get_daily_stats()
            advisor = SmartAdvisor()
            suggestions_data = await advisor.get_suggestions()
            suggestions = suggestions_data.get("suggestions", [])

            text = DashboardFormatter.format_security_report(
                score, blocked, daily, suggestions
            )
            return ToolResult.ok(text)
        except Exception as e:
            return ToolResult.fail(f"Failed to load security report: {e}")
