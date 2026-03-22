"""One-command Gateway rollback helper.

Sets `egress_policy.gateway_enabled=false` in config.yaml.
Optionally also sets `gateway_mode=shadow` to keep observations without enforce routing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_kuro_home


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def _save_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Disable Gateway enforce routing immediately.")
    parser.add_argument(
        "--config",
        type=str,
        default="",
        help="Path to config.yaml (default: ~/.kuro/config.yaml)",
    )
    parser.add_argument(
        "--set-shadow-mode",
        action="store_true",
        help="Also set egress_policy.gateway_mode=shadow",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview rollback changes without writing config",
    )
    args = parser.parse_args()

    cfg_path = (
        Path(args.config).expanduser()
        if str(args.config or "").strip()
        else (get_kuro_home() / "config.yaml")
    )
    cfg = _load_yaml(cfg_path)
    egress = cfg.get("egress_policy")
    if not isinstance(egress, dict):
        egress = {}
        cfg["egress_policy"] = egress

    egress["gateway_enabled"] = False
    if bool(args.set_shadow_mode):
        egress["gateway_mode"] = "shadow"

    if not bool(args.dry_run):
        _save_yaml(cfg_path, cfg)

    if bool(args.dry_run):
        print("Gateway rollback dry-run.")
        print(f"Target config: {cfg_path}")
    else:
        print("Gateway rollback applied.")
        print(f"Updated: {cfg_path}")
    print("Set egress_policy.gateway_enabled = false")
    if bool(args.set_shadow_mode):
        print("Set egress_policy.gateway_mode = shadow")
    if bool(args.dry_run):
        print("No file changes written.")
    else:
        print("Next step: restart Kuro process/service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
