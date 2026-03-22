# OpenKuro Gateway Security Roadmap

This roadmap defines a staged path from the current in-process security model
to a stronger gateway-centered architecture, while keeping current workflows
stable (Discord/Web/CLI, local models, MCP, ComfyUI, scheduler).

## Goals

1. Keep user experience fast and stable.
2. Add centralized outbound control and auditability.
3. Reduce blast radius of tool/model credential misuse.
4. Allow gradual rollout with quick rollback.

## Current Status

- Date: 2026-03-22
- Progress: Phase 1 complete, Phase 2 complete, Phase 3 complete, Phase 4 complete (runtime hardening), Phase 5 baseline complete, Phase 6 baseline complete, Phase 7 complete
- Landed in this iteration:
  - Added Lite Gateway proxy config fields in `egress_policy`.
  - Added proxy resolution logic in `EgressBroker`.
  - Added `gateway_mode` (`shadow` / `enforce`) and route decision logging.
  - Routed web/comfyui/discord outbound paths through gateway policy.
  - Added model-request proxy env sync for enforce mode.
  - Added MCP stdio subprocess proxy env routing in enforce mode.
  - Added gateway route logs in Security Web UI + persisted audit query path.
  - Added Phase 1 validation script + checklist documentation.
  - Added Security Web UI "Gateway Phase 1 Health" panel and validation API.
  - Added Phase 1 baseline metrics for deny-rate / false-block-rate / p95 delta / token growth.
  - Added cutover readiness output (`ready_for_enforce`) and recommended next-step hints.
  - Added Capability Token runtime core (issue + validate in tool execution path).
  - Added nonce replay cache persistence across process restarts.
  - Added Security dashboard capability-token denial analytics panel + API.
  - Added Secret Broker runtime for ephemeral provider-secret leasing.
  - Added Secret Broker revoke/rotate API endpoints and dashboard status visibility.
  - Added Secret Broker settings schema and config fields.
  - Added Isolated Runner config/runtime baseline for high-risk tool execution.
  - Added shell isolated profile hooks (reduced env + tighter timeout + lockable working_directory).
  - Added Isolated Runner hard-mode guards (network/write/spawn/redirect command blocking + allow-prefix mode + cwd allowed-dir restriction).
  - Added Isolated Runner external sandbox hook (`hard_external_sandbox_prefix`) with required-runner enforcement for stronger host-level isolation integration.
  - Added Data Firewall runtime for untrusted web/MCP tool outputs before context injection.
  - Added Data Firewall analytics API + Security dashboard section.
  - Added tamper-evident audit hash chain (`prev_chain_hash` + `chain_hash`) with legacy DB auto-migration.
  - Upgraded integrity verification to validate both HMAC and hash-chain continuity.
  - Added Phase 7 baseline gradual-rollout controls (`gateway_rollout_percent`, `gateway_rollout_seed`) for enforce-mode proxy cutover.
  - Upgraded Phase 7 drill script to full suite (baseline + regression + load + incident) and exposed drill summary in Security dashboard/API.
  - Added Phase 7 drill runbook updates and dry-run rollback support.
  - Added one-command rollback script (`scripts/gateway_rollback.py`) for fast cutback to non-gateway routing.

## Remaining Work Snapshot

- Phase 1 operational checklist:
  - Run shadow burn-in on production-like traffic and verify `ready_for_enforce=true`.
  - Switch to enforce mode and monitor first 24-72 hours.
- Outstanding items:
  - Phase 1 operational runbook execution in production traffic windows.

## Architecture Direction

- Current: Engine-centric checks (approval/tool policy/egress/audit) inside app.
- Target: Defense-in-depth:
  - Lite Gateway for outbound traffic governance first.
  - Capability Token + Secret Broker next.
  - Isolated high-risk runners after that.

## Phases

### Phase 0 - Baseline & Guardrails (Week 1)

- Inventory all outbound paths:
  - Web tools (`web_*`, `web_crawl_batch`)
  - ComfyUI plugin HTTP
  - Discord attachment fetch/upload paths
  - MCP server outbound behavior
  - Model provider outbound calls
- Define rollout metrics:
  - Block rate / false block rate
  - P95 latency delta
  - Token + network cost trend
  - Auto-repair trigger/success rate
- Add rollback checklist per component.

### Phase 1 - Lite Gateway (Weeks 2-3)

- [x] Add global gateway proxy settings under `egress_policy`.
- [x] Route egress-managed network calls through a single proxy endpoint.
- [x] Support bypass list for local/private hosts.
- [x] Keep feature default-off and config-driven.
- Rollout mode:
  - [x] Shadow mode logs first.
  - [x] Enforce mode cutover gating via validation checks (`ready_for_enforce`).

### Phase 2 - Capability Tokens (Weeks 4-5)

- [x] Issue short-lived, scoped tokens per tool execution.
- Bind token to:
  - [x] tool name
  - [x] session/user/adapter
  - [x] allowed args/path/domain profile
  - [x] TTL and nonce
- [x] Enforce token validation in execution path.
- [x] Persist nonce replay cache for cross-process restarts.
- [x] Add token-deny analytics panel in Web UI security page.

### Phase 3 - Secret Broker (Weeks 6-7)

- [x] Remove long-lived provider secrets from agent runtime context.
- [x] Exchange broker-issued ephemeral credentials for provider calls.
- [x] Support revocation and rotation.

### Phase 4 - Isolated Runners (Weeks 8-9)

- Move high-risk tools into isolated runtime:
  - [x] `shell_execute` baseline isolated profile
  - high-risk desktop control flows
  - optional MCP execution wrappers
- Constrain FS/network/syscalls/timeouts per runner.
  - [x] timeout + environment minimization baseline
  - [x] stronger hard-isolation integration via external sandbox runner prefix + required mode

### Phase 5 - Data Firewall (Week 10)

- Sanitize untrusted web/MCP content before model context injection.
- Detect/remove:
  - [x] prompt-injection strings
  - [x] large base64 payloads
  - [x] suspicious command-like snippets

### Phase 6 - Tamper-Evident Audit (Week 11)

- [x] Extend audit with integrity chain/signature.
- Strengthen traceability:
  - [x] tool call args
  - [x] approval decision
  - [x] outbound decision (allow/deny/proxy route)

### Phase 7 - Gradual Rollout & Validation (Week 12)

- [x] Rollout by percentage / environment tier (baseline percentage bucket).
- [x] Run regression, load, and incident drills.
- [x] Keep one-command rollback to pre-gateway behavior.

## Acceptance Criteria

- Security:
  - >=95% block on known forbidden outbound scenarios.
- Reliability:
  - <2% false-positive blocks on approved workloads.
- Performance:
  - P95 latency increase <15%.
- Operability:
  - rollback under 10 minutes.

## Initial Implementation Scope (Now)

This first implementation starts **Phase 1 (Lite Gateway)**:

- Add config fields in `egress_policy`:
  - `gateway_enabled`
  - `gateway_proxy_url`
  - `gateway_bypass_domains`
  - `gateway_include_private_network`
- Add proxy resolution helper in `EgressBroker`.
- Apply proxy routing to selected outbound paths:
  - `web_crawl_batch` HTTP fetches
  - ComfyUI plugin HTTP requests/downloads/uploads
  - Discord image download path

## Rollback Strategy

- Set `egress_policy.gateway_enabled: false` to disable routing instantly.
- Existing egress allow/deny checks remain active and unchanged.
- No schema break: defaults preserve current behavior.
