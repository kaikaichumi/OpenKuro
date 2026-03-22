# Gateway Phase 7 Drill Runbook

This runbook verifies Phase 7 rollout safety with four sections:

- Baseline (Phase 1 readiness checks)
- Regression (route-reason anomaly checks)
- Load (peak-hour deny-rate checks)
- Incident readiness (rollback/runbook availability)

Script:

```bash
poetry run python scripts/gateway_phase7_drill.py --days 7 --require-enforce-mode
```

Web/API:

- Security page card: `Gateway Phase 7 Drill`
- API: `/api/security/gateway/drill`

JSON output:

```bash
poetry run python scripts/gateway_phase7_drill.py --days 7 --require-enforce-mode --json
```

Stricter run example:

```bash
poetry run python scripts/gateway_phase7_drill.py \
  --days 7 \
  --require-enforce-mode \
  --min-rollout-percent 100 \
  --min-peak-hour-calls 30 \
  --max-peak-hour-deny-rate 0.08 \
  --max-missing-proxy-route-events 0 \
  --max-invalid-route-events 0 \
  --max-direct-ratio-when-full-rollout 0.15
```

## Required Pass Conditions

- Baseline section: all checks pass (or skip-allowed checks remain skipped).
- Regression section:
  - `missing_proxy_url_events` <= threshold
  - `invalid_route_events` <= threshold
  - direct-route ratio under threshold when `enforce + rollout=100`
- Load section:
  - peak-hour sample size passes or is skipped due low traffic
  - peak-hour deny rate <= threshold
- Incident section:
  - rollback script exists
  - runbook exists

## Rollback (Immediate)

One command rollback:

```bash
poetry run python scripts/gateway_rollback.py --set-shadow-mode
```

Dry-run rollback preview:

```bash
poetry run python scripts/gateway_rollback.py --set-shadow-mode --dry-run
```

Or manual config update:

Update config:

```yaml
egress_policy:
  gateway_enabled: false
```

Then restart Kuro process/service.

## Suggested Drill Cadence

- Before first production enforce cutover.
- After any gateway proxy upgrade/config change.
- Weekly during rollout period.
