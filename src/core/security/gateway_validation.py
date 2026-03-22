"""Gateway Phase 1 validation summary utilities."""

from __future__ import annotations

import json
import re
import sqlite3
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.config import KuroConfig, load_config

_LATENCY_RE = re.compile(r"\((\d+)ms\)")
_NETWORK_TOOL_PREFIXES = ("web_", "comfyui_", "mcp_")
_NETWORK_TOOL_EXACT = {"send_message", "a2a_call_agent", "a2a_discover_peers"}
_TARGET_URL_KEYS = (
    "url",
    "target_url",
    "endpoint",
    "webhook_url",
    "image_url",
    "download_url",
    "source_url",
    "link",
)


def _is_network_tool(name: str) -> bool:
    n = str(name or "").strip()
    if not n:
        return False
    if n in _NETWORK_TOOL_EXACT:
        return True
    return n.startswith(_NETWORK_TOOL_PREFIXES)


def _parse_params(raw: str | None) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


def _parse_latency_ms(summary: str | None) -> int | None:
    text = str(summary or "")
    m = _LATENCY_RE.search(text)
    if not m:
        return None
    try:
        value = int(m.group(1))
    except Exception:
        return None
    return value if value >= 0 else None


def _parse_ts(raw: str | None) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except Exception:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    data = sorted(v for v in values if v >= 0)
    if not data:
        return None
    idx = int(round((len(data) - 1) * 0.95))
    return data[max(0, min(idx, len(data) - 1))]


def _extract_target_url(params: dict[str, Any]) -> str:
    for key in _TARGET_URL_KEYS:
        val = params.get(key)
        if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
            return val.strip()

    for key, value in params.items():
        if "url" not in str(key or "").lower():
            continue
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            return value.strip()

    return ""


def _extract_target_host(params: dict[str, Any]) -> str:
    host = str(params.get("host") or "").strip().lower()
    if host:
        return host
    domain = str(params.get("domain") or "").strip().lower()
    if domain:
        return domain
    url = _extract_target_url(params)
    if not url:
        return ""
    try:
        return str(urlparse(url).hostname or "").strip().lower()
    except Exception:
        return ""


