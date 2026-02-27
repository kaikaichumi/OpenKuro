/**
 * Kuro - Security Dashboard Module
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml } from "./utils.js";
import KuroPlugins from "./plugins.js";

let lastData = null;

function scoreColor(score) {
    if (score >= 90) return "var(--success)";
    if (score >= 70) return "var(--warning)";
    return "var(--danger)";
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

    // --- Stats Grid ---
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

    // --- Two Column Charts ---
    html += '<div class="two-col">';

    // Risk Distribution
    html += '<div class="chart-section"><h3>' + t("security.riskDistribution") + '</h3>';
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
    html += '<div class="chart-section"><h3>' + t("security.topTools") + '</h3>';
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

    // --- Hourly Activity ---
    html += '<div class="chart-section"><h3>' + t("security.hourlyActivity") + '</h3>';
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
        html += '<div class="chart-section"><h3>' + t("security.approvedVsDenied") + '</h3>';
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

    fetchAndRender();

    // Auto-refresh every 30 seconds
    setInterval(fetchAndRender, 30000);

    onLocaleChange(() => {
        if (lastData) renderDashboard(lastData);
    });

    KuroPlugins.initAll();
    KuroPlugins.emit("onPageLoad", "security");
}

init();
