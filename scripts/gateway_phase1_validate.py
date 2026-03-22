"""Phase 1 Lite Gateway validation helper (CLI wrapper)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_kuro_home
from src.core.security.gateway_validation import run_gateway_phase1_validation


def _print_text(result: dict[str, Any]) -> None:
    print("=== OpenKuro Lite Gateway Phase 1 Validation ===")
    print(f"Window: last {result.get('days', 0)} day(s)")
    print(f"DB: {result.get('db_path', '-')}")
    if result.get("error"):
        print(f"Error: {result.get('error')}")
        return
    print("")
    print("[Config Snapshot]")
    for k, v in (result.get("config_snapshot") or {}).items():
        print(f"- {k}: {v}")
    print("")
    print("[Gateway Route Counts]")
    route_counts = result.get("route_counts") or {}
    if route_counts:
        for k in sorted(route_counts.keys()):
            print(f"- {k}: {route_counts[k]}")
    else:
        print("- (no gateway route logs)")
    print("")
    print("[Top Reasons]")
    reason_counts = result.get("reason_counts") or {}
    if reason_counts:
        top = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:8]
        for name, cnt in top:
            print(f"- {name}: {cnt}")
    else:
        print("- (no reason data)")
    print("")
    print("[Network Tool Metrics]")
    print(f"- calls: {result.get('network_tool_calls', 0)}")
    print(f"- denied: {result.get('network_tool_denied', 0)}")
    print(f"- deny_rate: {float(result.get('network_tool_deny_rate', 0.0)) * 100:.2f}%")
    print(f"- p95_latency_ms(current): {result.get('network_tool_latency_p95_ms')}")
    print(f"- p95_latency_ms(previous): {result.get('network_tool_latency_prev_p95_ms')}")
    delta_rate = result.get("network_tool_latency_p95_delta_rate")
    if delta_rate is None:
        print("- p95_latency_delta: n/a")
    else:
        print(f"- p95_latency_delta: {float(delta_rate) * 100:.2f}%")
    false_block = result.get("false_block") or {}
    print(
        "- false_block_rate: "
        + f"{float(false_block.get('false_block_rate', 0.0)) * 100:.2f}% "
        + f"(assessed={int(false_block.get('assessed_denied', 0))}, "
        + f"recovered={int(false_block.get('recovered_within_window', 0))})"
    )
    token_growth = result.get("token_growth_rate")
    if token_growth is None:
        print("- token_growth_rate: n/a")
    else:
        print(f"- token_growth_rate: {float(token_growth) * 100:.2f}%")
    print(f"- token_total_current: {int(result.get('token_total_current', 0))}")
    print(f"- token_total_previous: {int(result.get('token_total_previous', 0))}")
    print("")
    print("[Repair Metrics]")
    print(f"- diagnose_and_repair runs: {result.get('repair_runs', 0)}")
    print(f"- diagnose_and_repair ok runs: {result.get('repair_ok_runs', 0)}")
    print("")
    print("[Checks]")
    for chk in (result.get("checks") or []):
        name = str(chk.get("name") or "")
        mark = "PASS" if bool(chk.get("ok")) else "FAIL"
        if name == "gateway_config_enabled":
            detail = f"value={chk.get('value')} expected={chk.get('expected')}"
        elif name == "shadow_sample_size":
            detail = f"value={chk.get('value')} expected_min={chk.get('expected_min')}"
        elif name == "network_deny_rate":
            detail = (
                f"value={float(chk.get('value', 0.0)) * 100:.2f}% "
                f"expected_max={float(chk.get('expected_max', 0.0)) * 100:.2f}% "
                f"(denied={chk.get('denied', 0)}, total={chk.get('total', 0)})"
            )
        elif name == "gateway_route_observed":
            detail = f"gateway={chk.get('gateway', 0)} shadow={chk.get('shadow', 0)} direct={chk.get('direct', 0)}"
        elif name == "false_block_rate":
            detail = (
                f"value={float(chk.get('value', 0.0)) * 100:.2f}% "
                f"expected_max={float(chk.get('expected_max', 0.0)) * 100:.2f}% "
                f"(assessed={chk.get('assessed_denied', 0)}, recovered={chk.get('recovered', 0)})"
            )
        elif name == "latency_p95_delta":
            if chk.get("skipped"):
                detail = "skipped (insufficient baseline)"
            else:
                detail = (
                    f"value={float(chk.get('value', 0.0)) * 100:.2f}% "
                    f"expected_max={float(chk.get('expected_max', 0.0)) * 100:.2f}% "
                    f"(current={chk.get('current_p95_ms')}, previous={chk.get('previous_p95_ms')})"
                )
        elif name == "token_cost_growth":
            if chk.get("skipped"):
                detail = "skipped (no previous token baseline)"
            else:
                detail = (
                    f"value={float(chk.get('value', 0.0)) * 100:.2f}% "
                    f"expected_max={float(chk.get('expected_max', 0.0)) * 100:.2f}% "
                    f"(current={chk.get('current_total_tokens', 0)}, previous={chk.get('previous_total_tokens', 0)})"
                )
        else:
            detail = "-"
        print(f"- [{mark}] {name}: {detail}")
    cutover = result.get("cutover") or {}
    print("")
    print("[Cutover Recommendation]")
    print(f"- current_mode: {cutover.get('current_mode', '-')}")
    print(f"- ready_for_enforce: {bool(cutover.get('ready_for_enforce', False))}")
    print(f"- failed_checks: {', '.join(cutover.get('failed_checks', [])) or '-'}")
    print(f"- next_step: {cutover.get('recommended_next_step', '-')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Lite Gateway Phase 1 readiness.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    parser.add_argument(
        "--db",
        type=str,
        default="",
        help="Path to audit.db (default: ~/.kuro/audit.db)",
    )
    parser.add_argument(
        "--min-shadow-samples",
        type=int,
        default=50,
        help="Minimum shadow samples before enforce cutover recommendation (default: 50)",
    )
    parser.add_argument(
        "--max-network-deny-rate",
        type=float,
        default=0.02,
        help="Max acceptable denied ratio for network tools (default: 0.02 = 2%%)",
    )
    parser.add_argument(
        "--max-false-block-rate",
        type=float,
        default=0.05,
        help="Max acceptable false-block heuristic ratio (default: 0.05 = 5%%)",
    )
    parser.add_argument(
        "--max-latency-p95-delta",
        type=float,
        default=0.15,
        help="Max acceptable P95 latency delta vs previous window (default: 0.15 = 15%%)",
    )
    parser.add_argument(
        "--max-token-growth-rate",
        type=float,
        default=0.30,
        help="Max acceptable token growth vs previous window (default: 0.30 = 30%%)",
    )
    parser.add_argument(
        "--false-block-lookahead-minutes",
        type=int,
        default=60,
        help="Window for false-block heuristic recovery matching (default: 60)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser() if str(args.db or "").strip() else (get_kuro_home() / "audit.db")
    result = run_gateway_phase1_validation(
        db_path=db_path,
        days=max(1, int(args.days or 7)),
        min_shadow_samples=max(1, int(args.min_shadow_samples or 50)),
        max_network_deny_rate=max(0.0, float(args.max_network_deny_rate or 0.02)),
        max_false_block_rate=max(0.0, float(args.max_false_block_rate or 0.05)),
        max_latency_p95_delta=max(0.0, float(args.max_latency_p95_delta or 0.15)),
        max_token_growth_rate=max(0.0, float(args.max_token_growth_rate or 0.30)),
        false_block_lookahead_minutes=max(1, int(args.false_block_lookahead_minutes or 60)),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_text(result)
    return 0 if not result.get("error") else 2


if __name__ == "__main__":
    raise SystemExit(main())