def _read_gateway_rows(
    conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT timestamp, tool_name, parameters
            FROM audit_log
            WHERE event_type = 'security:gateway_route'
              AND timestamp >= ?
              AND timestamp < ?
            ORDER BY id DESC
            """,
            (start_iso, end_iso),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    out: list[dict[str, Any]] = []
    for ts, tool_name, params_raw in rows:
        params = _parse_params(params_raw)
        out.append(
            {
                "timestamp": str(ts or ""),
                "tool": str(params.get("tool") or tool_name or ""),
                "route": str(params.get("route") or "").strip().lower(),
                "reason": str(params.get("reason") or "").strip().lower(),
                "target": str(params.get("target") or ""),
                "host": str(params.get("host") or ""),
            }
        )
    return out


def _read_network_tool_rows(
    conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT timestamp, tool_name, approval_status, result_summary, parameters
            FROM audit_log
            WHERE event_type = 'tool_execution'
              AND timestamp >= ?
              AND timestamp < ?
            ORDER BY timestamp ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    out: list[dict[str, Any]] = []
    for ts, tool_name, approval_status, result_summary, params_raw in rows:
        name = str(tool_name or "")
        if not _is_network_tool(name):
            continue
        params = _parse_params(params_raw)
        out.append(
            {
                "timestamp": str(ts or ""),
                "dt": _parse_ts(ts),
                "tool": name,
                "approval_status": str(approval_status or "").strip().lower(),
                "result_summary": str(result_summary or ""),
                "params": params,
                "host": _extract_target_host(params),
                "latency_ms": _parse_latency_ms(result_summary),
            }
        )
    return out


def _read_token_daily(
    conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT SUBSTR(timestamp, 1, 10) AS day,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens
            FROM token_usage
            WHERE timestamp >= ?
              AND timestamp < ?
            GROUP BY day
            ORDER BY day ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "day": str(day),
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(total_tokens or 0),
        }
        for day, prompt_tokens, completion_tokens, total_tokens in rows
    ]


def _read_token_total(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> int:
    try:
        row = conn.execute(
            """
            SELECT SUM(total_tokens) AS total_tokens
            FROM token_usage
            WHERE timestamp >= ?
              AND timestamp < ?
            """,
            (start_iso, end_iso),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row:
        return 0
    return int(row[0] or 0)


def _read_repair_counts(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> tuple[int, int]:
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS runs,
                SUM(CASE WHEN LOWER(result_summary) LIKE 'ok%' THEN 1 ELSE 0 END) AS ok_runs
            FROM audit_log
            WHERE event_type = 'tool_execution'
              AND tool_name = 'diagnose_and_repair'
              AND timestamp >= ?
              AND timestamp < ?
            """,
            (start_iso, end_iso),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0, 0
    if not row:
        return 0, 0
    runs = int(row[0] or 0)
    ok_runs = int(row[1] or 0)
    return runs, ok_runs


def _summarize_daily_routes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        ts = str(row.get("timestamp") or "")
        day = ts[:10] if len(ts) >= 10 else "unknown"
        route = str(row.get("route") or "unknown")
        by_day[day][route] += 1

    out: list[dict[str, Any]] = []
    for day in sorted(by_day):
        cnt = by_day[day]
        out.append(
            {
                "day": day,
                "gateway": int(cnt.get("gateway", 0)),
                "shadow": int(cnt.get("shadow", 0)),
                "direct": int(cnt.get("direct", 0)),
            }
        )
    return out


def _estimate_false_block_rate(
    rows: list[dict[str, Any]],
    *,
    lookahead_minutes: int = 60,
) -> dict[str, Any]:
    approved_by_key: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    denied_rows: list[dict[str, Any]] = []

    for row in rows:
        tool = str(row.get("tool") or "").strip().lower()
        host = str(row.get("host") or "").strip().lower()
        dt = row.get("dt")
        if not isinstance(dt, datetime):
            continue
        status = str(row.get("approval_status") or "").strip().lower()
        key = (tool, host)
        if status == "approved" and tool and host:
            approved_by_key[key].append(dt)
        elif status == "denied":
            denied_rows.append(row)

    for key in approved_by_key:
        approved_by_key[key].sort()

    lookahead = timedelta(minutes=max(1, int(lookahead_minutes or 60)))
    assessed = 0
    recovered = 0
    unassessed = 0

    for denied in denied_rows:
        tool = str(denied.get("tool") or "").strip().lower()
        host = str(denied.get("host") or "").strip().lower()
        dt = denied.get("dt")
        if not isinstance(dt, datetime) or not tool or not host:
            unassessed += 1
            continue
        assessed += 1
        approved_list = approved_by_key.get((tool, host), [])
        if not approved_list:
            continue
        idx = bisect_left(approved_list, dt)
        if idx < len(approved_list) and approved_list[idx] - dt <= lookahead:
            recovered += 1

    rate = (float(recovered) / float(assessed)) if assessed > 0 else 0.0
    return {
        "denied_total": int(len(denied_rows)),
        "assessed_denied": int(assessed),
        "unassessed_denied": int(unassessed),
        "recovered_within_window": int(recovered),
        "false_block_rate": float(rate),
        "lookahead_minutes": int(max(1, int(lookahead_minutes or 60))),
    }


def _pct_delta(current: int | None, previous: int | None) -> tuple[int | None, float | None]:
    if current is None or previous is None:
        return None, None
    delta = int(current) - int(previous)
    if int(previous) <= 0:
        return delta, None
    return delta, float(delta) / float(previous)


def run_gateway_phase1_validation(
    db_path: str | Path,
    *,
    days: int = 7,
    min_shadow_samples: int = 50,
    max_network_deny_rate: float = 0.02,
    max_false_block_rate: float = 0.05,
    max_latency_p95_delta: float = 0.15,
    max_token_growth_rate: float = 0.30,
    false_block_lookahead_minutes: int = 60,
    config: KuroConfig | None = None,
) -> dict[str, Any]:
    """Build a phase-1 gateway validation summary from audit data."""
    cfg = config or load_config()
    egress = cfg.egress_policy
    now_utc = datetime.now(timezone.utc)
    window_days = max(1, int(days or 7))
    current_start = now_utc - timedelta(days=window_days)
    current_end = now_utc
    previous_start = current_start - timedelta(days=window_days)
    previous_end = current_start

    current_start_iso = current_start.isoformat()
    current_end_iso = current_end.isoformat()
    previous_start_iso = previous_start.isoformat()
    previous_end_iso = previous_end.isoformat()
    db_file = Path(db_path).expanduser()

    if not db_file.exists():
        return {
            "days": window_days,
            "db_path": str(db_file),
            "error": "audit_db_missing",
            "checks": [],
        }

    conn = sqlite3.connect(str(db_file))
    try:
        gateway_rows = _read_gateway_rows(conn, current_start_iso, current_end_iso)
        network_rows_current = _read_network_tool_rows(conn, current_start_iso, current_end_iso)
        network_rows_previous = _read_network_tool_rows(conn, previous_start_iso, previous_end_iso)
        token_daily = _read_token_daily(conn, current_start_iso, current_end_iso)
        token_total_current = _read_token_total(conn, current_start_iso, current_end_iso)
        token_total_previous = _read_token_total(conn, previous_start_iso, previous_end_iso)
        repair_runs, repair_ok_runs = _read_repair_counts(conn, current_start_iso, current_end_iso)
    finally:
        conn.close()

    route_counts = Counter(str(r.get("route") or "unknown") for r in gateway_rows)
    reason_counts = Counter(str(r.get("reason") or "unknown") for r in gateway_rows)
    route_daily = _summarize_daily_routes(gateway_rows)

    network_total = int(len(network_rows_current))
    network_denied = int(
        sum(1 for row in network_rows_current if str(row.get("approval_status") or "") == "denied")
    )
    network_deny_rate = (float(network_denied) / float(network_total)) if network_total > 0 else 0.0
    latency_values_current = [
        int(ms)
        for ms in (row.get("latency_ms") for row in network_rows_current)
        if isinstance(ms, int) and ms >= 0
    ]
    latency_values_previous = [
        int(ms)
        for ms in (row.get("latency_ms") for row in network_rows_previous)
        if isinstance(ms, int) and ms >= 0
    ]
    latency_p95_current = _p95(latency_values_current)
    latency_p95_previous = _p95(latency_values_previous)
    latency_delta_ms, latency_delta_rate = _pct_delta(latency_p95_current, latency_p95_previous)

    false_block = _estimate_false_block_rate(
        network_rows_current,
        lookahead_minutes=max(1, int(false_block_lookahead_minutes or 60)),
    )

    token_delta, token_growth_rate = _pct_delta(token_total_current, token_total_previous)

    config_snapshot = {
        "gateway_enabled": bool(getattr(egress, "gateway_enabled", False)),
        "gateway_mode": str(getattr(egress, "gateway_mode", "enforce")),
        "gateway_proxy_url_set": bool(str(getattr(egress, "gateway_proxy_url", "") or "").strip()),
        "gateway_bypass_domains_count": len(getattr(egress, "gateway_bypass_domains", []) or []),
        "gateway_include_private_network": bool(getattr(egress, "gateway_include_private_network", False)),
        "gateway_rollout_percent": int(max(0, min(100, int(getattr(egress, "gateway_rollout_percent", 100) or 0)))),
        "gateway_rollout_seed_set": bool(str(getattr(egress, "gateway_rollout_seed", "") or "").strip()),
    }

    latency_check_skipped = latency_p95_previous is None or latency_p95_current is None
    latency_check_ok = True if latency_check_skipped else bool(
        (latency_delta_rate or 0.0) <= float(max_latency_p95_delta)
    )
    token_growth_skipped = token_total_previous <= 0
    token_growth_ok = True if token_growth_skipped else bool(
        (token_growth_rate or 0.0) <= float(max_token_growth_rate)
    )

    checks: list[dict[str, Any]] = [
        {
            "name": "gateway_config_enabled",
            "ok": bool(config_snapshot["gateway_enabled"] and config_snapshot["gateway_proxy_url_set"]),
            "value": bool(config_snapshot["gateway_enabled"] and config_snapshot["gateway_proxy_url_set"]),
            "expected": True,
            "required_for_cutover": True,
        },
        {
            "name": "shadow_sample_size",
            "ok": int(route_counts.get("shadow", 0)) >= int(min_shadow_samples),
            "value": int(route_counts.get("shadow", 0)),
            "expected_min": int(min_shadow_samples),
            "required_for_cutover": True,
        },
        {
            "name": "network_deny_rate",
            "ok": network_deny_rate <= float(max_network_deny_rate),
            "value": float(network_deny_rate),
            "expected_max": float(max_network_deny_rate),
            "denied": int(network_denied),
            "total": int(network_total),
            "required_for_cutover": True,
        },
        {
            "name": "gateway_route_observed",
            "ok": int(route_counts.get("gateway", 0)) > 0 or int(route_counts.get("shadow", 0)) > 0,
            "gateway": int(route_counts.get("gateway", 0)),
            "shadow": int(route_counts.get("shadow", 0)),
            "direct": int(route_counts.get("direct", 0)),
            "required_for_cutover": True,
        },
        {
            "name": "false_block_rate",
            "ok": float(false_block.get("false_block_rate", 0.0)) <= float(max_false_block_rate),
            "value": float(false_block.get("false_block_rate", 0.0)),
            "expected_max": float(max_false_block_rate),
            "assessed_denied": int(false_block.get("assessed_denied", 0)),
            "recovered": int(false_block.get("recovered_within_window", 0)),
            "unassessed_denied": int(false_block.get("unassessed_denied", 0)),
            "required_for_cutover": True,
        },
        {
            "name": "latency_p95_delta",
            "ok": bool(latency_check_ok),
            "value": float(latency_delta_rate or 0.0) if latency_delta_rate is not None else None,
            "expected_max": float(max_latency_p95_delta),
            "current_p95_ms": latency_p95_current,
            "previous_p95_ms": latency_p95_previous,
            "delta_ms": latency_delta_ms,
            "skipped": bool(latency_check_skipped),
            "required_for_cutover": True,
        },
        {
            "name": "token_cost_growth",
            "ok": bool(token_growth_ok),
            "value": float(token_growth_rate or 0.0) if token_growth_rate is not None else None,
            "expected_max": float(max_token_growth_rate),
            "current_total_tokens": int(token_total_current),
            "previous_total_tokens": int(token_total_previous),
            "delta_tokens": int(token_delta or 0),
            "skipped": bool(token_growth_skipped),
            "required_for_cutover": True,
        },
    ]

    required_check_names = [
        str(chk.get("name") or "")
        for chk in checks
        if bool(chk.get("required_for_cutover", True))
    ]
    failed_checks = [
        str(chk.get("name") or "")
        for chk in checks
        if bool(chk.get("required_for_cutover", True)) and not bool(chk.get("ok"))
    ]
    mode = str(config_snapshot.get("gateway_mode") or "enforce").strip().lower()

    ready_for_enforce = bool(config_snapshot["gateway_enabled"]) and len(failed_checks) == 0
    if mode == "shadow":
        recommended_next_step = (
            "switch_to_enforce"
            if ready_for_enforce
            else (
                "run_shadow_burn_in"
                if int(route_counts.get("shadow", 0)) < int(min_shadow_samples)
                else "review_failed_checks"
            )
        )
    elif mode == "enforce":
        recommended_next_step = "already_enforce_monitoring" if ready_for_enforce else "review_failed_checks"
    else:
        recommended_next_step = "review_failed_checks"

    return {
        "days": window_days,
        "db_path": str(db_file),
        "time_window": {
            "current": {"start": current_start_iso, "end": current_end_iso},
            "previous": {"start": previous_start_iso, "end": previous_end_iso},
        },
        "config_snapshot": config_snapshot,
        "route_counts": {k: int(v) for k, v in route_counts.items()},
        "reason_counts": {k: int(v) for k, v in reason_counts.items()},
        "route_daily": route_daily,
        "network_tool_calls": int(network_total),
        "network_tool_denied": int(network_denied),
        "network_tool_deny_rate": float(network_deny_rate),
        "network_tool_latency_p95_ms": latency_p95_current,
        "network_tool_latency_prev_p95_ms": latency_p95_previous,
        "network_tool_latency_p95_delta_ms": latency_delta_ms,
        "network_tool_latency_p95_delta_rate": latency_delta_rate,
        "false_block": false_block,
        "token_daily": token_daily,
        "token_total_current": int(token_total_current),
        "token_total_previous": int(token_total_previous),
        "token_growth_rate": token_growth_rate,
        "token_growth_delta": token_delta,
        "repair_runs": int(repair_runs),
        "repair_ok_runs": int(repair_ok_runs),
        "checks": checks,
        "cutover": {
            "ready_for_enforce": bool(ready_for_enforce),
            "required_checks": required_check_names,
            "failed_checks": failed_checks,
            "recommended_next_step": recommended_next_step,
            "current_mode": mode,
        },
    }
