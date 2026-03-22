"""Phase 7 drill helper: baseline + regression + load + incident readiness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.security.gateway_drill import run_gateway_phase7_drill_suite


def _print_text(summary: dict[str, Any]) -> None:
    print("=== OpenKuro Gateway Phase 7 Drill ===")
    print(f"DB: {summary.get('db_path', '-')}")
    window = summary.get("window") or {}
    print(f"Window: {window.get('start', '-')} ~ {window.get('end', '-')}")
    if summary.get("status") == "error":
        print(f"Error: {summary.get('error')}")
        return

    print("")
    for section in (summary.get("sections") or []):
        name = str(section.get("name") or "section")
        mark = "PASS" if bool(section.get("passed", False)) else "FAIL"
        print(f"[{name}] {mark}")
        for chk in (section.get("checks") or []):
            cmark = "PASS" if bool(chk.get("ok", False)) else "FAIL"
            if bool(chk.get("skipped", False)):
                cmark = "SKIP"
            detail = str(chk.get("detail") or "-")
            print(f"- [{cmark}] {chk.get('name')}: {detail}")
        if name == "load":
            peak = section.get("peak_hour") or {}
            print(
                "- peak_hour: "
                + f"{peak.get('hour_bucket', '-')}, "
                + f"calls={int(peak.get('total_calls', 0) or 0)}, "
                + f"denied={int(peak.get('denied_calls', 0) or 0)}"
            )
        if name == "incident":
            print(f"- rollback: {section.get('rollback_command', '-')}")
        print("")

    print(f"[Result] passed={bool(summary.get('passed', False))}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Gateway Phase 7 drill suite.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    parser.add_argument("--db", type=str, default="", help="Path to audit.db (default: ~/.kuro/audit.db)")
    parser.add_argument(
        "--require-enforce-mode",
        action="store_true",
        help="Require gateway_mode=enforce for baseline checks",
    )
    parser.add_argument(
        "--min-rollout-percent",
        type=int,
        default=100,
        help="Minimum rollout percent required for baseline checks (default: 100)",
    )
    parser.add_argument(
        "--min-peak-hour-calls",
        type=int,
        default=20,
        help="Load drill minimum calls in peak hour to enforce deny-rate check (default: 20)",
    )
    parser.add_argument(
        "--max-peak-hour-deny-rate",
        type=float,
        default=0.10,
        help="Load drill max deny ratio allowed in peak hour (default: 0.10)",
    )
    parser.add_argument(
        "--max-missing-proxy-route-events",
        type=int,
        default=0,
        help="Regression drill max missing_proxy_url route reasons allowed (default: 0)",
    )
    parser.add_argument(
        "--max-invalid-route-events",
        type=int,
        default=0,
        help="Regression drill max invalid_url/missing_host route reasons allowed (default: 0)",
    )
    parser.add_argument(
        "--max-direct-ratio-when-full-rollout",
        type=float,
        default=0.20,
        help="Regression drill max direct route ratio in enforce+100%% rollout (default: 0.20)",
    )
    parser.add_argument(
        "--incident-deny-rate-threshold",
        type=float,
        default=0.10,
        help="Incident signal threshold for deny ratio (default: 0.10)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    summary = run_gateway_phase7_drill_suite(
        db_path=(Path(args.db).expanduser() if str(args.db or "").strip() else None),
        days=max(1, int(args.days or 7)),
        require_enforce_mode=bool(args.require_enforce_mode),
        min_rollout_percent=max(0, min(100, int(args.min_rollout_percent or 0))),
        min_peak_hour_calls=max(1, int(args.min_peak_hour_calls or 1)),
        max_peak_hour_deny_rate=max(0.0, float(args.max_peak_hour_deny_rate or 0.0)),
        max_missing_proxy_route_events=max(0, int(args.max_missing_proxy_route_events or 0)),
        max_invalid_route_events=max(0, int(args.max_invalid_route_events or 0)),
        max_direct_ratio_when_full_rollout=max(
            0.0,
            float(args.max_direct_ratio_when_full_rollout or 0.0),
        ),
        incident_deny_rate_threshold=max(0.0, float(args.incident_deny_rate_threshold or 0.0)),
    )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_text(summary)

    if summary.get("status") == "error":
        return 2
    return 0 if bool(summary.get("passed", False)) else 2


if __name__ == "__main__":
    raise SystemExit(main())
