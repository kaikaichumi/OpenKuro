"""Gateway Phase 7 drill suite helpers.

Combines baseline validation with regression/load/incident readiness checks.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import KuroConfig, get_kuro_home, load_config
from src.core.security.gateway_validation import run_gateway_phase1_validation

_NETWORK_TOOL_SQL_FILTER = (
    "(tool_name LIKE 'web_%' OR tool_name LIKE 'comfyui_%' OR tool_name LIKE 'mcp_%' "
    "OR tool_name IN ('send_message','a2a_call_agent','a2a_discover_peers'))"
)


def _find_check(result: dict[str, Any], name: str) -> dict[str, Any]:
    for chk in (result.get("checks") or []):
        if str(chk.get("name") or "") == name:
            return chk
    return {}


def _to_bool(value: Any) -> bool:
    return bool(value)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _evaluate_baseline(
    result: dict[str, Any],
    *,
    require_enforce_mode: bool,
    min_rollout_percent: int,
) -> dict[str, Any]:
    cfg = result.get("config_snapshot") or {}
    route_counts = result.get("route_counts") or {}
    mode = str(cfg.get("gateway_mode") or "").strip().lower()
    rollout_percent = _to_int(cfg.get("gateway_rollout_percent", 100), 100)

    checks: list[dict[str, Any]] = []
    cutover = result.get("cutover") or {}
    phase1_required_ok = _to_bool(cutover.get("ready_for_enforce", False))
    failed_checks = {
        str(v).strip().lower()
        for v in (cutover.get("failed_checks") or [])
        if str(v).strip()
    }
    if not phase1_required_ok and mode == "enforce":
        # In steady-state enforce mode, shadow sample count can naturally decay to zero.
        phase1_required_ok = failed_checks.issubset({"shadow_sample_size"})
    checks.append(
        {
            "name": "phase1_ready_for_enforce",
            "ok": phase1_required_ok,
            "detail": f"ready_for_enforce={phase1_required_ok}",
        }
    )

    mode_ok = (mode == "enforce") if require_enforce_mode else (mode in {"shadow", "enforce"})
    checks.append(
        {
            "name": "gateway_mode",
            "ok": mode_ok,
            "detail": f"mode={mode}, require_enforce={require_enforce_mode}",
        }
    )

    checks.append(
        {
            "name": "rollout_percent",
            "ok": rollout_percent >= int(min_rollout_percent),
            "detail": f"rollout_percent={rollout_percent}, min_required={int(min_rollout_percent)}",
        }
    )

    gateway_count = _to_int(route_counts.get("gateway", 0), 0)
    shadow_count = _to_int(route_counts.get("shadow", 0), 0)
    direct_count = _to_int(route_counts.get("direct", 0), 0)
    route_observed_ok = gateway_count > 0
    if mode == "shadow" and not require_enforce_mode:
        route_observed_ok = gateway_count > 0 or shadow_count > 0
    checks.append(
        {
            "name": "gateway_route_observed",
            "ok": route_observed_ok,
            "detail": f"gateway={gateway_count}, shadow={shadow_count}, direct={direct_count}",
        }
    )

    for name in ("network_deny_rate", "false_block_rate", "latency_p95_delta", "token_cost_growth"):
        chk = _find_check(result, name)
        detail: str
        if _to_bool(chk.get("skipped", False)):
            detail = "skipped"
        elif name in {"network_deny_rate", "false_block_rate"}:
            detail = (
                f"value={_to_float(chk.get('value', 0.0)) * 100:.2f}%, "
                f"expected_max={_to_float(chk.get('expected_max', 0.0)) * 100:.2f}%"
            )
        else:
            detail = f"value={_to_float(chk.get('value', 0.0)) * 100:.2f}%"
        checks.append(
            {
                "name": name,
                "ok": _to_bool(chk.get("ok", False)),
                "detail": detail,
                "skipped": _to_bool(chk.get("skipped", False)),
            }
        )

    passed = all(_to_bool(chk.get("ok", False)) for chk in checks)
    return {
        "name": "baseline",
        "passed": passed,
        "checks": checks,
    }


def _read_gateway_window_counts(
    db_file: Path,
    *,
    start_iso: str,
    end_iso: str,
) -> tuple[Counter[str], Counter[str]]:
    route_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()

    conn = sqlite3.connect(str(db_file))
    try:
        rows = conn.execute(
            """
            SELECT parameters
            FROM audit_log
            WHERE event_type = 'security:gateway_route'
              AND timestamp >= ?
              AND timestamp < ?
            """,
            (start_iso, end_iso),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()

    for (params_raw,) in rows:
        params: dict[str, Any] = {}
        try:
            parsed = json.loads(str(params_raw or "{}"))
            if isinstance(parsed, dict):
                params = parsed
        except Exception:
            params = {}
        route = str(params.get("route") or "").strip().lower() or "unknown"
        reason = str(params.get("reason") or "").strip().lower() or "unknown"
        route_counts[route] += 1
        reason_counts[reason] += 1

    return route_counts, reason_counts


def _query_peak_hour_network_stats(
    db_file: Path,
    *,
    start_iso: str,
    end_iso: str,
) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_file))
    try:
        row = conn.execute(
            f"""
            SELECT SUBSTR(timestamp, 1, 13) AS hour_bucket,
                   COUNT(*) AS total_calls,
                   SUM(CASE WHEN LOWER(approval_status) = 'denied' THEN 1 ELSE 0 END) AS denied_calls
            FROM audit_log
            WHERE event_type = 'tool_execution'
              AND timestamp >= ?
              AND timestamp < ?
              AND {_NETWORK_TOOL_SQL_FILTER}
            GROUP BY hour_bucket
            ORDER BY total_calls DESC, hour_bucket DESC
            LIMIT 1
            """,
            (start_iso, end_iso),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()

    if not row:
        return {
            "hour_bucket": "",
            "total_calls": 0,
            "denied_calls": 0,
            "deny_rate": 0.0,
        }

    total_calls = _to_int(row[1], 0)
    denied_calls = _to_int(row[2], 0)
    deny_rate = (float(denied_calls) / float(total_calls)) if total_calls > 0 else 0.0
    return {
        "hour_bucket": str(row[0] or ""),
        "total_calls": total_calls,
        "denied_calls": denied_calls,
        "deny_rate": deny_rate,
    }


def _evaluate_regression(
    result: dict[str, Any],
    *,
    reason_counts: Counter[str],
    max_missing_proxy_route_events: int,
    max_invalid_route_events: int,
    max_direct_ratio_when_full_rollout: float,
) -> dict[str, Any]:
    cfg = result.get("config_snapshot") or {}
    mode = str(cfg.get("gateway_mode") or "").strip().lower()
    rollout_percent = _to_int(cfg.get("gateway_rollout_percent", 100), 100)
    route_counts = Counter(result.get("route_counts") or {})
    gateway = _to_int(route_counts.get("gateway", 0), 0)
    direct = _to_int(route_counts.get("direct", 0), 0)
    shadow = _to_int(route_counts.get("shadow", 0), 0)
    total = gateway + direct + shadow

    checks: list[dict[str, Any]] = []

    missing_proxy_events = _to_int(reason_counts.get("missing_proxy_url", 0), 0)
    checks.append(
        {
            "name": "missing_proxy_url_events",
            "ok": missing_proxy_events <= int(max_missing_proxy_route_events),
            "detail": (
                f"value={missing_proxy_events}, max_allowed={int(max_missing_proxy_route_events)}"
            ),
        }
    )

    invalid_route_events = _to_int(reason_counts.get("invalid_url", 0), 0) + _to_int(
        reason_counts.get("missing_host", 0),
        0,
    )
    checks.append(
        {
            "name": "invalid_route_events",
            "ok": invalid_route_events <= int(max_invalid_route_events),
            "detail": (
                f"value={invalid_route_events}, max_allowed={int(max_invalid_route_events)}"
            ),
        }
    )

    if mode == "enforce" and rollout_percent >= 100 and total > 0:
        direct_ratio = float(direct) / float(total)
        checks.append(
            {
                "name": "direct_ratio_full_rollout",
                "ok": direct_ratio <= float(max_direct_ratio_when_full_rollout),
                "detail": (
                    f"value={direct_ratio * 100:.2f}%, "
                    f"max_allowed={float(max_direct_ratio_when_full_rollout) * 100:.2f}%"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "direct_ratio_full_rollout",
                "ok": True,
                "detail": "skipped (requires enforce + full rollout + route samples)",
                "skipped": True,
            }
        )

    passed = all(_to_bool(chk.get("ok", False)) for chk in checks)
    return {
        "name": "regression",
        "passed": passed,
        "checks": checks,
    }


def _evaluate_load(
    *,
    peak_hour: dict[str, Any],
    min_peak_hour_calls: int,
    max_peak_hour_deny_rate: float,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    total_calls = _to_int(peak_hour.get("total_calls", 0), 0)
    denied_calls = _to_int(peak_hour.get("denied_calls", 0), 0)
    deny_rate = _to_float(peak_hour.get("deny_rate", 0.0), 0.0)

    if total_calls < int(min_peak_hour_calls):
        checks.append(
            {
                "name": "peak_hour_sample_size",
                "ok": True,
                "detail": f"skipped (total_calls={total_calls}, min_required={int(min_peak_hour_calls)})",
                "skipped": True,
            }
        )
        checks.append(
            {
                "name": "peak_hour_deny_rate",
                "ok": True,
                "detail": "skipped (insufficient peak sample size)",
                "skipped": True,
            }
        )
    else:
        checks.append(
            {
                "name": "peak_hour_sample_size",
                "ok": True,
                "detail": f"value={total_calls}, min_required={int(min_peak_hour_calls)}",
            }
        )
        checks.append(
            {
                "name": "peak_hour_deny_rate",
                "ok": deny_rate <= float(max_peak_hour_deny_rate),
                "detail": (
                    f"value={deny_rate * 100:.2f}% "
                    f"(denied={denied_calls}, total={total_calls}), "
                    f"max_allowed={float(max_peak_hour_deny_rate) * 100:.2f}%"
                ),
            }
        )

    passed = all(_to_bool(chk.get("ok", False)) for chk in checks)
    return {
        "name": "load",
        "passed": passed,
        "checks": checks,
        "peak_hour": peak_hour,
    }


def _evaluate_incident(
    *,
    result: dict[str, Any],
    reason_counts: Counter[str],
    incident_deny_rate_threshold: float,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    deny_rate = _to_float(result.get("network_tool_deny_rate", 0.0), 0.0)
    indicator_count = (
        _to_int(reason_counts.get("missing_proxy_url", 0), 0)
        + _to_int(reason_counts.get("invalid_url", 0), 0)
        + _to_int(reason_counts.get("missing_host", 0), 0)
    )
    incident_signal = deny_rate >= float(incident_deny_rate_threshold) or indicator_count > 0
    checks.append(
        {
            "name": "incident_signal_detected",
            "ok": True,
            "detail": (
                f"signal={'yes' if incident_signal else 'no'} "
                f"(deny_rate={deny_rate * 100:.2f}%, route_anomalies={indicator_count})"
            ),
            "signal": incident_signal,
        }
    )

    rollback_script = Path("scripts/gateway_rollback.py")
    checks.append(
        {
            "name": "rollback_script_available",
            "ok": rollback_script.is_file(),
            "detail": str(rollback_script),
        }
    )

    drill_doc = Path("docs/GATEWAY_PHASE7_DRILL.md")
    checks.append(
        {
            "name": "incident_runbook_available",
            "ok": drill_doc.is_file(),
            "detail": str(drill_doc),
        }
    )

    passed = all(_to_bool(chk.get("ok", False)) for chk in checks)
    return {
        "name": "incident",
        "passed": passed,
        "checks": checks,
        "rollback_command": "poetry run python scripts/gateway_rollback.py --set-shadow-mode",
    }


def run_gateway_phase7_drill_suite(
    *,
    db_path: str | Path | None = None,
    days: int = 7,
    require_enforce_mode: bool = False,
    min_rollout_percent: int = 100,
    min_peak_hour_calls: int = 20,
    max_peak_hour_deny_rate: float = 0.10,
    max_missing_proxy_route_events: int = 0,
    max_invalid_route_events: int = 0,
    max_direct_ratio_when_full_rollout: float = 0.20,
    incident_deny_rate_threshold: float = 0.10,
    config: KuroConfig | None = None,
) -> dict[str, Any]:
    """Run Phase 7 drill suite and return structured summary."""
    cfg = config or load_config()
    db_file = (
        Path(db_path).expanduser()
        if db_path is not None and str(db_path).strip()
        else (get_kuro_home() / "audit.db")
    )
    window_days = max(1, int(days or 7))
    now_utc = datetime.now(timezone.utc)
    start_iso = (now_utc - timedelta(days=window_days)).isoformat()
    end_iso = now_utc.isoformat()

    validation = run_gateway_phase1_validation(
        db_path=db_file,
        days=window_days,
        config=cfg,
    )
    if validation.get("error"):
        return {
            "status": "error",
            "error": validation.get("error"),
            "validation": validation,
            "sections": [],
            "passed": False,
            "days": window_days,
            "db_path": str(db_file),
        }

    route_counts_window, reason_counts_window = _read_gateway_window_counts(
        db_file,
        start_iso=start_iso,
        end_iso=end_iso,
    )
    peak_hour = _query_peak_hour_network_stats(
        db_file,
        start_iso=start_iso,
        end_iso=end_iso,
    )

    baseline = _evaluate_baseline(
        validation,
        require_enforce_mode=require_enforce_mode,
        min_rollout_percent=max(0, min(100, int(min_rollout_percent or 0))),
    )
    regression = _evaluate_regression(
        validation,
        reason_counts=reason_counts_window,
        max_missing_proxy_route_events=max(0, int(max_missing_proxy_route_events or 0)),
        max_invalid_route_events=max(0, int(max_invalid_route_events or 0)),
        max_direct_ratio_when_full_rollout=max(0.0, float(max_direct_ratio_when_full_rollout or 0.0)),
    )
    load = _evaluate_load(
        peak_hour=peak_hour,
        min_peak_hour_calls=max(1, int(min_peak_hour_calls or 1)),
        max_peak_hour_deny_rate=max(0.0, float(max_peak_hour_deny_rate or 0.0)),
    )
    incident = _evaluate_incident(
        result=validation,
        reason_counts=reason_counts_window,
        incident_deny_rate_threshold=max(0.0, float(incident_deny_rate_threshold or 0.0)),
    )
    sections = [baseline, regression, load, incident]
    passed = all(_to_bool(sec.get("passed", False)) for sec in sections)

    return {
        "status": "ok",
        "passed": passed,
        "sections": sections,
        "validation": validation,
        "window": {
            "start": start_iso,
            "end": end_iso,
            "days": window_days,
        },
        "db_path": str(db_file),
        "route_counts_window": dict(route_counts_window),
        "reason_counts_window": dict(reason_counts_window),
        "peak_hour": peak_hour,
    }
