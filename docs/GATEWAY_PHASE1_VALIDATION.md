# Gateway Phase 1 Validation Checklist

This checklist verifies Lite Gateway rollout safety before switching from `shadow` to `enforce`.

Web UI shortcut:

- Open `/security` and check the **Gateway Phase 1 Health** panel.
- API: `/api/security/gateway/validation`

## 1) Prepare Config

Set these values in `~/.kuro/config.yaml`:

```yaml
egress_policy:
  gateway_enabled: true
  gateway_mode: "shadow"
  gateway_proxy_url: "http://<your-gateway-host>:<port>"
  gateway_bypass_domains:
    - "localhost"
  gateway_include_private_network: false
```

## 2) Collect Shadow Traffic

Run normal workloads for at least 1-3 days:

- `web_*` tools
- `web_crawl_batch`
- ComfyUI tools
- MCP tools (if enabled)
- Discord attachment/image fetch flows

## 3) Run Validation Script

```bash
poetry run python scripts/gateway_phase1_validate.py --days 7
```

Optional JSON output:

```bash
poetry run python scripts/gateway_phase1_validate.py --days 7 --json
```

## 4) Review Key Signals

Target thresholds for Phase 1 cutover:

- `shadow_sample_size`: PASS (default threshold `>= 50`)
- `network_deny_rate`: PASS (default threshold `<= 2%`)
- `false_block_rate`: PASS (default threshold `<= 5%`, heuristic)
- `latency_p95_delta`: PASS (default threshold `<= 15%` vs previous window)
- `token_cost_growth`: PASS (default threshold `<= 30%` vs previous window)
- `gateway_route_observed`: PASS
- Config snapshot shows:
  - `gateway_enabled: true`
  - `gateway_proxy_url_set: true`
- `cutover.ready_for_enforce: true`

Notes:

- `network_deny_rate` is a proxy signal for risk; still manually review denied events.
- `false_block_rate` is estimated by checking denied attempts that later succeed on the same tool/host.
- `p95_latency_ms` and `token_growth_rate` are compared against a previous window of equal length.
- Check `token_daily` and `route_daily` trends for anomalies.

Optional stricter validation:

```bash
poetry run python scripts/gateway_phase1_validate.py \
  --days 7 \
  --min-shadow-samples 80 \
  --max-network-deny-rate 0.02 \
  --max-false-block-rate 0.03 \
  --max-latency-p95-delta 0.12 \
  --max-token-growth-rate 0.25
```

## 5) Switch to Enforce

After validation passes:

```yaml
egress_policy:
  gateway_mode: "enforce"
```

Then restart Kuro and re-run validation:

```bash
poetry run python scripts/gateway_phase1_validate.py --days 1
```

Expected:

- `gateway` route count increases.
- `shadow` route count becomes near zero.
- No major spike in denies/errors.

## 6) Rollback Plan

Immediate rollback:

```yaml
egress_policy:
  gateway_enabled: false
```

Then restart Kuro.
