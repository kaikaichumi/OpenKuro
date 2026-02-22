"""Usage analytics and smart suggestions engine.

Analyzes action logs and audit data to provide:
- Tool usage statistics
- Cost estimation per model/provider
- Smart optimization suggestions
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

from src.config import get_kuro_home

logger = structlog.get_logger()

# Token cost per 1K tokens (USD)
# Last updated: 2026-02-22
MODEL_COSTS_UPDATED = "2026-02-22"
MODEL_COSTS: dict[str, dict[str, float]] = {
    "anthropic/claude-sonnet-4.5": {"input": 0.003, "output": 0.015},
    "anthropic/claude-opus-4.6": {"input": 0.015, "output": 0.075},
    "anthropic/claude-haiku-4.5": {"input": 0.001, "output": 0.005},
    "openai/gpt-5.3-codex": {"input": 0.003, "output": 0.012},
    "openai/gpt-5.2": {"input": 0.005, "output": 0.015},
    "openai/gpt-5": {"input": 0.005, "output": 0.015},
    "gemini/gemini-3-flash": {"input": 0.0001, "output": 0.0004},
    "gemini/gemini-3-pro": {"input": 0.00125, "output": 0.005},
    "gemini/gemini-2.5-flash": {"input": 0.00015, "output": 0.0006},
    "gemini/gemini-2.5-pro": {"input": 0.00125, "output": 0.005},
    # Ollama/local models are free
    "ollama/*": {"input": 0.0, "output": 0.0},
}


def _get_model_cost(model: str) -> dict[str, float] | None:
    """Get cost per 1K tokens for a model, or None if not in pricing table."""
    if model in MODEL_COSTS:
        return MODEL_COSTS[model]
    # Check prefix match for ollama
    provider = model.split("/")[0] if "/" in model else model
    if provider == "ollama":
        return {"input": 0.0, "output": 0.0}
    return None  # Unknown model: no cost estimation


def get_pricing_info() -> dict[str, Any]:
    """Return pricing table metadata for the frontend."""
    return {
        "last_updated": MODEL_COSTS_UPDATED,
        "models": {
            model: {"input": cost["input"], "output": cost["output"]}
            for model, cost in MODEL_COSTS.items()
        },
    }


class UsageAnalyzer:
    """Analyze tool usage patterns from action logs."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self._log_dir = log_dir or (get_kuro_home() / "action_logs")

    async def get_usage_summary(self, days: int = 30) -> dict[str, Any]:
        """Get tool usage summary for the last N days.

        Returns:
            dict with tool_counts, daily_activity, total_calls,
            avg_duration, most_used, least_used, error_rate
        """
        tool_counts: Counter[str] = Counter()
        daily_activity: dict[str, int] = defaultdict(int)
        durations: dict[str, list[int]] = defaultdict(list)
        errors: int = 0
        total_calls: int = 0
        sessions: set[str] = set()

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        for log_file in sorted(self._log_dir.glob("actions-*.jsonl")):
            try:
                date_str = log_file.stem.replace("actions-", "").split("-")
                if len(date_str) >= 3:
                    file_date = datetime(
                        int(date_str[0]), int(date_str[1]), int(date_str[2]),
                        tzinfo=timezone.utc,
                    )
                    if file_date < cutoff:
                        continue
            except (ValueError, IndexError):
                continue

            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if entry.get("type") != "tool_call":
                            continue

                        tool = entry.get("tool", "unknown")
                        tool_counts[tool] += 1
                        total_calls += 1

                        # Extract date for daily activity
                        ts = entry.get("ts", "")
                        if ts:
                            day = ts[:10]
                            daily_activity[day] += 1

                        # Track durations
                        dur = entry.get("duration_ms", 0)
                        if dur > 0:
                            durations[tool].append(dur)

                        # Track errors
                        if entry.get("status") == "error" or entry.get("error"):
                            errors += 1

                        # Track sessions
                        sid = entry.get("sid", "")
                        if sid:
                            sessions.add(sid)

            except Exception as e:
                logger.warning("analytics_read_error", file=str(log_file), error=str(e))

        # Compute averages
        avg_durations = {}
        for tool, durs in durations.items():
            avg_durations[tool] = round(sum(durs) / len(durs)) if durs else 0

        # Sort daily activity
        sorted_daily = sorted(daily_activity.items())

        return {
            "period_days": days,
            "total_calls": total_calls,
            "unique_sessions": len(sessions),
            "error_count": errors,
            "error_rate": round(errors / total_calls * 100, 1) if total_calls > 0 else 0,
            "tool_counts": dict(tool_counts.most_common()),
            "daily_activity": [{"date": d, "count": c} for d, c in sorted_daily],
            "avg_duration_ms": avg_durations,
            "most_used": tool_counts.most_common(5),
            "least_used": tool_counts.most_common()[:-6:-1] if len(tool_counts) >= 5 else [],
        }


