/**
 * Kuro - Analytics Dashboard Module
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml, formatTokens } from "./utils.js";
import KuroPlugins from "./plugins.js";

let lastResults = null;
let saveTimers = {};  // debounce timers per model

function calcCostDisplay(pt, ct, inputRate, outputRate) {
    const ir = parseFloat(inputRate);
    const or_ = parseFloat(outputRate);
    if (isNaN(ir) && isNaN(or_)) return "N/A";
    const cost = (isNaN(ir) ? 0 : pt / 1000 * ir) + (isNaN(or_) ? 0 : ct / 1000 * or_);
    return "$" + cost.toFixed(4);
}

function savePricing(model, inputRate, outputRate) {
    // Debounce: save 600ms after last keystroke
    if (saveTimers[model]) clearTimeout(saveTimers[model]);
    saveTimers[model] = setTimeout(() => {
        const ir = parseFloat(inputRate) || 0;
        const or_ = parseFloat(outputRate) || 0;
        fetch("/api/analytics/pricing/" + encodeURIComponent(model), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ input: ir, output: or_ }),
        }).catch(e => console.error("save pricing error:", e));
    }, 600);
}

function recalcTotalCost() {
    const table = document.getElementById("model-cost-table");
    if (!table) return;
    let total = 0;
    table.querySelectorAll("tbody tr").forEach(tr => {
        const pt = parseFloat(tr.dataset.pt) || 0;
        const ct = parseFloat(tr.dataset.ct) || 0;
        const ir = parseFloat(tr.querySelector(".input-rate")?.value);
        const or_ = parseFloat(tr.querySelector(".output-rate")?.value);
        if (!isNaN(ir) || !isNaN(or_)) {
            total += (isNaN(ir) ? 0 : pt / 1000 * ir) + (isNaN(or_) ? 0 : ct / 1000 * or_);
        }
    });
    // Update summary card
    const costCard = document.querySelector(".stat-value.yellow");
    if (costCard) costCard.textContent = "$" + total.toFixed(2);
}

function bindRateInputs() {
    const table = document.getElementById("model-cost-table");
    if (!table) return;
    table.addEventListener("input", function(e) {
        if (!e.target.classList.contains("rate-input")) return;
        const tr = e.target.closest("tr");
        if (!tr) return;
        const model = tr.dataset.model;
        const pt = parseFloat(tr.dataset.pt) || 0;
        const ct = parseFloat(tr.dataset.ct) || 0;
        const inputRate = tr.querySelector(".input-rate").value;
        const outputRate = tr.querySelector(".output-rate").value;
        // Recalc this row's cost
        tr.querySelector(".cost-cell").textContent = calcCostDisplay(pt, ct, inputRate, outputRate);
        // Recalc total
        recalcTotalCost();
        // Auto-save
        savePricing(model, inputRate, outputRate);
    });
}

function renderAnalytics(usage, costs, suggestions, pricing) {
    const dashboard = document.getElementById("dashboard");
    const loading = document.getElementById("loading");
    if (loading) loading.style.display = "none";

    let html = "";

    // --- Summary Stats ---
    const totalTokens = costs.total_tokens || 0;
    html += '<div class="stats-grid">';
    html += '<div class="stat-card"><div class="stat-value blue">' + (usage.total_calls || 0) +
            '</div><div class="stat-label">' + t("analytics.toolCalls30d") + '</div></div>';
    html += '<div class="stat-card"><div class="stat-value yellow">$' + (costs.total_estimated_cost_usd || 0).toFixed(2) +
            '</div><div class="stat-label">' + t("analytics.estCost30d") + '</div></div>';
    html += '<div class="stat-card"><div class="stat-value blue">' + formatTokens(totalTokens) +
            '</div><div class="stat-label">' + t("analytics.totalTokens30d") + '</div></div>';
    html += '<div class="stat-card"><div class="stat-value ' + ((usage.error_rate || 0) > 5 ? "red" : "green") + '">' +
            (usage.error_rate || 0) + '%</div><div class="stat-label">' + t("analytics.errorRate") + '</div></div>';
    html += '</div>';

    // Tool Usage
    html += '<div class="chart-section"><h3>' + t("analytics.mostUsedTools") + '</h3>';
    const toolCounts = usage.tool_counts || {};
    const toolList = [];
    for (const k in toolCounts) { toolList.push([k, toolCounts[k]]); }
    toolList.sort((a, b) => b[1] - a[1]);
    const maxCount = toolList.length > 0 ? toolList[0][1] : 1;

    html += '<div class="usage-bars">';
    for (let i = 0; i < Math.min(toolList.length, 10); i++) {
        const name = toolList[i][0];
        const count = toolList[i][1];
        const pct = Math.round(count / maxCount * 100);
        html += '<div class="usage-bar-row">';
        html += '<span class="usage-bar-label">' + escapeHtml(name) + '</span>';
        html += '<div class="usage-bar-track"><div class="usage-bar-fill" style="width:' + pct + '%"></div></div>';
        html += '<span class="usage-bar-count">' + count + '</span>';
        html += '</div>';
    }
    if (toolList.length === 0) {
        html += '<div style="color:var(--text-muted);font-size:0.85rem">' + t("analytics.noToolUsage") + '</div>';
    }
    html += '</div></div>';

    // Token Usage & Cost by Model
    html += '<div class="chart-section"><h3>' + t("analytics.tokenUsageCost") + '</h3>';
    const byModel = costs.by_model || {};
    const modelList = [];
    for (const m in byModel) { modelList.push([m, byModel[m]]); }
    modelList.sort((a, b) => {
        if (a[1].has_pricing && !b[1].has_pricing) return -1;
        if (!a[1].has_pricing && b[1].has_pricing) return 1;
        if (a[1].has_pricing && b[1].has_pricing) return (b[1].estimated_cost_usd || 0) - (a[1].estimated_cost_usd || 0);
        return (b[1].total_tokens || 0) - (a[1].total_tokens || 0);
    });

    if (modelList.length > 0) {
        html += '<div class="table-scroll"><table class="data-table" id="model-cost-table"><thead><tr>';
        html += '<th>' + t("analytics.model") + '</th><th>' + t("analytics.calls") + '</th>';
        html += '<th>' + t("analytics.promptTokens") + '</th><th>' + t("analytics.completionTokens") + '</th><th>' + t("analytics.totalTokens") + '</th>';
        html += '<th>' + t("analytics.inputRate") + '</th><th>' + t("analytics.outputRate") + '</th>';
        html += '<th>' + t("analytics.estCost") + '</th>';
        html += '</tr></thead><tbody>';
        for (let j = 0; j < modelList.length; j++) {
            const mn = modelList[j][0];
            const info = modelList[j][1];
            const inputRate = (info.has_pricing && info.pricing) ? info.pricing.input : "";
            const outputRate = (info.has_pricing && info.pricing) ? info.pricing.output : "";
            const pt = info.prompt_tokens || 0;
            const ct = info.completion_tokens || 0;
            html += '<tr data-model="' + escapeHtml(mn) + '" data-pt="' + pt + '" data-ct="' + ct + '">';
            html += '<td class="model-name">' + escapeHtml(mn) + '</td>';
            html += '<td>' + (info.calls || 0) + '</td>';
            html += '<td class="tokens">' + formatTokens(pt) + '</td>';
            html += '<td class="tokens">' + formatTokens(ct) + '</td>';
            html += '<td class="tokens">' + formatTokens(info.total_tokens || 0) + '</td>';
            html += '<td class="rate"><span class="rate-prefix">$</span><input type="number" class="rate-input input-rate" step="any" min="0" placeholder="—" value="' + inputRate + '"><span class="rate-suffix">/1K</span></td>';
            html += '<td class="rate"><span class="rate-prefix">$</span><input type="number" class="rate-input output-rate" step="any" min="0" placeholder="—" value="' + outputRate + '"><span class="rate-suffix">/1K</span></td>';
            html += '<td class="cost-val cost-cell">' + calcCostDisplay(pt, ct, inputRate, outputRate) + '</td>';
            html += '</tr>';
        }
        html += '</tbody></table></div>';
    } else {
        html += '<div style="color:var(--text-muted);font-size:0.85rem">' + t("analytics.noTokenUsage") + '</div>';
    }
    html += '</div>';

    // --- Daily Activity ---
    const daily = usage.daily_activity || [];
    if (daily.length > 0) {
        html += '<div class="chart-section"><h3>' + t("analytics.dailyActivity") + '</h3>';
        let maxD = 1;
        for (let d = 0; d < daily.length; d++) {
            if (daily[d].count > maxD) maxD = daily[d].count;
        }
        html += '<div class="bar-chart tall">';
        for (let d2 = 0; d2 < daily.length; d2++) {
            const pctD = Math.round(daily[d2].count / maxD * 100);
            html += '<div class="bar" style="height:' + Math.max(pctD, 3) + '%" data-tooltip="' + daily[d2].date + ': ' + daily[d2].count + ' calls"></div>';
        }
        html += '</div>';
        html += '<div class="bar-labels">';
        for (let d3 = 0; d3 < daily.length; d3++) {
            if (d3 % 5 === 0) {
                html += '<span>' + daily[d3].date.slice(5) + '</span>';
            } else {
                html += '<span></span>';
            }
        }
        html += '</div></div>';
    }

    // --- Pricing Reference (only show models NOT already in the cost table) ---
    if (pricing && pricing.models) {
        const usedModels = new Set(modelList.map(m => m[0]));
        const unusedPricing = [];
        for (const pm in pricing.models) {
            if (!usedModels.has(pm)) unusedPricing.push([pm, pricing.models[pm]]);
        }
        if (unusedPricing.length > 0) {
            html += '<div class="chart-section"><h3>' + t("analytics.pricingReference") + '</h3>';
            html += '<div class="pricing-meta">';
            html += '<span>' + t("analytics.lastUpdated") + ':</span>';
            html += '<span class="badge">' + escapeHtml(pricing.last_updated || "unknown") + '</span>';
            html += '<span style="margin-left:0.5rem;font-size:0.75rem;color:var(--text-muted)">' + t("analytics.perTokens") + '</span>';
            html += '</div>';
            html += '<table class="data-table"><thead><tr>';
            html += '<th>' + t("analytics.model") + '</th><th>' + t("analytics.input") + '</th><th>' + t("analytics.output") + '</th>';
            html += '</tr></thead><tbody>';
            for (let pi = 0; pi < unusedPricing.length; pi++) {
                const pm = unusedPricing[pi][0];
                const pv = unusedPricing[pi][1];
                const isFree = pv.input === 0 && pv.output === 0;
                html += '<tr>';
                html += '<td style="color:var(--accent);font-weight:600">' + escapeHtml(pm) + '</td>';
                if (isFree) {
                    html += '<td class="free">' + t("common.free") + '</td><td class="free">' + t("common.free") + '</td>';
                } else {
                    html += '<td class="rate">$' + pv.input + '</td>';
                    html += '<td class="rate">$' + pv.output + '</td>';
                }
                html += '</tr>';
            }
            html += '</tbody></table></div>';
        }
    }

    // --- Avg Duration ---
    const durations = usage.avg_duration_ms || {};
    const durList = [];
    for (const dk in durations) { durList.push([dk, durations[dk]]); }
    durList.sort((a, b) => b[1] - a[1]);

    if (durList.length > 0) {
        html += '<div class="chart-section"><h3>' + t("analytics.avgDuration") + '</h3>';
        html += '<table class="data-table"><thead><tr><th>' + t("security.tool") + '</th><th>' + t("analytics.avgMs") + '</th></tr></thead><tbody>';
        for (let di = 0; di < Math.min(durList.length, 10); di++) {
            html += '<tr><td style="color:var(--accent)">' + escapeHtml(durList[di][0]) + '</td><td>' + durList[di][1] + ' ms</td></tr>';
        }
        html += '</tbody></table></div>';
    }

    // --- Smart Suggestions ---
    const sug = suggestions.suggestions || [];
    if (sug.length > 0) {
        html += '<div class="chart-section"><h3>' + t("analytics.smartSuggestions") + '</h3>';
        html += '<div class="suggestions">';
        for (let si = 0; si < sug.length; si++) {
            const sg = sug[si];
            const cat = sg.category || "general";
            html += '<div class="suggestion-card ' + cat + '">';
            html += '<div class="suggestion-header">';
            html += '<span class="suggestion-icon">' + (sg.icon || "") + '</span>';
            html += '<span class="suggestion-title">' + escapeHtml(sg.title) + '</span>';
            html += '<span class="suggestion-priority">' + escapeHtml(sg.priority || "") + '</span>';
            html += '</div>';
            html += '<div class="suggestion-detail">' + escapeHtml(sg.detail) + '</div>';
            html += '</div>';
        }
        html += '</div></div>';
    }

    dashboard.innerHTML = html;
    bindRateInputs();
}

function fetchAndRender() {
    Promise.all([
        fetch("/api/analytics/usage").then(r => r.json()),
        fetch("/api/analytics/costs").then(r => r.json()),
        fetch("/api/analytics/suggestions").then(r => r.json()),
        fetch("/api/analytics/pricing").then(r => r.json()),
    ])
    .then(results => {
        lastResults = results;
        renderAnalytics(results[0], results[1], results[2], results[3]);
    })
    .catch(e => {
        const loading = document.getElementById("loading");
        if (loading) loading.textContent = t("analytics.loadFailed") + ": " + e.message;
    });
}

// === Init ===

async function init() {
    await initLayout({ activePath: "/analytics" });

    fetchAndRender();

    onLocaleChange(() => {
        if (lastResults) {
            renderAnalytics(lastResults[0], lastResults[1], lastResults[2], lastResults[3]);
        }
    });

    KuroPlugins.initAll();
    KuroPlugins.emit("onPageLoad", "analytics");
}

init();
