"""Usage analytics and smart suggestions engine.

Analyzes action logs and audit data to provide:
- Tool usage statistics
- Cost estimation per model/provider
- Smart optimization suggestions
- Budget guardrails (notify / hard-stop)
"""

from __future__ import annotations

import asyncio
import fnmatch
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


_CUSTOM_PRICING_FILE = "custom_pricing.json"
_BUDGET_RULES_FILE = "analytics_budget_rules.json"
_BUDGET_STATE_FILE = "analytics_budget_state.json"
_VALID_BUDGET_PERIODS = {"daily", "weekly", "monthly"}
_VALID_BUDGET_ACTIONS = {"notify", "stop"}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if value is None:
            return default
        return int(str(value).strip())
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).strip())
    except Exception:
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _parse_ymd(value: str | None) -> datetime | None:
    """Parse YYYY-MM-DD into UTC datetime at 00:00:00."""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_iso_ts(value: str | None) -> datetime | None:
    """Parse ISO timestamp into UTC datetime."""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_time_range(
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[datetime, datetime, int]:
    """Resolve the query range as [start, end_exclusive) in UTC."""
    now = datetime.now(timezone.utc)
    safe_days = max(1, _safe_int(days, 30))

    start_dt = _parse_ymd(start_date)
    end_day_dt = _parse_ymd(end_date)

    if start_dt is not None or end_day_dt is not None:
        end_dt = end_day_dt + timedelta(days=1) if end_day_dt is not None else now
        if start_dt is None:
            start_dt = end_dt - timedelta(days=safe_days)
    else:
        end_dt = now
        start_dt = now - timedelta(days=safe_days)

    if start_dt >= end_dt:
        end_dt = start_dt + timedelta(days=1)

    period_days = max(1, (end_dt.date() - start_dt.date()).days)
    return start_dt, end_dt, period_days


def _range_payload(start_dt: datetime, end_dt: datetime, period_days: int) -> dict[str, Any]:
    end_display = (end_dt - timedelta(microseconds=1)).date().isoformat()
    return {
        "period_days": period_days,
        "period_start": start_dt.date().isoformat(),
        "period_end": end_display,
    }


def _load_custom_pricing() -> dict[str, dict[str, float]]:
    """Load user-defined pricing overrides from ~/.kuro/custom_pricing.json."""
    path = get_kuro_home() / _CUSTOM_PRICING_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_custom_pricing(pricing: dict[str, dict[str, float]]) -> None:
    """Persist user-defined pricing overrides to disk."""
    path = get_kuro_home() / _CUSTOM_PRICING_FILE
    path.write_text(json.dumps(pricing, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_effective_pricing() -> dict[str, dict[str, float]]:
    """Get merged pricing: built-in defaults overridden by user custom pricing."""
    merged = {m: dict(c) for m, c in MODEL_COSTS.items()}
    custom = _load_custom_pricing()
    for model, cost in custom.items():
        merged[model] = cost
    return merged


def _get_model_cost(model: str) -> dict[str, float] | None:
    """Get cost per 1K tokens for a model, or None if not in pricing table."""
    effective = _get_effective_pricing()
    if model in effective:
        return effective[model]
    # Check prefix match for ollama / openai local models
    provider = model.split("/")[0] if "/" in model else model
    if provider in ("ollama", "lmstudio"):
        return {"input": 0.0, "output": 0.0}
    # Check custom pricing prefix wildcard (e.g. "openai/*")
    wildcard = f"{provider}/*"
    if wildcard in effective:
        return effective[wildcard]
    return None  # Unknown model: no cost estimation


def get_pricing_info() -> dict[str, Any]:
    """Return pricing table metadata for the frontend."""
    effective = _get_effective_pricing()
    custom = _load_custom_pricing()
    return {
        "last_updated": MODEL_COSTS_UPDATED,
        "models": {
            model: {
                "input": cost["input"],
                "output": cost["output"],
                "custom": model in custom,
            }
            for model, cost in effective.items()
        },
    }


def update_model_pricing(model: str, input_rate: float, output_rate: float) -> dict[str, float]:
    """Update pricing for a specific model. Returns the updated pricing entry."""
    custom = _load_custom_pricing()
    custom[model] = {"input": input_rate, "output": output_rate}
    _save_custom_pricing(custom)
    return custom[model]


def delete_custom_pricing(model: str) -> bool:
    """Remove custom pricing override for a model, reverting to built-in default."""
    custom = _load_custom_pricing()
    if model in custom:
        del custom[model]
        _save_custom_pricing(custom)
        return True
    return False


class UsageAnalyzer:
    """Analyze tool usage patterns from action logs."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self._log_dir = log_dir or (get_kuro_home() / "action_logs")

    async def get_usage_summary(
        self,
        days: int = 30,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Get tool usage summary for a date range.

        If start_date/end_date are omitted, falls back to the last ``days`` days.
        """
        start_dt, end_dt, period_days = _resolve_time_range(days, start_date, end_date)

        tool_counts: Counter[str] = Counter()
        daily_activity: dict[str, int] = defaultdict(int)
        durations: dict[str, list[int]] = defaultdict(list)
        errors = 0
        total_calls = 0
        sessions: set[str] = set()

        start_day = start_dt.date()
        end_day = end_dt.date()  # exclusive

        for log_file in sorted(self._log_dir.glob("actions-*.jsonl")):
            # Fast file-level filtering using filename date when available
            try:
                parts = log_file.stem.replace("actions-", "").split("-")
                if len(parts) >= 3:
                    file_day = datetime(
                        int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=timezone.utc
                    ).date()
                    if file_day < start_day or file_day >= end_day:
                        continue
            except (ValueError, IndexError):
                pass

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

                        ts_dt = _parse_iso_ts(entry.get("ts", ""))
                        if ts_dt is not None:
                            if ts_dt < start_dt or ts_dt >= end_dt:
                                continue
                            day = ts_dt.strftime("%Y-%m-%d")
                        else:
                            # fallback: day-only lexical comparison
                            ts_raw = str(entry.get("ts", "") or "")
                            day = ts_raw[:10]
                            if day < start_day.isoformat() or day >= end_day.isoformat():
                                continue

                        tool = str(entry.get("tool", "unknown") or "unknown")
                        tool_counts[tool] += 1
                        total_calls += 1
                        daily_activity[day] += 1

                        dur = _safe_int(entry.get("duration_ms", 0), 0)
                        if dur > 0:
                            durations[tool].append(dur)

                        if entry.get("status") == "error" or entry.get("error"):
                            errors += 1

                        sid = str(entry.get("sid", "") or "")
                        if sid:
                            sessions.add(sid)

            except Exception as e:
                logger.warning("analytics_read_error", file=str(log_file), error=str(e))

        avg_durations: dict[str, int] = {}
        for tool, durs in durations.items():
            avg_durations[tool] = round(sum(durs) / len(durs)) if durs else 0

        sorted_daily = sorted(daily_activity.items())
        payload = _range_payload(start_dt, end_dt, period_days)
        payload.update({
            "total_calls": total_calls,
            "unique_sessions": len(sessions),
            "error_count": errors,
            "error_rate": round(errors / total_calls * 100, 1) if total_calls > 0 else 0,
            "tool_counts": dict(tool_counts.most_common()),
            "daily_activity": [{"date": d, "count": c} for d, c in sorted_daily],
            "avg_duration_ms": avg_durations,
            "most_used": tool_counts.most_common(5),
            "least_used": tool_counts.most_common()[:-6:-1] if len(tool_counts) >= 5 else [],
        })
        return payload


class CostEstimator:
    """Estimate API costs from actual token usage data."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or str(get_kuro_home() / "audit.db")

    async def estimate_costs(
        self,
        days: int = 30,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Calculate costs from real token_usage data.

        If start_date/end_date are omitted, falls back to the last ``days`` days.
        """
        start_dt, end_dt, period_days = _resolve_time_range(days, start_date, end_date)
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()

        model_data: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        daily_costs: dict[str, float] = defaultdict(float)
        daily_tokens: dict[str, int] = defaultdict(int)

        try:
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'"
                ) as cursor:
                    if await cursor.fetchone() is None:
                        return self._empty_result(start_dt, end_dt, period_days)

                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """SELECT model, prompt_tokens, completion_tokens, total_tokens, timestamp
                       FROM token_usage
                       WHERE timestamp >= ? AND timestamp < ?
                       ORDER BY timestamp""",
                    (start_iso, end_iso),
                ) as cursor:
                    async for row in cursor:
                        model = str(row["model"])
                        pt = _safe_int(row["prompt_tokens"], 0)
                        ct = _safe_int(row["completion_tokens"], 0)
                        tt = _safe_int(row["total_tokens"], 0)
                        day = str(row["timestamp"])[:10]

                        model_data[model]["calls"] += 1
                        model_data[model]["prompt_tokens"] += pt
                        model_data[model]["completion_tokens"] += ct
                        model_data[model]["total_tokens"] += tt
                        daily_tokens[day] += tt

                        cost_info = _get_model_cost(model)
                        if cost_info is not None:
                            call_cost = pt / 1000 * cost_info["input"] + ct / 1000 * cost_info["output"]
                            daily_costs[day] += call_cost

        except Exception as e:
            logger.warning("cost_estimation_error", error=str(e))

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

        payload = _range_payload(start_dt, end_dt, period_days)
        payload.update({
            "total_estimated_cost_usd": round(total_cost, 6),
            "total_tokens": total_tokens,
            "by_model": by_model,
            "daily_costs": [{"date": d, "cost_usd": round(c, 6)} for d, c in sorted_daily],
            "daily_tokens": [{"date": d, "tokens": t} for d, t in sorted_daily_tokens],
            "pricing_info": get_pricing_info(),
        })
        return payload

    @staticmethod
    def _empty_result(start_dt: datetime, end_dt: datetime, period_days: int) -> dict[str, Any]:
        payload = _range_payload(start_dt, end_dt, period_days)
        payload.update({
            "total_estimated_cost_usd": 0,
            "total_tokens": 0,
            "by_model": {},
            "daily_costs": [],
            "daily_tokens": [],
            "pricing_info": get_pricing_info(),
        })
        return payload


class BudgetManager:
    """Manage analytics budget rules and evaluate notify/stop limits."""

    def __init__(
        self,
        db_path: str | None = None,
        rules_path: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._db_path = db_path or str(get_kuro_home() / "audit.db")
        self._rules_path = rules_path or (get_kuro_home() / _BUDGET_RULES_FILE)
        self._state_path = state_path or (get_kuro_home() / _BUDGET_STATE_FILE)
        self._lock = asyncio.Lock()

    @property
    def db_path(self) -> str:
        return self._db_path

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _normalize_models(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            val = str(item or "").strip()
            if not val or val in seen:
                continue
            seen.add(val)
            out.append(val)
        return out

    @staticmethod
    def _normalize_notify_targets(raw: Any) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            adapter = ""
            user_id = ""
            if isinstance(item, dict):
                adapter = str(item.get("adapter") or item.get("adapter_name") or "").strip()
                user_id = str(item.get("user_id") or item.get("target") or "").strip()
            elif isinstance(item, str):
                raw_item = item.strip()
                if ":" in raw_item:
                    adapter, user_id = raw_item.split(":", 1)
                    adapter = adapter.strip()
                    user_id = user_id.strip()
            if not adapter or not user_id:
                continue
            key = (adapter, user_id)
            if key in seen:
                continue
            seen.add(key)
            out.append({"adapter": adapter, "user_id": user_id})
        return out

    @staticmethod
    def _normalize_rule_id(raw_id: str, index: int, used_ids: set[str]) -> str:
        base = raw_id.strip() or f"rule_{index + 1}"
        candidate = base
        suffix = 2
        while candidate in used_ids:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used_ids.add(candidate)
        return candidate

    def _normalize_rules(self, raw_rules: Any) -> list[dict[str, Any]]:
        source = raw_rules if isinstance(raw_rules, list) else []
        out: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        for idx, item in enumerate(source):
            data = item if isinstance(item, dict) else {}
            period = str(data.get("period", "monthly")).strip().lower()
            if period not in _VALID_BUDGET_PERIODS:
                period = "monthly"

            action = str(data.get("action", "notify")).strip().lower()
            if action not in _VALID_BUDGET_ACTIONS:
                action = "notify"

            limit_usd = max(0.0, _safe_float(data.get("limit_usd"), 0.0))
            notify_percent = _clamp(_safe_float(data.get("notify_percent"), 80.0), 1.0, 100.0)
            name = str(data.get("name") or "").strip()
            if not name:
                name = f"{period}-{action}-{idx + 1}"

            rule_id = self._normalize_rule_id(str(data.get("id") or ""), idx, used_ids)
            models = self._normalize_models(data.get("models"))
            targets = self._normalize_notify_targets(data.get("notify_targets"))
            enabled = bool(data.get("enabled", True))

            out.append({
                "id": rule_id,
                "name": name,
                "enabled": enabled,
                "period": period,
                "action": action,
                "limit_usd": round(limit_usd, 6),
                "notify_percent": round(notify_percent, 2),
                "models": models,
                "notify_targets": targets,
            })
        return out

    def _load_rules_unlocked(self) -> list[dict[str, Any]]:
        data = self._read_json(self._rules_path, {"rules": []})
        if isinstance(data, dict):
            raw_rules = data.get("rules", [])
        elif isinstance(data, list):
            raw_rules = data
        else:
            raw_rules = []
        rules = self._normalize_rules(raw_rules)
        canonical = {"rules": rules}
        if data != canonical:
            self._write_json(self._rules_path, canonical)
        return rules

    def _save_rules_unlocked(self, rules: list[dict[str, Any]]) -> None:
        normalized = self._normalize_rules(rules)
        self._write_json(self._rules_path, {"rules": normalized})

    def _load_state_unlocked(self) -> dict[str, Any]:
        data = self._read_json(self._state_path, {"notified": {}})
        notified = data.get("notified") if isinstance(data, dict) else {}
        if not isinstance(notified, dict):
            notified = {}
        return {"notified": notified}

    def _save_state_unlocked(self, state: dict[str, Any]) -> None:
        notified = state.get("notified", {})
        if not isinstance(notified, dict):
            notified = {}
        self._write_json(self._state_path, {"notified": notified})

    @staticmethod
    def _period_window(period: str, now: datetime) -> tuple[datetime, datetime, str]:
        anchor = now.astimezone(timezone.utc)
        if period == "daily":
            start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        elif period == "weekly":
            base = anchor - timedelta(days=anchor.weekday())
            start = base.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=7)
        else:  # monthly
            start = anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)
        bucket = f"{period}:{start.date().isoformat()}"
        return start, end, bucket

    @staticmethod
    def _matches_model(model: str, patterns: list[str]) -> bool:
        if not patterns:
            return True
        for pattern in patterns:
            if pattern == model:
                return True
            if fnmatch.fnmatch(model, pattern):
                return True
        return False

    async def _fetch_usage_rows(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'"
                ) as cursor:
                    if await cursor.fetchone() is None:
                        return rows

                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """SELECT timestamp, model, prompt_tokens, completion_tokens, total_tokens
                       FROM token_usage
                       WHERE timestamp >= ? AND timestamp < ?
                       ORDER BY timestamp""",
                    (start_iso, end_iso),
                ) as cursor:
                    async for row in cursor:
                        rows.append(dict(row))
        except Exception as e:
            logger.warning("budget_usage_fetch_error", error=str(e))
        return rows

    async def _compute_rule_stats(
        self,
        rules: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not rules:
            return {}

        now_dt = now or datetime.now(timezone.utc)
        windows: dict[str, dict[str, Any]] = {}
        min_start: datetime | None = None
        max_end: datetime | None = None

        for rule in rules:
            start_dt, end_dt, bucket = self._period_window(rule["period"], now_dt)
            windows[rule["id"]] = {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "start_iso": start_dt.isoformat(),
                "end_iso": end_dt.isoformat(),
                "window_key": bucket,
            }
            if min_start is None or start_dt < min_start:
                min_start = start_dt
            if max_end is None or end_dt > max_end:
                max_end = end_dt

        if min_start is None or max_end is None:
            return {}

        usage_rows = await self._fetch_usage_rows(min_start.isoformat(), max_end.isoformat())

        stats: dict[str, dict[str, Any]] = {}
        for rule in rules:
            win = windows[rule["id"]]
            period_days = max(1, (win["end_dt"].date() - win["start_dt"].date()).days)
            stats[rule["id"]] = {
                "spent_usd": 0.0,
                "calls": 0,
                "tokens": 0,
                "priced_calls": 0,
                "unpriced_calls": 0,
                "period_start": win["start_dt"].date().isoformat(),
                "period_end": (win["end_dt"] - timedelta(microseconds=1)).date().isoformat(),
                "period_days": period_days,
                "window_key": win["window_key"],
            }

        match_cache: dict[tuple[str, str], bool] = {}
        for row in usage_rows:
            ts = str(row.get("timestamp", ""))
            model = str(row.get("model", ""))
            pt = _safe_int(row.get("prompt_tokens"), 0)
            ct = _safe_int(row.get("completion_tokens"), 0)
            tt = _safe_int(row.get("total_tokens"), 0)

            cost_info = _get_model_cost(model)
            row_cost = None
            if cost_info is not None:
                row_cost = pt / 1000 * cost_info["input"] + ct / 1000 * cost_info["output"]

            for rule in rules:
                rule_id = rule["id"]
                win = windows[rule_id]
                if ts < win["start_iso"] or ts >= win["end_iso"]:
                    continue

                cache_key = (rule_id, model)
                matched = match_cache.get(cache_key)
                if matched is None:
                    matched = self._matches_model(model, rule.get("models", []))
                    match_cache[cache_key] = matched
                if not matched:
                    continue

                stat = stats[rule_id]
                stat["calls"] += 1
                stat["tokens"] += tt
                if row_cost is None:
                    stat["unpriced_calls"] += 1
                else:
                    stat["priced_calls"] += 1
                    stat["spent_usd"] += row_cost

        for rule in rules:
            stat = stats[rule["id"]]
            limit = max(0.0, _safe_float(rule.get("limit_usd"), 0.0))
            spent = round(stat["spent_usd"], 6)
            stat["spent_usd"] = spent
            stat["limit_usd"] = round(limit, 6)
            if limit > 0:
                usage_percent = spent / limit * 100
                stat["usage_percent"] = round(usage_percent, 2)
                stat["remaining_usd"] = round(max(0.0, limit - spent), 6)
            else:
                stat["usage_percent"] = 0.0
                stat["remaining_usd"] = None

        return stats

    @staticmethod
    def _prune_state_notified(
        notified: dict[str, Any],
        valid_rule_ids: set[str],
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in notified.items():
            if "|" not in key:
                continue
            rule_id = key.split("|", 1)[0]
            if rule_id not in valid_rule_ids:
                continue
            out[key] = value
        return out

    async def get_rules(self, include_stats: bool = True) -> dict[str, Any]:
        async with self._lock:
            rules = self._load_rules_unlocked()
            stats = await self._compute_rule_stats(rules) if include_stats else {}

        merged_rules: list[dict[str, Any]] = []
        for rule in rules:
            item = dict(rule)
            if include_stats:
                stat = stats.get(rule["id"], {})
                item["stats"] = stat
                if rule["action"] == "stop":
                    item["is_blocked_now"] = bool(
                        _safe_float(stat.get("limit_usd"), 0.0) > 0
                        and _safe_float(stat.get("spent_usd"), 0.0) >= _safe_float(stat.get("limit_usd"), 0.0)
                    )
            merged_rules.append(item)

        return {
            "rules": merged_rules,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def replace_rules(self, rules: list[dict[str, Any]]) -> dict[str, Any]:
        async with self._lock:
            normalized = self._normalize_rules(rules)
            self._save_rules_unlocked(normalized)

            state = self._load_state_unlocked()
            notified = state.get("notified", {})
            if not isinstance(notified, dict):
                notified = {}
            valid_ids = {r["id"] for r in normalized}
            state["notified"] = self._prune_state_notified(notified, valid_ids)
            self._save_state_unlocked(state)

            stats = await self._compute_rule_stats(normalized)

        merged_rules: list[dict[str, Any]] = []
        for rule in normalized:
            item = dict(rule)
            item["stats"] = stats.get(rule["id"], {})
            if rule["action"] == "stop":
                stat = item["stats"]
                item["is_blocked_now"] = bool(
                    _safe_float(stat.get("limit_usd"), 0.0) > 0
                    and _safe_float(stat.get("spent_usd"), 0.0) >= _safe_float(stat.get("limit_usd"), 0.0)
                )
            merged_rules.append(item)

        return {
            "rules": merged_rules,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def check_stop_limits(
        self,
        model: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Check if any enabled hard-stop budget rule is exceeded for this model."""
        async with self._lock:
            rules = [
                r for r in self._load_rules_unlocked()
                if r.get("enabled")
                and r.get("action") == "stop"
                and self._matches_model(model, r.get("models", []))
            ]
            if not rules:
                return {"blocked": False, "matches": []}

            stats = await self._compute_rule_stats(rules, now=now)

        matches: list[dict[str, Any]] = []
        for rule in rules:
            stat = stats.get(rule["id"], {})
            limit = _safe_float(stat.get("limit_usd"), 0.0)
            spent = _safe_float(stat.get("spent_usd"), 0.0)
            if limit <= 0:
                continue
            if spent >= limit:
                matches.append({
                    "id": rule["id"],
                    "name": rule["name"],
                    "period": rule["period"],
                    "limit_usd": round(limit, 6),
                    "spent_usd": round(spent, 6),
                    "usage_percent": round(_safe_float(stat.get("usage_percent"), 0.0), 2),
                    "period_start": stat.get("period_start"),
                    "period_end": stat.get("period_end"),
                    "window_key": stat.get("window_key"),
                })

        return {"blocked": bool(matches), "matches": matches}

    @staticmethod
    def _fallback_notify_target_from_session(session: Any | None) -> list[dict[str, str]]:
        if session is None:
            return []
        adapter = str(getattr(session, "adapter", "") or "").strip()
        user_id = str(getattr(session, "user_id", "") or "").strip()
        if not adapter or not user_id:
            return []
        if adapter in {"web", "cli"}:
            return []
        return [{"adapter": adapter, "user_id": user_id}]

    @staticmethod
    def _build_notify_message(rule: dict[str, Any], stat: dict[str, Any]) -> str:
        models = rule.get("models", [])
        model_scope = ", ".join(models) if models else "全部模型"
        return (
            f"[預算提醒] {rule.get('name', rule.get('id', 'rule'))}\n"
            f"期間：{rule.get('period')}（{stat.get('period_start')} ~ {stat.get('period_end')}）\n"
            f"使用量：${_safe_float(stat.get('spent_usd'), 0.0):.4f} / "
            f"${_safe_float(stat.get('limit_usd'), 0.0):.4f} "
            f"（{_safe_float(stat.get('usage_percent'), 0.0):.2f}%）\n"
            f"模型：{model_scope}\n"
            f"提醒門檻：{_safe_float(rule.get('notify_percent'), 80.0):.2f}%"
        )

    async def check_and_notify(
        self,
        model: str,
        session: Any | None = None,
        adapter_manager: Any | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Send budget notifications when notify thresholds are crossed."""
        if adapter_manager is None:
            return {"sent": 0, "triggered": []}

        async with self._lock:
            all_rules = self._load_rules_unlocked()
            rules = [
                r for r in all_rules
                if r.get("enabled")
                and r.get("action") == "notify"
                and self._matches_model(model, r.get("models", []))
                and _safe_float(r.get("limit_usd"), 0.0) > 0
            ]
            if not rules:
                return {"sent": 0, "triggered": []}

            stats = await self._compute_rule_stats(rules, now=now)
            state = self._load_state_unlocked()
            notified = state.get("notified", {})
            if not isinstance(notified, dict):
                notified = {}

            sent_total = 0
            triggered: list[dict[str, Any]] = []
            now_iso = datetime.now(timezone.utc).isoformat()

            for rule in rules:
                stat = stats.get(rule["id"], {})
                usage_percent = _safe_float(stat.get("usage_percent"), 0.0)
                threshold = _clamp(_safe_float(rule.get("notify_percent"), 80.0), 1.0, 100.0)
                if usage_percent < threshold:
                    continue

                dedupe_key = f"{rule['id']}|{stat.get('window_key', '')}|{threshold:.2f}"
                if dedupe_key in notified:
                    continue

                targets = list(rule.get("notify_targets", []))
                if not targets:
                    targets = self._fallback_notify_target_from_session(session)
                if not targets:
                    continue

                message = self._build_notify_message(rule, stat)
                sent_for_rule = 0
                for target in targets:
                    adapter_name = str(target.get("adapter", "")).strip()
                    user_id = str(target.get("user_id", "")).strip()
                    if not adapter_name or not user_id:
                        continue
                    try:
                        ok = await adapter_manager.send_notification(adapter_name, user_id, message)
                    except Exception:
                        ok = False
                    if ok:
                        sent_for_rule += 1

                if sent_for_rule > 0:
                    notified[dedupe_key] = now_iso
                    sent_total += sent_for_rule
                    triggered.append({
                        "id": rule["id"],
                        "name": rule["name"],
                        "period": rule["period"],
                        "usage_percent": round(usage_percent, 2),
                        "spent_usd": round(_safe_float(stat.get("spent_usd"), 0.0), 6),
                        "limit_usd": round(_safe_float(stat.get("limit_usd"), 0.0), 6),
                        "sent_targets": sent_for_rule,
                    })

            state["notified"] = self._prune_state_notified(
                notified,
                valid_rule_ids={r["id"] for r in all_rules},
            )
            self._save_state_unlocked(state)

            return {"sent": sent_total, "triggered": triggered}


_budget_manager_singleton: BudgetManager | None = None


def get_budget_manager(db_path: str | None = None) -> BudgetManager:
    """Get singleton budget manager (shared by Web API + engine)."""
    global _budget_manager_singleton
    if _budget_manager_singleton is None:
        _budget_manager_singleton = BudgetManager(db_path=db_path)
    elif db_path and _budget_manager_singleton.db_path != db_path:
        _budget_manager_singleton = BudgetManager(db_path=db_path)
    return _budget_manager_singleton


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

        # Suggestion: Heavily used tool -> create Skill
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