class CostEstimator:
    """Estimate API costs from actual token usage data."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or str(get_kuro_home() / "audit.db")

    async def estimate_costs(self, days: int = 30) -> dict[str, Any]:
        """Calculate costs from real token_usage data.

        For models in MODEL_COSTS, computes actual cost.
        For unknown models, returns token counts only (no cost).
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

        # Aggregate token usage per model
        model_data: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        daily_costs: dict[str, float] = defaultdict(float)
        daily_tokens: dict[str, int] = defaultdict(int)

        try:
            async with aiosqlite.connect(self._db_path) as db:
                # Check if token_usage table exists
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'"
                ) as cursor:
                    if await cursor.fetchone() is None:
                        # Table doesn't exist yet → return empty
                        return self._empty_result(days)

                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """SELECT model, prompt_tokens, completion_tokens, total_tokens, timestamp
                       FROM token_usage
                       WHERE timestamp >= ?
                       ORDER BY timestamp""",
                    (cutoff,),
                ) as cursor:
                    async for row in cursor:
                        model = row["model"]
                        pt = row["prompt_tokens"]
                        ct = row["completion_tokens"]
                        tt = row["total_tokens"]
                        day = row["timestamp"][:10]

                        model_data[model]["calls"] += 1
                        model_data[model]["prompt_tokens"] += pt
                        model_data[model]["completion_tokens"] += ct
                        model_data[model]["total_tokens"] += tt

                        daily_tokens[day] += tt

                        # Only compute cost for known models
                        cost_info = _get_model_cost(model)
                        if cost_info is not None:
                            call_cost = pt / 1000 * cost_info["input"] + ct / 1000 * cost_info["output"]
                            daily_costs[day] += call_cost

        except Exception as e:
            logger.warning("cost_estimation_error", error=str(e))

        # Build per-model breakdown
        by_model: dict[str, dict[str, Any]] = {}
        total_cost = 0.0
        total_tokens = 0

        for model, data in sorted(model_data.items(), key=lambda x: -x[1]["total_tokens"]):
            cost_info = _get_model_cost(model)
            has_pricing = cost_info is not None

            entry: dict[str, Any] = {
                "calls": data["calls"],
                "prompt_tokens": data["prompt_tokens"],
                "completion_tokens": data["completion_tokens"],
                "total_tokens": data["total_tokens"],
                "has_pricing": has_pricing,
            }

            if has_pricing:
                est = (
                    data["prompt_tokens"] / 1000 * cost_info["input"]
                    + data["completion_tokens"] / 1000 * cost_info["output"]
                )
                entry["estimated_cost_usd"] = round(est, 6)
                entry["pricing"] = {"input": cost_info["input"], "output": cost_info["output"]}
                total_cost += est
            else:
                entry["estimated_cost_usd"] = None
                entry["pricing"] = None

            total_tokens += data["total_tokens"]
            by_model[model] = entry

        sorted_daily = sorted(daily_costs.items())
        sorted_daily_tokens = sorted(daily_tokens.items())

        return {
            "period_days": days,
            "total_estimated_cost_usd": round(total_cost, 6),
            "total_tokens": total_tokens,
            "by_model": by_model,
            "daily_costs": [
                {"date": d, "cost_usd": round(c, 6)} for d, c in sorted_daily
            ],
            "daily_tokens": [
                {"date": d, "tokens": t} for d, t in sorted_daily_tokens
            ],
            "pricing_info": get_pricing_info(),
        }

    @staticmethod
    def _empty_result(days: int) -> dict[str, Any]:
        return {
            "period_days": days,
            "total_estimated_cost_usd": 0,
            "total_tokens": 0,
            "by_model": {},
            "daily_costs": [],
            "daily_tokens": [],
            "pricing_info": get_pricing_info(),
        }


