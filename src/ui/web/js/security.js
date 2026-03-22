/**
 * Kuro - Security Dashboard Module
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml } from "./utils.js";
import { initPanelNav, refreshPanelNav } from "./panel_nav.js";
import KuroPlugins from "./plugins.js";

let lastData = null;

function scoreColor(score) {
    if (score >= 90) return "var(--success)";
    if (score >= 70) return "var(--warning)";
    return "var(--danger)";
}

function formatGatewayTime(ts) {
    if (!ts) return "";
    try {
        return new Date(ts).toLocaleString();
    } catch (_e) {
        return String(ts);
    }
}

function formatGatewayReason(reason) {
    const key = String(reason || "").trim().toLowerCase();
    const map = {
        routed_via_gateway: "security.gatewayReasonRouted",
        shadow_mode_candidate: "security.gatewayReasonShadowCandidate",
        bypass_domain: "security.gatewayReasonBypassDomain",
        private_network_bypass: "security.gatewayReasonPrivateBypass",
        rollout_not_selected: "security.gatewayReasonRolloutSkip",
        missing_proxy_url: "security.gatewayReasonMissingProxy",
        invalid_url: "security.gatewayReasonInvalidUrl",
        missing_host: "security.gatewayReasonMissingHost",
    };
    const i18nKey = map[key];
    if (i18nKey) return t(i18nKey, reason || key || "-");
    return key || "-";
}

function formatCapabilityReason(reason) {
    const key = String(reason || "").trim().toLowerCase();
    const map = {
        "missing capability token": "security.capReasonMissingToken",
        "invalid token format": "security.capReasonInvalidFormat",
        "invalid base64 token": "security.capReasonInvalidBase64",
        "signature mismatch": "security.capReasonSignatureMismatch",
        "invalid token payload": "security.capReasonInvalidPayload",
        "invalid token payload type": "security.capReasonInvalidPayloadType",
        "token issued in the future": "security.capReasonIssuedFuture",
        "token expired": "security.capReasonExpired",
        "tool mismatch": "security.capReasonToolMismatch",
        "session mismatch": "security.capReasonSessionMismatch",
        "adapter mismatch": "security.capReasonAdapterMismatch",
        "active model mismatch": "security.capReasonModelMismatch",
        "arguments digest mismatch": "security.capReasonArgsMismatch",
        "domain profile mismatch": "security.capReasonDomainMismatch",
        "path profile mismatch": "security.capReasonPathMismatch",
        "missing nonce": "security.capReasonMissingNonce",
        "nonce already used": "security.capReasonNonceReplay",
        "unknown": "security.capReasonUnknown",
    };
    const i18nKey = map[key];
    if (i18nKey) return t(i18nKey);
    return reason || "-";
}

function formatPercent(value) {
    const n = Number(value || 0);
    return (n * 100).toFixed(2) + "%";
}

function formatSignedPercent(value) {
    if (value == null) return "-";
    const n = Number(value);
    if (!Number.isFinite(n)) return "-";
    const pct = n * 100;
    const sign = pct > 0 ? "+" : "";
    return sign + pct.toFixed(2) + "%";
}

function formatGatewayCheckName(name) {
    const key = String(name || "").trim().toLowerCase();
    const map = {
        gateway_config_enabled: "security.gatewayCheckConfig",
        shadow_sample_size: "security.gatewayCheckShadowSamples",
        network_deny_rate: "security.gatewayCheckDenyRate",
        gateway_route_observed: "security.gatewayCheckRouteObserved",
        false_block_rate: "security.gatewayCheckFalseBlock",
        latency_p95_delta: "security.gatewayCheckLatencyDelta",
        token_cost_growth: "security.gatewayCheckTokenGrowth",
    };
    return t(map[key] || "security.gatewayCheckUnknown");
}

function formatGatewayNextStep(step) {
    const key = String(step || "").trim().toLowerCase();
    const map = {
        switch_to_enforce: "security.gatewayNextSwitchToEnforce",
        already_enforce_monitoring: "security.gatewayNextAlreadyEnforce",
        run_shadow_burn_in: "security.gatewayNextShadowBurnIn",
        review_failed_checks: "security.gatewayNextReviewFailed",
    };
    return t(map[key] || "security.gatewayCheckUnknown");
}

function formatGatewayDrillSection(sectionName) {
    const key = String(sectionName || "").trim().toLowerCase();
    const map = {
        baseline: "security.gatewayDrillBaseline",
        regression: "security.gatewayDrillRegression",
        load: "security.gatewayDrillLoad",
        incident: "security.gatewayDrillIncident",
    };
    return t(map[key] || "security.gatewayDrillUnknown");
}

async function postSecretBrokerAction(url, payload) {
    const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
    });
    let data = {};
    try {
        data = await resp.json();
    } catch (_e) {
        data = {};
    }
    if (!resp.ok || String(data.status || "").toLowerCase() !== "ok") {
        const reason = String(data.reason || data.message || resp.statusText || "unknown");
        throw new Error(reason);
    }
    return data;
}

function wireSecretBrokerActions(secretBroker) {
    const providerSelect = document.getElementById("secret-broker-provider");
    const newSecretInput = document.getElementById("secret-broker-new-secret");
    const revokeBtn = document.getElementById("secret-broker-revoke-btn");
    const rotateBtn = document.getElementById("secret-broker-rotate-btn");
    const clearBtn = document.getElementById("secret-broker-clear-btn");
    const statusEl = document.getElementById("secret-broker-status");

    if (!providerSelect || !statusEl) return;

    const providers = Array.isArray(secretBroker?.known_providers)
        ? secretBroker.known_providers
        : [];
    const defaultProvider = providers.length > 0 ? String(providers[0]) : "";
    if (!providerSelect.value && defaultProvider) {
        providerSelect.value = defaultProvider;
    }

    const setStatus = (message, isError = false) => {
        statusEl.textContent = message || "";
        statusEl.style.color = isError ? "var(--danger)" : "var(--text-muted)";
    };

    if (revokeBtn) {
        revokeBtn.addEventListener("click", async () => {
            const provider = String(providerSelect.value || "").trim();
            if (!provider) {
                setStatus(t("security.secretBrokerNoProviders"), true);
                return;
            }
            setStatus(t("common.loading", "Loading..."));
            try {
                await postSecretBrokerAction("/api/security/secret-broker/revoke", {
                    provider,
                });
                setStatus(t("security.secretBrokerActionSuccess"));
                fetchAndRender();
            } catch (e) {
                setStatus(t("security.secretBrokerActionFailed") + ": " + (e?.message || "error"), true);
            }
        });
    }

    if (rotateBtn) {
        rotateBtn.addEventListener("click", async () => {
            const provider = String(providerSelect.value || "").trim();
            if (!provider) {
                setStatus(t("security.secretBrokerNoProviders"), true);
                return;
            }
            const nextSecret = String(newSecretInput?.value || "").trim();
            setStatus(t("common.loading", "Loading..."));
            try {
                await postSecretBrokerAction("/api/security/secret-broker/rotate", {
                    provider,
                    new_secret: nextSecret,
                });
                setStatus(t("security.secretBrokerActionSuccess"));
                if (newSecretInput) newSecretInput.value = "";
                fetchAndRender();
            } catch (e) {
                setStatus(t("security.secretBrokerActionFailed") + ": " + (e?.message || "error"), true);
            }
        });
    }

    if (clearBtn) {
        clearBtn.addEventListener("click", async () => {
            const provider = String(providerSelect.value || "").trim();
            if (!provider) {
                setStatus(t("security.secretBrokerNoProviders"), true);
                return;
            }
            setStatus(t("common.loading", "Loading..."));
            try {
                await postSecretBrokerAction("/api/security/secret-broker/rotate", {
                    provider,
                    new_secret: "",
                });
                setStatus(t("security.secretBrokerActionSuccess"));
                if (newSecretInput) newSecretInput.value = "";
                fetchAndRender();
            } catch (e) {
                setStatus(t("security.secretBrokerActionFailed") + ": " + (e?.message || "error"), true);
            }
        });
    }
}

function formatGatewayCheckDetail(check) {
    const name = String(check?.name || "").trim().toLowerCase();
    if (name === "gateway_config_enabled") {
        return "value=" + String(check?.value) + " expected=true";
    }
    if (name === "shadow_sample_size") {
        return "value=" + String(check?.value ?? 0) + " expected_min=" + String(check?.expected_min ?? 0);
    }
    if (name === "network_deny_rate") {
        const value = formatPercent(check?.value || 0);
        const expectedMax = formatPercent(check?.expected_max || 0);
        const denied = Number(check?.denied || 0);
        const total = Number(check?.total || 0);
        return "value=" + value + " expected_max=" + expectedMax + " (denied=" + denied + ", total=" + total + ")";
    }
    if (name === "gateway_route_observed") {
        const gateway = Number(check?.gateway || 0);
        const shadow = Number(check?.shadow || 0);
        const direct = Number(check?.direct || 0);
        return "gateway=" + gateway + ", shadow=" + shadow + ", direct=" + direct;
    }
    if (name === "false_block_rate") {
        const value = formatPercent(check?.value || 0);
        const expectedMax = formatPercent(check?.expected_max || 0);
        const assessed = Number(check?.assessed_denied || 0);
        const recovered = Number(check?.recovered || 0);
        return "value=" + value + " expected_max=" + expectedMax + " (assessed=" + assessed + ", recovered=" + recovered + ")";
    }
    if (name === "latency_p95_delta") {
        if (check?.skipped) {
            return t("security.gatewayCheckSkipped");
        }
        const value = formatSignedPercent(check?.value);
        const expectedMax = formatPercent(check?.expected_max || 0);
        const currentP95 = check?.current_p95_ms == null ? "-" : String(check.current_p95_ms);
        const previousP95 = check?.previous_p95_ms == null ? "-" : String(check.previous_p95_ms);
        return "value=" + value + " expected_max=" + expectedMax + " (current=" + currentP95 + ", previous=" + previousP95 + ")";
    }
    if (name === "token_cost_growth") {
        if (check?.skipped) {
            return t("security.gatewayCheckSkipped");
        }
        const value = formatSignedPercent(check?.value);
        const expectedMax = formatPercent(check?.expected_max || 0);
        const current = Number(check?.current_total_tokens || 0);
        const previous = Number(check?.previous_total_tokens || 0);
        return "value=" + value + " expected_max=" + expectedMax + " (current=" + current + ", previous=" + previous + ")";
    }
    return "-";
}

function renderDashboard(data) {
    lastData = data;
    const dashboard = document.getElementById("dashboard");
    const loading = document.getElementById("loading");
    if (loading) loading.style.display = "none";

    const stats = data.daily_stats || {};
    const blocked = data.blocked_history || {};
    const score = data.security_score || {};

    let html = "";

    // --- Score Card ---
    const s = score.score || 0;
    const circumference = 2 * Math.PI * 50;
    const offset = circumference * (1 - s / 100);
    const color = scoreColor(s);

    html += '<div id="security-score" class="section">';
    html += '<div class="score-card">';
    html += '<div class="score-ring">';
    html += '<svg width="120" height="120" viewBox="0 0 120 120">';
    html += '<circle cx="60" cy="60" r="50" class="score-ring-bg"/>';
    html += '<circle cx="60" cy="60" r="50" class="score-ring-value" ';
    html += 'stroke="' + color + '" stroke-dasharray="' + circumference + '" stroke-dashoffset="' + offset + '"/>';
    html += '</svg>';
    html += '<div class="score-number" style="color:' + color + '">' + s + '</div>';
    html += '</div>';
    html += '<div class="score-details">';
    html += '<h2>' + t("security.securityScore") + ": " + (score.grade || "?") + '</h2>';

    html += '<div class="score-factors">';
    const factors = score.factors || [];
    for (let i = 0; i < factors.length; i++) {
        const f = factors[i];
        const cls = "factor factor-" + (f.status || "ok");
        html += '<div class="' + cls + '"><span class="factor-dot"></span>' +
                 escapeHtml(f.name) + ": " + escapeHtml(f.detail) + '</div>';
    }
    html += '</div>';

    if (score.recommendations && score.recommendations.length > 0) {
        html += '<div class="recommendations" style="margin-top:0.75rem">';
        for (let r = 0; r < score.recommendations.length; r++) {
            html += '<div class="rec-item">' + escapeHtml(score.recommendations[r]) + '</div>';
        }
        html += '</div>';
    }
    html += '</div></div>';
    html += '</div>';

    // --- Stats Grid ---
    html += '<div id="security-overview" class="section">';
    html += '<div class="stats-grid">';
    html += '<div class="stat-card"><div class="stat-value blue">' + (stats.total_events || 0) +
            '</div><div class="stat-label">' + t("security.totalEvents") + '</div></div>';
    html += '<div class="stat-card"><div class="stat-value green">' + (stats.approved || 0) +
            '</div><div class="stat-label">' + t("security.approved") + '</div></div>';
    html += '<div class="stat-card"><div class="stat-value red">' + (stats.denied || 0) +
            '</div><div class="stat-label">' + t("security.denied") + '</div></div>';
    html += '<div class="stat-card"><div class="stat-value yellow">' + (stats.security_events || 0) +
            '</div><div class="stat-label">' + t("security.securityEvents") + '</div></div>';
    html += '</div>';
    html += '</div>';

    // --- Two Column Charts ---
    html += '<div class="two-col">';

    // Risk Distribution
    html += '<div id="security-risk" class="chart-section"><h3>' + t("security.riskDistribution") + '</h3>';
    html += '<div class="risk-bars">';
    const risk = stats.risk_distribution || {};
    const maxRisk = Math.max(risk.low || 0, risk.medium || 0, risk.high || 0, risk.critical || 0, 1);
    const levels = ["low", "medium", "high", "critical"];
    for (let l = 0; l < levels.length; l++) {
        const lv = levels[l];
        const count = risk[lv] || 0;
        const pct = Math.round(count / maxRisk * 100);
        html += '<div class="risk-bar-row risk-' + lv + '">';
        html += '<span class="risk-bar-label">' + lv + '</span>';
        html += '<div class="risk-bar-track"><div class="risk-bar-fill" style="width:' + pct + '%"></div></div>';
        html += '<span class="risk-bar-count">' + count + '</span>';
        html += '</div>';
    }
    html += '</div></div>';

    // Top Tools
    html += '<div id="security-tools" class="chart-section"><h3>' + t("security.topTools") + '</h3>';
    const tools = stats.top_tools || [];
    if (tools.length > 0) {
        html += '<table class="data-table"><thead><tr><th>' + t("security.tool") + '</th><th>' + t("security.calls") + '</th></tr></thead><tbody>';
        for (let ti = 0; ti < Math.min(tools.length, 8); ti++) {
            html += '<tr><td class="tool-name">' + escapeHtml(tools[ti].tool) + '</td><td>' + tools[ti].count + '</td></tr>';
        }
        html += '</tbody></table>';
    } else {
        html += '<div style="color:var(--text-muted);font-size:0.85rem">' + t("security.noToolCalls") + '</div>';
    }
    html += '</div></div>';

    // --- Gateway Phase 1 Health ---
    const gatewayValidation = data.gateway_validation || {};
    html += '<div id="security-gateway-health" class="chart-section"><h3>' + t("security.gatewayHealthTitle") + '</h3>';
    if (gatewayValidation.error) {
        html += '<div style="color:var(--text-muted);font-size:0.85rem">' + t("security.gatewayHealthNoData") + '</div>';
    } else {
        const cfg = gatewayValidation.config_snapshot || {};
        const routeCounts = gatewayValidation.route_counts || {};
        const checks = Array.isArray(gatewayValidation.checks) ? gatewayValidation.checks : [];
        const cutover = gatewayValidation.cutover || {};
        const falseBlock = gatewayValidation.false_block || {};
        const mode = String(cfg.gateway_mode || "-");
        const shadowSamples = Number(routeCounts.shadow || 0);
        const denyRate = Number(gatewayValidation.network_tool_deny_rate || 0);
        const p95 = gatewayValidation.network_tool_latency_p95_ms;
        const latencyDelta = gatewayValidation.network_tool_latency_p95_delta_rate;
        const tokenGrowth = gatewayValidation.token_growth_rate;
        const falseBlockRate = Number(falseBlock.false_block_rate || 0);
        const ready = Boolean(cutover.ready_for_enforce);
        const failedChecks = Array.isArray(cutover.failed_checks) ? cutover.failed_checks : [];
        const nextStep = formatGatewayNextStep(cutover.recommended_next_step || "");

        html += '<div class="stats-grid">';
        html += '<div class="stat-card"><div class="stat-value blue">' + escapeHtml(mode) +
            '</div><div class="stat-label">' + t("security.gatewayHealthMode") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value yellow">' + shadowSamples +
            '</div><div class="stat-label">' + t("security.gatewayHealthShadowSamples") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value green">' + escapeHtml(formatPercent(denyRate)) +
            '</div><div class="stat-label">' + t("security.gatewayHealthDenyRate") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value blue">' + escapeHtml(String(p95 == null ? "-" : p95)) +
            '</div><div class="stat-label">' + t("security.gatewayHealthP95") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value yellow">' + escapeHtml(formatPercent(falseBlockRate)) +
            '</div><div class="stat-label">' + t("security.gatewayHealthFalseBlock") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value blue">' + escapeHtml(formatSignedPercent(latencyDelta)) +
            '</div><div class="stat-label">' + t("security.gatewayHealthLatencyDelta") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value blue">' + escapeHtml(formatSignedPercent(tokenGrowth)) +
            '</div><div class="stat-label">' + t("security.gatewayHealthTokenGrowth") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value ' + (ready ? "green" : "red") + '">' +
            escapeHtml(ready ? t("security.gatewayHealthReadyYes") : t("security.gatewayHealthReadyNo")) +
            '</div><div class="stat-label">' + t("security.gatewayHealthReady") + '</div></div>';
        html += '</div>';
        html += '<div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.35rem">' +
            t("security.gatewayHealthWindow") + ": " + Number(gatewayValidation.days || 0) + " day(s)" +
            '</div>';
        html += '<div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.2rem">' +
            t("security.gatewayHealthNextStep") + ": " + escapeHtml(nextStep) + '</div>';
        if (failedChecks.length > 0) {
            html += '<div style="font-size:0.78rem;color:var(--warning);margin-top:0.2rem">' +
                t("security.gatewayHealthFailedChecks") + ": " +
                escapeHtml(failedChecks.map(formatGatewayCheckName).join(", ")) +
                '</div>';
        }

        if (checks.length > 0) {
            html += '<div class="score-factors" style="margin-top:0.75rem">';
            for (let ci = 0; ci < checks.length; ci++) {
                const chk = checks[ci] || {};
                const cls = "factor factor-" + (chk.ok ? "ok" : "warning");
                html += '<div class="' + cls + '"><span class="factor-dot"></span>' +
                    escapeHtml(formatGatewayCheckName(chk.name)) + ": " +
                    escapeHtml(formatGatewayCheckDetail(chk)) + '</div>';
            }
            html += '</div>';
        }
    }
    html += '</div>';

    // --- Gateway Phase 7 Drill ---
    const gatewayDrill = data.gateway_drill || {};
    html += '<div id="security-gateway-drill" class="chart-section"><h3>' + t("security.gatewayDrillTitle") + '</h3>';
    if (String(gatewayDrill.status || "").toLowerCase() === "error") {
        html += '<div style="color:var(--text-muted);font-size:0.85rem">' +
            t("security.gatewayDrillNoData") + '</div>';
    } else {
        const drillPassed = Boolean(gatewayDrill.passed);
        const sections = Array.isArray(gatewayDrill.sections) ? gatewayDrill.sections : [];
        const peakHour = gatewayDrill.peak_hour || {};
        const peakCalls = Number(peakHour.total_calls || 0);
        const peakDenied = Number(peakHour.denied_calls || 0);
        const peakDenyRate = Number(peakHour.deny_rate || 0);
        html += '<div class="stats-grid">';
        html += '<div class="stat-card"><div class="stat-value ' + (drillPassed ? "green" : "red") + '">' +
            escapeHtml(drillPassed ? t("security.gatewayHealthReadyYes") : t("security.gatewayHealthReadyNo")) +
            '</div><div class="stat-label">' + t("security.gatewayDrillOverall") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value blue">' + escapeHtml(String(peakHour.hour_bucket || "-")) +
            '</div><div class="stat-label">' + t("security.gatewayDrillPeakHour") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value yellow">' + peakCalls +
            '</div><div class="stat-label">' + t("security.gatewayDrillPeakCalls") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value red">' + peakDenied +
            '</div><div class="stat-label">' + t("security.gatewayDrillPeakDenied") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value blue">' + escapeHtml(formatPercent(peakDenyRate)) +
            '</div><div class="stat-label">' + t("security.gatewayDrillPeakDenyRate") + '</div></div>';
        html += '</div>';

        for (let si = 0; si < sections.length; si++) {
            const sec = sections[si] || {};
            const secChecks = Array.isArray(sec.checks) ? sec.checks : [];
            const secPassed = Boolean(sec.passed);
            html += '<div style="margin-top:0.65rem">';
            html += '<div style="font-size:0.82rem;font-weight:600;color:' +
                (secPassed ? "var(--success)" : "var(--warning)") + '">';
            html += escapeHtml(formatGatewayDrillSection(sec.name)) + ': ' +
                escapeHtml(secPassed ? t("security.gatewayHealthReadyYes") : t("security.gatewayHealthReadyNo"));
            html += '</div>';
            if (secChecks.length > 0) {
                html += '<div class="score-factors" style="margin-top:0.35rem">';
                for (let ci = 0; ci < secChecks.length; ci++) {
                    const chk = secChecks[ci] || {};
                    const skipped = Boolean(chk.skipped);
                    const ok = Boolean(chk.ok);
                    const cls = "factor factor-" + (ok ? "ok" : "warning");
                    html += '<div class="' + cls + '"><span class="factor-dot"></span>' +
                        escapeHtml((skipped ? "[SKIP] " : "") + String(chk.name || "-")) +
                        ": " + escapeHtml(String(chk.detail || "-")) + '</div>';
                }
                html += '</div>';
            }
            html += '</div>';
        }
    }
    html += '</div>';

    // --- Gateway Logs ---
    const gatewayLogs = Array.isArray(data.gateway_logs) ? data.gateway_logs : [];
    html += '<div id="security-gateway" class="chart-section"><h3>' + t("security.gatewayLogs") + '</h3>';
    if (gatewayLogs.length > 0) {
        html += '<div class="table-scroll"><table class="data-table"><thead><tr>' +
            '<th>' + t("security.gatewayTime") + '</th>' +
            '<th>' + t("security.gatewayTool") + '</th>' +
            '<th>' + t("security.gatewayRoute") + '</th>' +
            '<th>' + t("security.gatewayReason") + '</th>' +
            '<th>' + t("security.gatewayTarget") + '</th>' +
            '<th>' + t("security.gatewayProxy") + '</th>' +
            '</tr></thead><tbody>';
        for (let gi = 0; gi < Math.min(gatewayLogs.length, 60); gi++) {
            const row = gatewayLogs[gi] || {};
            const routeRaw = String(row.route || "").toLowerCase();
            const route = routeRaw === "gateway"
                ? t("security.gatewayRouteGateway")
                : (routeRaw === "shadow" ? t("security.gatewayRouteShadow") : t("security.gatewayRouteDirect"));
            html += '<tr>' +
                '<td>' + escapeHtml(formatGatewayTime(row.timestamp)) + '</td>' +
                '<td>' + escapeHtml(String(row.tool_name || "-")) + '</td>' +
                '<td>' + escapeHtml(route) + '</td>' +
                '<td>' + escapeHtml(formatGatewayReason(row.reason)) + '</td>' +
                '<td class="tool-name">' + escapeHtml(String(row.target || "-")) + '</td>' +
                '<td>' + escapeHtml(String(row.proxy || "-")) + '</td>' +
                '</tr>';
        }
        html += '</tbody></table></div>';
    } else {
        html += '<div style="color:var(--text-muted);font-size:0.85rem">' + t("security.noGatewayLogs") + '</div>';
    }
    html += '</div>';

    // --- Capability Token Denials ---
    const capability = data.capability_token_denials || {};
    const totalCapabilityDenied = Number(capability.total_denied || 0);
    const capabilityDays = Number(capability.days || 7);
    const capabilityDaily = Array.isArray(capability.daily_counts) ? capability.daily_counts : [];
    const capabilityTopReasons = Array.isArray(capability.top_reasons) ? capability.top_reasons : [];
    const capabilityTopTools = Array.isArray(capability.top_tools) ? capability.top_tools : [];
    const capabilityRecent = Array.isArray(capability.recent) ? capability.recent : [];

    html += '<div id="security-capability" class="chart-section"><h3>' + t("security.capabilityDenyTitle", "Capability Token Denials") + '</h3>';
    if (totalCapabilityDenied <= 0) {
        html += '<div style="color:var(--text-muted);font-size:0.85rem">' + t("security.capabilityDenyNoData", "No capability token denials in this window") + '</div>';
    } else {
        html += '<div class="stats-grid">';
        html += '<div class="stat-card"><div class="stat-value red">' + totalCapabilityDenied +
            '</div><div class="stat-label">' + t("security.capabilityDenyTotal", "Denied Events") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value yellow">' + Number(capability.unique_reasons || 0) +
            '</div><div class="stat-label">' + t("security.capabilityDenyReasons", "Unique Reasons") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value blue">' + Number(capability.unique_tools || 0) +
            '</div><div class="stat-label">' + t("security.capabilityDenyTools", "Affected Tools") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value green">' + capabilityDays +
            '</div><div class="stat-label">' + t("security.capabilityDenyWindowDays", "Window (days)") + '</div></div>';
        html += '</div>';

        if (capabilityDaily.length > 0) {
            const maxCap = Math.max.apply(null, capabilityDaily.map((d) => Number(d.count || 0)).concat([1]));
            html += '<div class="bar-chart" style="margin-top:0.75rem">';
            for (let ci = 0; ci < capabilityDaily.length; ci++) {
                const row = capabilityDaily[ci] || {};
                const count = Number(row.count || 0);
                const pct = Math.round(count / maxCap * 100);
                html += '<div class="bar" style="height:' + Math.max(pct, 3) + '%" data-tooltip="' +
                    escapeHtml(String(row.date || "-") + " - " + count) + '"></div>';
            }
            html += '</div>';
            html += '<div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.35rem">' +
                t("security.capabilityDenyTrend", "Daily denials trend") + '</div>';
        }

        html += '<div class="two-col" style="margin-top:0.75rem">';
        html += '<div class="chart-section"><h3>' + t("security.capabilityTopReasons", "Top Denial Reasons") + '</h3>';
        if (capabilityTopReasons.length > 0) {
            html += '<table class="data-table"><thead><tr><th>' + t("security.gatewayReason") + '</th><th>' + t("security.calls") + '</th></tr></thead><tbody>';
            for (let ri = 0; ri < Math.min(capabilityTopReasons.length, 8); ri++) {
                const row = capabilityTopReasons[ri] || {};
                html += '<tr><td class="tool-name">' + escapeHtml(formatCapabilityReason(row.reason)) + '</td><td>' +
                    Number(row.count || 0) + '</td></tr>';
            }
            html += '</tbody></table>';
        } else {
            html += '<div style="color:var(--text-muted);font-size:0.85rem">' + t("security.capabilityDenyNoData", "No capability token denials in this window") + '</div>';
        }
        html += '</div>';

        html += '<div class="chart-section"><h3>' + t("security.capabilityTopTools", "Top Denied Tools") + '</h3>';
        if (capabilityTopTools.length > 0) {
            html += '<table class="data-table"><thead><tr><th>' + t("security.tool") + '</th><th>' + t("security.calls") + '</th></tr></thead><tbody>';
            for (let ti = 0; ti < Math.min(capabilityTopTools.length, 8); ti++) {
                const row = capabilityTopTools[ti] || {};
                html += '<tr><td class="tool-name">' + escapeHtml(String(row.tool || "-")) + '</td><td>' +
                    Number(row.count || 0) + '</td></tr>';
            }
            html += '</tbody></table>';
        } else {
            html += '<div style="color:var(--text-muted);font-size:0.85rem">' + t("security.capabilityDenyNoData", "No capability token denials in this window") + '</div>';
        }
        html += '</div></div>';

        if (capabilityRecent.length > 0) {
            html += '<div class="chart-section" style="margin-top:0.75rem"><h3>' + t("security.capabilityRecent", "Recent Capability Denials") + '</h3>';
            html += '<div class="table-scroll"><table class="data-table"><thead><tr>' +
                '<th>' + t("security.gatewayTime") + '</th>' +
                '<th>' + t("security.tool") + '</th>' +
                '<th>' + t("security.gatewayReason") + '</th>' +
                '</tr></thead><tbody>';
            for (let ri = 0; ri < Math.min(capabilityRecent.length, 20); ri++) {
                const row = capabilityRecent[ri] || {};
                html += '<tr>' +
                    '<td>' + escapeHtml(formatGatewayTime(row.timestamp)) + '</td>' +
                    '<td>' + escapeHtml(String(row.tool_name || "-")) + '</td>' +
                    '<td class="tool-name">' + escapeHtml(formatCapabilityReason(row.reason)) + '</td>' +
                    '</tr>';
            }
            html += '</tbody></table></div></div>';
        }
    }
    html += '</div>';

    // --- Secret Broker ---
    const secretBroker = data.secret_broker || {};
    const sbEnabled = Boolean(secretBroker.enabled);
    const sbProviders = Array.isArray(secretBroker.known_providers) ? secretBroker.known_providers : [];
    const sbOverrides = Array.isArray(secretBroker.rotated_override_providers)
        ? secretBroker.rotated_override_providers
        : [];
    const sbGenerations = secretBroker.provider_generations || {};
    html += '<div id="security-secret-broker" class="chart-section"><h3>' +
        t("security.secretBrokerTitle", "Secret Broker") + '</h3>';
    if (!sbEnabled) {
        html += '<div style="color:var(--text-muted);font-size:0.85rem">' +
            t("security.secretBrokerDisabled", "Secret Broker is disabled in current config") + '</div>';
    } else {
        html += '<div class="stats-grid">';
        html += '<div class="stat-card"><div class="stat-value blue">' + Number(secretBroker.active_leases || 0) +
            '</div><div class="stat-label">' + t("security.secretBrokerActiveLeases", "Active Leases") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value green">' + sbProviders.length +
            '</div><div class="stat-label">' + t("security.secretBrokerProviders", "Known Providers") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value yellow">' + sbOverrides.length +
            '</div><div class="stat-label">' + t("security.secretBrokerOverrides", "Override Providers") + '</div></div>';
        html += '</div>';

        if (Object.keys(sbGenerations).length > 0) {
            html += '<div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.45rem">';
            html += 'generation: ' + escapeHtml(
                Object.entries(sbGenerations)
                    .map(([name, gen]) => `${name}:${gen}`)
                    .join(", "),
            );
            html += '</div>';
        }

        html += '<div style="display:flex;flex-wrap:wrap;gap:0.5rem;margin-top:0.75rem;align-items:flex-end">';
        html += '<div style="min-width:220px;flex:1">';
        html += '<label style="display:block;font-size:0.78rem;color:var(--text-muted);margin-bottom:0.2rem">' +
            t("security.secretBrokerProvider", "Provider") + '</label>';
        html += '<select id="secret-broker-provider" class="setting-input">';
        if (sbProviders.length === 0) {
            html += '<option value="">' + escapeHtml(t("security.secretBrokerNoProviders", "No providers available")) + '</option>';
        } else {
            html += '<option value="">' + escapeHtml(t("security.secretBrokerSelectProvider", "Select provider")) + '</option>';
            for (let pi = 0; pi < sbProviders.length; pi++) {
                const provider = String(sbProviders[pi] || "");
                html += '<option value="' + escapeHtml(provider) + '">' + escapeHtml(provider) + '</option>';
            }
        }
        html += '</select></div>';
        html += '<button id="secret-broker-revoke-btn" class="btn-secondary">' +
            t("security.secretBrokerRevoke", "Revoke Provider Leases") + '</button>';
        html += '</div>';

        html += '<div style="display:flex;flex-wrap:wrap;gap:0.5rem;margin-top:0.5rem;align-items:flex-end">';
        html += '<div style="min-width:320px;flex:1">';
        html += '<label style="display:block;font-size:0.78rem;color:var(--text-muted);margin-bottom:0.2rem">' +
            t("security.secretBrokerNewSecret", "New Secret (optional)") + '</label>';
        html += '<input id="secret-broker-new-secret" type="password" class="setting-input" placeholder="' +
            escapeHtml(t("security.secretBrokerNewSecret", "New Secret (optional)")) + '"/>';
        html += '</div>';
        html += '<button id="secret-broker-rotate-btn" class="btn-primary">' +
            t("security.secretBrokerRotate", "Rotate Secret") + '</button>';
        html += '<button id="secret-broker-clear-btn" class="btn-secondary">' +
            t("security.secretBrokerClearOverride", "Clear Override") + '</button>';
        html += '</div>';
        html += '<div id="secret-broker-status" style="font-size:0.78rem;color:var(--text-muted);margin-top:0.45rem"></div>';
    }
    html += '</div>';

    // --- Audit Integrity (Phase 6) ---
    const integrity = data.integrity || {};
    const integrityTotal = Number(integrity.total_checked || 0);
    const integrityTampered = Number(integrity.tampered || 0);
    const integrityOk = String(integrity.integrity || "").toLowerCase() === "ok";
    html += '<div id="security-integrity" class="chart-section"><h3>' +
        t("security.auditIntegrityTitle", "Audit Integrity") + '</h3>';
    html += '<div class="stats-grid">';
    html += '<div class="stat-card"><div class="stat-value blue">' + integrityTotal +
        '</div><div class="stat-label">' + t("security.auditIntegrityChecked", "Checked Entries") + '</div></div>';
    html += '<div class="stat-card"><div class="stat-value ' + (integrityTampered > 0 ? "red" : "green") + '">' + integrityTampered +
        '</div><div class="stat-label">' + t("security.auditIntegrityTampered", "Tampered Entries") + '</div></div>';
    html += '<div class="stat-card"><div class="stat-value ' + (integrityOk ? "green" : "red") + '">' +
        escapeHtml(integrityOk ? t("security.auditIntegrityOk", "OK") : t("security.auditIntegrityCompromised", "COMPROMISED")) +
        '</div><div class="stat-label">' + t("security.auditIntegrityStatus", "Integrity") + '</div></div>';
    html += '</div></div>';

    // --- Data Firewall (Phase 5) ---
    const dataFirewall = data.data_firewall || {};
    const dfTotal = Number(dataFirewall.total_events || 0);
    html += '<div id="security-data-firewall" class="chart-section"><h3>' +
        t("security.dataFirewallTitle", "Data Firewall") + '</h3>';
    if (dfTotal <= 0) {
        html += '<div style="color:var(--text-muted);font-size:0.85rem">' +
            t("security.dataFirewallNoData", "No sanitization events in this window") + '</div>';
    } else {
        html += '<div class="stats-grid">';
        html += '<div class="stat-card"><div class="stat-value blue">' + dfTotal +
            '</div><div class="stat-label">' + t("security.dataFirewallEvents", "Sanitized Events") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value yellow">' + Number(dataFirewall.removed_prompt_injection_lines || 0) +
            '</div><div class="stat-label">' + t("security.dataFirewallRemovedInjection", "Injection Lines Removed") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value yellow">' + Number(dataFirewall.removed_command_like_lines || 0) +
            '</div><div class="stat-label">' + t("security.dataFirewallRemovedCommands", "Command-like Lines Removed") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value yellow">' + Number(dataFirewall.removed_base64_chunks || 0) +
            '</div><div class="stat-label">' + t("security.dataFirewallRemovedBase64", "Base64 Chunks Removed") + '</div></div>';
        html += '<div class="stat-card"><div class="stat-value ' +
            (Number(dataFirewall.truncated_events || 0) > 0 ? "red" : "green") + '">' + Number(dataFirewall.truncated_events || 0) +
            '</div><div class="stat-label">' + t("security.dataFirewallTruncated", "Truncated Events") + '</div></div>';
        html += '</div>';

        const dfTopTools = Array.isArray(dataFirewall.top_tools) ? dataFirewall.top_tools : [];
        if (dfTopTools.length > 0) {
            html += '<div class="chart-section" style="margin-top:0.75rem"><h3>' +
                t("security.dataFirewallTopTools", "Top Sanitized Tools") + '</h3>';
            html += '<table class="data-table"><thead><tr><th>' + t("security.tool") + '</th><th>' +
                t("security.calls") + '</th></tr></thead><tbody>';
            for (let i = 0; i < Math.min(dfTopTools.length, 8); i++) {
                const row = dfTopTools[i] || {};
                html += '<tr><td class="tool-name">' + escapeHtml(String(row.tool || "-")) + '</td><td>' +
                    Number(row.count || 0) + '</td></tr>';
            }
            html += '</tbody></table></div>';
        }
    }
    html += '</div>';

    // --- Hourly Activity ---
    html += '<div id="security-hourly" class="chart-section"><h3>' + t("security.hourlyActivity") + '</h3>';
    const hourly = stats.hourly_activity || [];
    const maxH = Math.max.apply(null, hourly.concat([1]));
    html += '<div class="bar-chart">';
    for (let h = 0; h < 24; h++) {
        const val = hourly[h] || 0;
        const hPct = Math.round(val / maxH * 100);
        html += '<div class="bar" style="height:' + Math.max(hPct, 3) + '%" data-tooltip="' + h + ':00 - ' + val + ' events"></div>';
    }
    html += '</div>';
    html += '<div class="bar-labels">';
    for (let h2 = 0; h2 < 24; h2 += 3) {
        html += '<span>' + h2 + '</span><span></span><span></span>';
    }
    html += '</div></div>';

    // --- Blocked History (7 days) ---
    const dailyCounts = blocked.daily_counts || [];
    if (dailyCounts.length > 0) {
        html += '<div id="security-history" class="chart-section"><h3>' + t("security.approvedVsDenied") + '</h3>';
        let maxB = 1;
        for (let b = 0; b < dailyCounts.length; b++) {
            const tot = (dailyCounts[b].approved || 0) + (dailyCounts[b].denied || 0);
            if (tot > maxB) maxB = tot;
        }
        html += '<div class="blocked-chart">';
        for (let d = 0; d < dailyCounts.length; d++) {
            const day = dailyCounts[d];
            const appH = Math.round((day.approved || 0) / maxB * 70);
            const denH = Math.round((day.denied || 0) / maxB * 70);
            html += '<div class="blocked-day">';
            html += '<div class="blocked-day-bar denied" style="height:' + Math.max(denH, 1) + 'px"></div>';
            html += '<div class="blocked-day-bar approved" style="height:' + Math.max(appH, 1) + 'px"></div>';
            html += '<div class="blocked-day-label">' + (day.date || "").slice(5) + '</div>';
            html += '</div>';
        }
        html += '</div>';
        html += '<div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.5rem">' +
                 '<span style="color:var(--success)">&#9632;</span> ' + t("security.approved") + ": " + (blocked.total_approved || 0) +
                 ' &nbsp; <span style="color:var(--danger)">&#9632;</span> ' + t("security.denied") + ": " + (blocked.total_blocked || 0) +
                 '</div></div>';
    }

    dashboard.innerHTML = html;
    wireSecretBrokerActions(data.secret_broker || {});
    refreshPanelNav();
}

function fetchAndRender() {
    fetch("/api/security/dashboard")
        .then(r => r.json())
        .then(renderDashboard)
        .catch(e => {
            const loading = document.getElementById("loading");
            if (loading) loading.textContent = t("security.loadFailed") + ": " + e.message;
        });
}

// === Init ===

async function init() {
    await initLayout({ activePath: "/security" });
    await initPanelNav("security");

    fetchAndRender();

    // Auto-refresh every 30 seconds
    setInterval(fetchAndRender, 30000);

    onLocaleChange(() => {
        if (lastData) renderDashboard(lastData);
        refreshPanelNav();
    });

    KuroPlugins.initAll();
    KuroPlugins.emit("onPageLoad", "security");
}

init();