class SmartAdvisor:
    """Generate smart optimization suggestions from usage patterns."""

    def __init__(
        self,
        db_path: str | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self._db_path = db_path or str(get_kuro_home() / "audit.db")
        self._log_dir = log_dir or (get_kuro_home() / "action_logs")

    async def get_suggestions(self) -> dict[str, Any]:
        """Analyze usage and return optimization suggestions.

        Categories:
        - cost: Ways to reduce API costs
        - security: Security posture improvements
        - efficiency: Workflow automation suggestions
        """
        suggestions: list[dict[str, str]] = []

        # --- Analyze tool usage ---
        analyzer = UsageAnalyzer(self._log_dir)
        try:
            usage = await analyzer.get_usage_summary(14)
        except Exception:
            usage = {"total_calls": 0, "tool_counts": {}, "error_rate": 0}

        tool_counts = usage.get("tool_counts", {})
        total_calls = usage.get("total_calls", 0)

        # Suggestion: High error rate
        error_rate = usage.get("error_rate", 0)
        if error_rate > 10:
            suggestions.append({
                "category": "efficiency",
                "priority": "high",
                "title": "High error rate detected",
                "detail": f"Error rate is {error_rate}% over the last 14 days. "
                          "Review frequently failing tools and check configurations.",
                "icon": "⚠️",
            })

        # Suggestion: Frequent shell_execute
        shell_count = tool_counts.get("shell_execute", 0)
        if shell_count > 20:
            suggestions.append({
                "category": "security",
                "priority": "medium",
                "title": "Frequent shell command execution",
                "detail": f"shell_execute was called {shell_count} times in 14 days. "
                          "Consider creating Skills or Plugins for repetitive commands.",
                "icon": "🔒",
            })

        # Suggestion: Heavily used tool → create Skill
        if total_calls > 0:
            for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1])[:3]:
                ratio = count / total_calls
                if ratio > 0.25 and count > 10:
                    suggestions.append({
                        "category": "efficiency",
                        "priority": "medium",
                        "title": f"'{tool}' is heavily used ({ratio:.0%} of calls)",
                        "detail": f"Called {count} times. Consider creating a Skill or "
                                  "Workflow to automate common patterns involving this tool.",
                        "icon": "⚡",
                    })

        # --- Analyze cost patterns ---
        estimator = CostEstimator(self._db_path)
        try:
            costs = await estimator.estimate_costs(14)
        except Exception:
            costs = {"total_estimated_cost_usd": 0, "by_model": {}}

        by_model = costs.get("by_model", {})
        total_cost = costs.get("total_estimated_cost_usd", 0)

        # Suggestion: Using expensive model for many simple calls
        for model, info in by_model.items():
            est_cost = info.get("estimated_cost_usd") or 0
            if ("claude-opus" in model or "gpt-5.2" in model) and info["calls"] > 50:
                suggestions.append({
                    "category": "cost",
                    "priority": "high",
                    "title": f"Expensive model '{model}' used frequently",
                    "detail": f"Called {info['calls']} times (~${est_cost}). "
                              "Consider using a sub-agent with Ollama or Gemini Flash for simple tasks.",
                    "icon": "💰",
                })

        # Suggestion: No local model usage
        has_local = any("ollama" in m for m in by_model)
        if not has_local and total_cost > 0.5:
            suggestions.append({
                "category": "cost",
                "priority": "medium",
                "title": "No local model usage detected",
                "detail": "You're only using cloud models. Setting up Ollama with a local "
                          "model could save costs for simple tasks (file operations, searches, etc.).",
                "icon": "🏠",
            })

        # --- Analyze security ---
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                # Check for denied operations
                async with db.execute(
                    """SELECT tool_name, COUNT(*) as cnt FROM audit_log
                       WHERE approval_status = 'denied'
                       GROUP BY tool_name ORDER BY cnt DESC LIMIT 5"""
                ) as cursor:
                    denied_tools = [(row["tool_name"], row["cnt"]) async for row in cursor]

            for tool, count in denied_tools:
                if count >= 5:
                    suggestions.append({
                        "category": "security",
                        "priority": "low",
                        "title": f"'{tool}' frequently denied ({count} times)",
                        "detail": "If these denials are intentional, no action needed. "
                                  "If you trust this tool, consider elevating session trust "
                                  "or adjusting auto_approve_levels.",
                        "icon": "🛡️",
                    })
        except Exception:
            pass

        # Default if no suggestions
        if not suggestions:
            suggestions.append({
                "category": "general",
                "priority": "info",
                "title": "System running optimally",
                "detail": "No optimization suggestions at this time. Keep using Kuro!",
                "icon": "✅",
            })

        return {
            "suggestions": suggestions,
            "summary": {
                "total_calls_14d": total_calls,
                "estimated_cost_14d": total_cost,
                "error_rate": error_rate,
            },
        }
