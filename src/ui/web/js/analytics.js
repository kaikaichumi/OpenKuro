/**
 * Kuro - Analytics Dashboard Module
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml, formatTokens } from "./utils.js";
import KuroPlugins from "./plugins.js";

let lastResults = null;
let saveTimers = {};
let range = defaultRange();
let budgets = [];
let models = [];
let adapters = [];
let targetOptions = [];
let budgetMsg = { text: "", error: false };

function ymd(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function defaultRange() {
    const now = new Date();
    return { start: ymd(new Date(now.getFullYear(), now.getMonth(), 1)), end: ymd(now) };
}
const query = () => {
    const p = new URLSearchParams();
    if (range.start) p.set("start", range.start);
    if (range.end) p.set("end", range.end);
    return p.toString() ? "?" + p.toString() : "";
};

function normalizeRule(raw, i = 0) {
    const x = raw && typeof raw === "object" ? raw : {};
    return {
        id: String(x.id || `rule_${i + 1}`),
        name: String(x.name || ""),
        enabled: x.enabled !== false,
        period: ["daily", "weekly", "monthly"].includes(x.period) ? x.period : "monthly",
        action: ["notify", "stop"].includes(x.action) ? x.action : "notify",
        limit_usd: parseFloat(x.limit_usd) || 0,
        notify_percent: parseFloat(x.notify_percent) || 80,
        models: Array.isArray(x.models) ? x.models.map((v) => String(v || "").trim()).filter(Boolean) : [],
        notify_targets: Array.isArray(x.notify_targets) ? x.notify_targets : [],
        stats: x.stats || null,
        is_blocked_now: !!x.is_blocked_now,
    };
}
const emptyRule = () => normalizeRule({ id: `rule_${Date.now().toString(36)}`, enabled: true, period: "monthly", action: "notify", limit_usd: 0, notify_percent: 80, models: [], notify_targets: [] });

function parseTargetValue(value) {
    const text = String(value || "").trim();
    const idx = text.indexOf(":");
    if (idx <= 0 || idx >= text.length - 1) return null;
    return { adapter: text.slice(0, idx).trim(), user_id: text.slice(idx + 1).trim() };
}
function formatTargetValue(target) {
    const adapter = String(target?.adapter || "").trim();
    const userId = String(target?.user_id || "").trim();
    return adapter && userId ? `${adapter}:${userId}` : "";
}
function normalizeTarget(value) {
    if (value && typeof value === "object") {
        const adapter = String(value.adapter || "").trim();
        const user_id = String(value.user_id || "").trim();
        return {
            adapter,
            user_id,
            label: String(value.label || formatTargetValue({ adapter, user_id })).trim(),
        };
    }
    const parsed = parseTargetValue(value);
    if (!parsed) return { adapter: "", user_id: "", label: "" };
    return { ...parsed, label: formatTargetValue(parsed) };
}
function targetKey(target) {
    return formatTargetValue(target);
}
function dedupeModels(values) {
    return Array.from(new Set((Array.isArray(values) ? values : [])
        .map((v) => String(v || "").trim())
        .filter(Boolean)));
}
function dedupeTargets(values) {
    const seen = new Set();
    const out = [];
    for (const raw of (Array.isArray(values) ? values : [])) {
        const normalized = normalizeTarget(raw);
        if (!normalized.adapter || !normalized.user_id) continue;
        const key = targetKey(normalized);
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({
            adapter: normalized.adapter,
            user_id: normalized.user_id,
            label: normalized.label || key,
        });
    }
    return out;
}
function parseJsonArray(raw) {
    try {
        const parsed = JSON.parse(String(raw || "[]"));
        return Array.isArray(parsed) ? parsed : [];
    } catch {
        return [];
    }
}
function parseModelListFromHidden(inputEl) {
    return dedupeModels(parseJsonArray(inputEl?.value || "[]"));
}
function parseTargetListFromHidden(inputEl) {
    return dedupeTargets(parseJsonArray(inputEl?.value || "[]"));
}
function buildTargetOptionList(existingTargets = []) {
    return dedupeTargets([...(targetOptions || []), ...(existingTargets || [])])
        .sort((a, b) => formatTargetValue(a).localeCompare(formatTargetValue(b)));
}
function resolveTargetLabel(target) {
    const key = targetKey(target);
    const known = dedupeTargets(targetOptions || []).find((item) => targetKey(item) === key);
    if (known?.label) return String(known.label);
    if (target?.label) return String(target.label);
    return key;
}
function renderModelChips(modelsList) {
    const list = dedupeModels(modelsList);
    if (!list.length) {
        return `<span class="budget-chip-empty">${escapeHtml(t("analytics.budgetNoModelsSelected") || "No models selected")}</span>`;
    }
    return list.map((model) => {
        return `<span class="budget-chip"><span class="budget-chip-text">${escapeHtml(model)}</span><button type="button" class="budget-chip-del" data-chip-kind="model" data-chip-value="${escapeHtml(model)}" aria-label="remove">&times;</button></span>`;
    }).join("");
}
function renderTargetChips(targets) {
    const list = dedupeTargets(targets);
    if (!list.length) {
        return `<span class="budget-chip-empty">${escapeHtml(t("analytics.budgetNoTargetsSelected") || "No targets selected")}</span>`;
    }
    return list.map((target) => {
        const value = formatTargetValue(target);
        const label = resolveTargetLabel(target);
        return `<span class="budget-chip"><span class="budget-chip-text">${escapeHtml(label)}</span><button type="button" class="budget-chip-del" data-chip-kind="target" data-chip-value="${escapeHtml(value)}" aria-label="remove">&times;</button></span>`;
    }).join("");
}
function mergeKnownModels(extraModels = []) {
    models = dedupeModels([...(models || []), ...(Array.isArray(extraModels) ? extraModels : [])])
        .sort((a, b) => a.localeCompare(b));
}

async function loadMeta() {
    models = [];
    targetOptions = [];
    let discoveredModels = [];

    try {
        const md = await fetch("/api/models").then((r) => r.json());
        discoveredModels = discoveredModels.concat(Array.isArray(md.available) ? md.available : []);
    } catch {
        // Keep going so we can still merge models from pricing/rules.
    }

    try {
        const pricing = await fetch("/api/analytics/pricing").then((r) => r.json());
        discoveredModels = discoveredModels.concat(Object.keys(pricing?.models || {}));
    } catch {
        // Pricing table is optional for model discovery.
    }

    try {
        const bd = await fetch("/api/analytics/budgets?include_stats=true").then((r) => r.json());
        budgets = Array.isArray(bd.rules) ? bd.rules.map((r, i) => normalizeRule(r, i)) : [];
        adapters = Array.isArray(bd.available_adapters) ? bd.available_adapters : [];
        targetOptions = Array.isArray(bd.target_options) ? dedupeTargets(bd.target_options) : [];
        for (const rule of budgets) {
            discoveredModels = discoveredModels.concat(Array.isArray(rule.models) ? rule.models : []);
        }
    } catch (e) {
        budgets = [];
        adapters = [];
        targetOptions = [];
        budgetMsg = { text: (t("analytics.budgetLoadFailed") || "Failed to load budget rules") + ": " + e.message, error: true };
    }

    mergeKnownModels(discoveredModels);
}

function calcCostDisplay(pt, ct, inputRate, outputRate) {
    const ir = parseFloat(inputRate);
    const or_ = parseFloat(outputRate);
    if (isNaN(ir) && isNaN(or_)) return "N/A";
    const c = (isNaN(ir) ? 0 : pt / 1000 * ir) + (isNaN(or_) ? 0 : ct / 1000 * or_);
    return "$" + c.toFixed(4);
}
function savePricing(model, inputRate, outputRate) {
    if (saveTimers[model]) clearTimeout(saveTimers[model]);
    saveTimers[model] = setTimeout(() => {
        fetch("/api/analytics/pricing/" + encodeURIComponent(model), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ input: parseFloat(inputRate) || 0, output: parseFloat(outputRate) || 0 }),
        }).catch((e) => console.error("save pricing error", e));
    }, 600);
}
function bindRateInputs() {
    const table = document.getElementById("model-cost-table");
    if (!table) return;
    table.addEventListener("input", (e) => {
        if (!e.target.classList.contains("rate-input")) return;
        const tr = e.target.closest("tr");
        if (!tr) return;
        const pt = parseFloat(tr.dataset.pt) || 0;
        const ct = parseFloat(tr.dataset.ct) || 0;
        const ir = tr.querySelector(".input-rate").value;
        const or_ = tr.querySelector(".output-rate").value;
        tr.querySelector(".cost-cell").textContent = calcCostDisplay(pt, ct, ir, or_);
        savePricing(tr.dataset.model, ir, or_);
    });
}

function filterBar(usage) {
    const actual = `${usage?.period_start || range.start || "—"} ~ ${usage?.period_end || range.end || "—"}`;
    return `
    <div class="chart-section analytics-filters">
      <div class="analytics-filters-row">
        <div class="analytics-filters-left">
          <label>${t("analytics.startDate") || "Start date"}<input type="date" id="a-start" value="${escapeHtml(range.start || "")}"></label>
          <label>${t("analytics.endDate") || "End date"}<input type="date" id="a-end" value="${escapeHtml(range.end || "")}"></label>
          <button class="btn btn-sm btn-primary" id="a-apply">${t("analytics.applyFilter") || "Apply"}</button>
          <button class="btn btn-sm btn-secondary" id="a-reset">${t("analytics.resetFilter") || "Reset"}</button>
        </div>
        <div class="analytics-filters-right">${t("analytics.currentRange") || "Range"}: <span class="badge">${escapeHtml(actual)}</span></div>
      </div>
      <div id="a-filter-msg" class="analytics-filter-msg"></div>
    </div>`;
}

function budgetSection() {
    const adapterHint = `${t("analytics.budgetAdapterHint") || "Available adapters"}: ${adapters.length ? escapeHtml(adapters.join(", ")) : "-"}`;
    const status = budgetMsg.text ? `<div class="budget-status ${budgetMsg.error ? "error" : "ok"}">${escapeHtml(budgetMsg.text)}</div>` : `<div class="budget-status"></div>`;
    const cards = budgets.length ? budgets.map((rule, i) => {
        const r = normalizeRule(rule, i);
        const selectedModels = dedupeModels(r.models || []);
        const allModels = selectedModels.length === 0;
        const modelOptions = dedupeModels([...(models || []), ...selectedModels]).sort((a, b) => a.localeCompare(b));
        const modelSelectOptions = [`<option value="">${escapeHtml(t("analytics.budgetSelectModel") || "Select model")}</option>`]
            .concat(modelOptions.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`))
            .join("");
        const selectedTargets = dedupeTargets(r.notify_targets || []).map((target) => ({
            adapter: target.adapter,
            user_id: target.user_id,
            label: resolveTargetLabel(target),
        }));
        const targetOptionList = buildTargetOptionList(selectedTargets);
        const targetSelectOptions = [`<option value="">${escapeHtml(t("analytics.budgetSelectTarget") || "Select target")}</option>`]
            .concat(targetOptionList.map((target) => {
                const value = formatTargetValue(target);
                const label = resolveTargetLabel(target);
                return `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`;
            }))
            .join("");
        const s = r.stats || {};
        const usage = Number.isFinite(parseFloat(s.usage_percent)) ? parseFloat(s.usage_percent).toFixed(2) : "0.00";
        return `<div class="budget-rule-card" data-id="${escapeHtml(r.id)}">
          <div class="budget-rule-header">
            <strong>${r.name ? escapeHtml(r.name) : (t("analytics.budgetRuleName") || "Rule name")}</strong>
            <button class="btn btn-sm btn-danger" data-del="${escapeHtml(r.id)}">${t("common.delete") || "Delete"}</button>
          </div>
          <div class="budget-grid">
            <label>${t("analytics.budgetEnabled") || "Enabled"}<input type="checkbox" class="b-enabled" ${r.enabled ? "checked" : ""}></label>
            <label>${t("analytics.budgetRuleName") || "Rule name"}<input type="text" class="b-name" value="${escapeHtml(r.name)}"></label>
            <label>${t("analytics.budgetPeriod") || "Period"}<select class="b-period"><option value="daily"${r.period === "daily" ? " selected" : ""}>${t("analytics.budgetPeriodDaily") || "Daily"}</option><option value="weekly"${r.period === "weekly" ? " selected" : ""}>${t("analytics.budgetPeriodWeekly") || "Weekly"}</option><option value="monthly"${r.period === "monthly" ? " selected" : ""}>${t("analytics.budgetPeriodMonthly") || "Monthly"}</option></select></label>
            <label>${t("analytics.budgetAction") || "Action"}<select class="b-action"><option value="notify"${r.action === "notify" ? " selected" : ""}>${t("analytics.budgetActionNotify") || "Notify"}</option><option value="stop"${r.action === "stop" ? " selected" : ""}>${t("analytics.budgetActionStop") || "Stop"}</option></select></label>
            <label>${t("analytics.budgetLimitUsd") || "Limit (USD)"}<input type="number" min="0" step="0.0001" class="b-limit" value="${escapeHtml(r.limit_usd)}"></label>
            <label>${t("analytics.budgetNotifyPercent") || "Notify at (%)"}<input type="number" min="1" max="100" step="0.1" class="b-notify" value="${escapeHtml(r.notify_percent)}"></label>
            <label class="budget-models-label">${t("analytics.budgetModels") || "Models"}
              <div class="budget-picker">
                <label class="budget-inline-check"><input type="checkbox" class="b-all-models"${allModels ? " checked" : ""}>${t("analytics.budgetAllModels") || "All models"}</label>
                <div class="budget-picker-row">
                  <select class="b-model-select"${allModels ? " disabled" : ""}>${modelSelectOptions}</select>
                  <button type="button" class="btn btn-sm btn-secondary b-model-add"${allModels ? " disabled" : ""}>${t("analytics.budgetAddModel") || "Add model"}</button>
                </div>
                <input type="hidden" class="b-models-hidden" value="${escapeHtml(JSON.stringify(selectedModels))}">
                <div class="budget-chip-list b-model-chip-list">${renderModelChips(selectedModels)}</div>
                <small>${t("analytics.budgetModelsHint") || "Pick from dropdown and add. Choose all models to apply globally."}</small>
              </div>
            </label>
            <label class="budget-targets-label">${t("analytics.budgetTargets") || "Notify targets"}
              <div class="budget-picker">
                <div class="budget-picker-row">
                  <select class="b-target-select">${targetSelectOptions}</select>
                  <button type="button" class="btn btn-sm btn-secondary b-target-add">${t("analytics.budgetAddTarget") || "Add target"}</button>
                </div>
                <input type="hidden" class="b-targets-hidden" value="${escapeHtml(JSON.stringify(selectedTargets))}">
                <div class="budget-chip-list b-target-chip-list">${renderTargetChips(selectedTargets)}</div>
                <small>${t("analytics.budgetTargetsHint") || "Pick from dropdown and add. You can select multiple targets."}</small>
              </div>
            </label>
          </div>
          <div class="budget-rule-stats"><span>${t("analytics.budgetStats") || "Current usage"}: $${(parseFloat(s.spent_usd) || 0).toFixed(4)} / $${(parseFloat(r.limit_usd) || 0).toFixed(4)} (${usage}%)</span>${r.is_blocked_now ? `<span class="badge budget-badge-stop">${t("analytics.budgetBlockedNow") || "Blocked now"}</span>` : ""}</div>
        </div>`;
    }).join("") : `<div class="budget-empty">${t("analytics.budgetNoRules") || "No budget rules yet."}</div>`;
    return `<div class="chart-section budget-section">
      <h3>${t("analytics.budgetTitle") || "Budget Guardrails"}</h3>
      <div class="budget-desc">${t("analytics.budgetDesc") || "Configure notify/hard-stop budget rules by period and model."}</div>
      <div class="budget-adapter-hint">${adapterHint}</div>
      ${status}
      <div class="budget-toolbar"><button class="btn btn-sm btn-secondary" id="b-add">${t("analytics.budgetAddRule") || "Add Rule"}</button><button class="btn btn-sm btn-primary" id="b-save">${t("analytics.budgetSave") || "Save Rules"}</button></div>
      <div class="budget-rules">${cards}</div>
    </div>`;
}

function render(usage, costs, suggestions, pricing) {
    const dashboard = document.getElementById("dashboard");
    const loading = document.getElementById("loading");
    if (loading) loading.style.display = "none";

    const byModel = costs.by_model || {};
    const rows = Object.entries(byModel);
    rows.sort((a, b) => (b[1].total_tokens || 0) - (a[1].total_tokens || 0));
    mergeKnownModels(rows.map(([name]) => name));
    mergeKnownModels(Object.keys(pricing?.models || {}));
    const modelTable = rows.length ? `<div class="table-scroll"><table class="data-table" id="model-cost-table"><thead><tr><th>${t("analytics.model")}</th><th>${t("analytics.calls")}</th><th>${t("analytics.promptTokens")}</th><th>${t("analytics.completionTokens")}</th><th>${t("analytics.totalTokens")}</th><th>${t("analytics.inputRate")}</th><th>${t("analytics.outputRate")}</th><th>${t("analytics.estCost")}</th></tr></thead><tbody>${
        rows.map(([name, info]) => {
            const ir = info?.pricing?.input ?? "";
            const or_ = info?.pricing?.output ?? "";
            const pt = info.prompt_tokens || 0;
            const ct = info.completion_tokens || 0;
            return `<tr data-model="${escapeHtml(name)}" data-pt="${pt}" data-ct="${ct}"><td class="model-name">${escapeHtml(name)}</td><td>${info.calls || 0}</td><td>${formatTokens(pt)}</td><td>${formatTokens(ct)}</td><td>${formatTokens(info.total_tokens || 0)}</td><td class="rate"><span class="rate-prefix">$</span><input class="rate-input input-rate" type="number" step="any" min="0" value="${ir}"><span class="rate-suffix">/1K</span></td><td class="rate"><span class="rate-prefix">$</span><input class="rate-input output-rate" type="number" step="any" min="0" value="${or_}"><span class="rate-suffix">/1K</span></td><td class="cost-val cost-cell">${calcCostDisplay(pt, ct, ir, or_)}</td></tr>`;
        }).join("")
    }</tbody></table></div>` : `<div style="color:var(--text-muted);font-size:0.85rem">${t("analytics.noTokenUsage")}</div>`;
    const tools = Object.entries(usage.tool_counts || {}).sort((a, b) => b[1] - a[1]);
    const maxTool = tools.length ? tools[0][1] : 1;
    const toolBars = tools.length ? tools.slice(0, 10).map(([name, count]) => `<div class="usage-bar-row"><span class="usage-bar-label">${escapeHtml(name)}</span><div class="usage-bar-track"><div class="usage-bar-fill" style="width:${Math.round(count / maxTool * 100)}%"></div></div><span class="usage-bar-count">${count}</span></div>`).join("") : `<div style="color:var(--text-muted);font-size:0.85rem">${t("analytics.noToolUsage")}</div>`;

    const daily = usage.daily_activity || [];
    const maxDaily = daily.length ? Math.max.apply(null, daily.map((x) => x.count).concat([1])) : 1;
    const dailyHtml = daily.length
        ? `<div class="chart-section"><h3>${t("analytics.dailyActivity")} <span class="badge">${escapeHtml((usage.period_start || "") + " ~ " + (usage.period_end || ""))}</span></h3><div class="bar-chart tall">${
            daily.map((d) => `<div class="bar" style="height:${Math.max(Math.round((d.count || 0) / maxDaily * 100), 3)}%" data-tooltip="${escapeHtml(d.date)}: ${d.count}"></div>`).join("")
        }</div></div>`
        : "";

    const usedSet = new Set(rows.map(([name]) => name));
    const pricingRows = pricing && pricing.models
        ? Object.entries(pricing.models).filter(([name]) => !usedSet.has(name))
        : [];
    const pricingHtml = pricingRows.length
        ? `<div class="chart-section"><h3>${t("analytics.pricingReference")}</h3><div class="pricing-meta"><span>${t("analytics.lastUpdated")}:</span><span class="badge">${escapeHtml(pricing.last_updated || "unknown")}</span><span style="margin-left:0.5rem;font-size:0.75rem;color:var(--text-muted)">${t("analytics.perTokens")}</span></div><table class="data-table"><thead><tr><th>${t("analytics.model")}</th><th>${t("analytics.input")}</th><th>${t("analytics.output")}</th></tr></thead><tbody>${
            pricingRows.map(([name, info]) => {
                if (info && info.input === 0 && info.output === 0) {
                    return `<tr><td style="color:var(--accent);font-weight:600">${escapeHtml(name)}</td><td class="free">${t("common.free")}</td><td class="free">${t("common.free")}</td></tr>`;
                }
                return `<tr><td style="color:var(--accent);font-weight:600">${escapeHtml(name)}</td><td class="rate">$${info?.input ?? "-"}</td><td class="rate">$${info?.output ?? "-"}</td></tr>`;
            }).join("")
        }</tbody></table></div>`
        : "";

    const durations = Object.entries(usage.avg_duration_ms || {}).sort((a, b) => b[1] - a[1]);
    const durationHtml = durations.length
        ? `<div class="chart-section"><h3>${t("analytics.avgDuration")}</h3><table class="data-table"><thead><tr><th>${t("security.tool")}</th><th>${t("analytics.avgMs")}</th></tr></thead><tbody>${
            durations.slice(0, 10).map(([name, ms]) => `<tr><td style="color:var(--accent)">${escapeHtml(name)}</td><td>${ms} ms</td></tr>`).join("")
        }</tbody></table></div>`
        : "";

    const sug = (suggestions.suggestions || []).map((s) => `<div class="suggestion-card ${escapeHtml(s.category || "general")}"><div class="suggestion-header"><span class="suggestion-icon">${s.icon || ""}</span><span class="suggestion-title">${escapeHtml(s.title || "")}</span><span class="suggestion-priority">${escapeHtml(s.priority || "")}</span></div><div class="suggestion-detail">${escapeHtml(s.detail || "")}</div></div>`).join("");

    dashboard.innerHTML = `
      ${filterBar(usage)}
      ${budgetSection()}
      <div class="stats-grid">
        <div class="stat-card"><div class="stat-value blue">${usage.total_calls || 0}</div><div class="stat-label">${t("analytics.toolCalls") || t("analytics.toolCalls30d")}</div></div>
        <div class="stat-card"><div class="stat-value yellow">$${(costs.total_estimated_cost_usd || 0).toFixed(2)}</div><div class="stat-label">${t("analytics.estCost") || t("analytics.estCost30d")}</div></div>
        <div class="stat-card"><div class="stat-value blue">${formatTokens(costs.total_tokens || 0)}</div><div class="stat-label">${t("analytics.totalTokens") || t("analytics.totalTokens30d")}</div></div>
        <div class="stat-card"><div class="stat-value ${(usage.error_rate || 0) > 5 ? "red" : "green"}">${usage.error_rate || 0}%</div><div class="stat-label">${t("analytics.errorRate")}</div></div>
      </div>
      <div class="chart-section"><h3>${t("analytics.mostUsedTools")}</h3><div class="usage-bars">${toolBars}</div></div>
      <div class="chart-section"><h3>${t("analytics.tokenUsageCost")}</h3>${modelTable}</div>
      ${dailyHtml}
      ${pricingHtml}
      ${durationHtml}
      <div class="chart-section"><h3>${t("analytics.smartSuggestions")}</h3><div class="suggestions">${sug || ""}</div></div>
    `;

    bindRateInputs();
    bindFilter();
    bindBudget();
}

function setFilterMsg(msg, err = false) {
    const el = document.getElementById("a-filter-msg");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "analytics-filter-msg" + (err ? " error" : "");
}
function bindFilter() {
    const start = document.getElementById("a-start");
    const end = document.getElementById("a-end");
    const apply = document.getElementById("a-apply");
    const reset = document.getElementById("a-reset");
    if (!start || !end || !apply || !reset) return;
    apply.onclick = () => {
        if (!start.value || !end.value || start.value > end.value) {
            setFilterMsg(t("analytics.rangeInvalid") || "Invalid date range", true);
            return;
        }
        range = { start: start.value, end: end.value };
        setFilterMsg("");
        fetchAndRender();
    };
    reset.onclick = () => {
        range = defaultRange();
        setFilterMsg("");
        fetchAndRender();
    };
}

function setCardModels(card, modelList) {
    const hidden = card.querySelector(".b-models-hidden");
    const chips = card.querySelector(".b-model-chip-list");
    const normalized = dedupeModels(modelList);
    if (hidden) hidden.value = JSON.stringify(normalized);
    if (chips) chips.innerHTML = renderModelChips(normalized);
}
function setCardTargets(card, targets) {
    const hidden = card.querySelector(".b-targets-hidden");
    const chips = card.querySelector(".b-target-chip-list");
    const normalized = dedupeTargets(targets).map((target) => ({
        adapter: target.adapter,
        user_id: target.user_id,
        label: resolveTargetLabel(target),
    }));
    if (hidden) hidden.value = JSON.stringify(normalized);
    if (chips) chips.innerHTML = renderTargetChips(normalized);
}
function syncCardModelControls(card) {
    const allModels = card.querySelector(".b-all-models")?.checked === true;
    const modelSelect = card.querySelector(".b-model-select");
    const addBtn = card.querySelector(".b-model-add");
    if (modelSelect) modelSelect.disabled = allModels;
    if (addBtn) addBtn.disabled = allModels;
}

function collectBudgetRules() {
    return Array.from(document.querySelectorAll(".budget-rule-card")).map((card, i) => normalizeRule({
        id: card.dataset.id || `rule_${i + 1}`,
        name: card.querySelector(".b-name")?.value || "",
        enabled: card.querySelector(".b-enabled")?.checked === true,
        period: card.querySelector(".b-period")?.value || "monthly",
        action: card.querySelector(".b-action")?.value || "notify",
        limit_usd: parseFloat(card.querySelector(".b-limit")?.value || "0") || 0,
        notify_percent: parseFloat(card.querySelector(".b-notify")?.value || "80") || 80,
        models: card.querySelector(".b-all-models")?.checked === true
            ? []
            : parseModelListFromHidden(card.querySelector(".b-models-hidden")),
        notify_targets: parseTargetListFromHidden(card.querySelector(".b-targets-hidden"))
            .map((target) => ({ adapter: target.adapter, user_id: target.user_id })),
    }, i));
}
async function saveBudgets() {
    const btn = document.getElementById("b-save");
    if (btn) btn.disabled = true;
    try {
        const res = await fetch("/api/analytics/budgets", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rules: collectBudgetRules() }),
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        budgets = Array.isArray(data.rules) ? data.rules.map((r, i) => normalizeRule(r, i)) : [];
        adapters = Array.isArray(data.available_adapters) ? data.available_adapters : adapters;
        targetOptions = Array.isArray(data.target_options) ? dedupeTargets(data.target_options) : targetOptions;
        budgetMsg = { text: t("analytics.budgetSaved") || "Budget rules saved", error: false };
    } catch (e) {
        budgetMsg = { text: (t("analytics.budgetSaveFailed") || "Failed to save budget rules") + ": " + e.message, error: true };
    } finally {
        if (btn) btn.disabled = false;
        if (lastResults) render(lastResults[0], lastResults[1], lastResults[2], lastResults[3]);
    }
}
function bindBudget() {
    const add = document.getElementById("b-add");
    const save = document.getElementById("b-save");
    if (add) add.onclick = () => {
        budgets.push(emptyRule());
        budgetMsg = { text: "", error: false };
        if (lastResults) render(lastResults[0], lastResults[1], lastResults[2], lastResults[3]);
    };
    if (save) save.onclick = saveBudgets;
    document.querySelectorAll("[data-del]").forEach((btn) => {
        btn.onclick = () => {
            budgets = budgets.filter((r) => r.id !== btn.getAttribute("data-del"));
            budgetMsg = { text: "", error: false };
            if (lastResults) render(lastResults[0], lastResults[1], lastResults[2], lastResults[3]);
        };
    });

    const rulesWrap = document.querySelector(".budget-rules");
    if (!rulesWrap) return;

    rulesWrap.querySelectorAll(".budget-rule-card").forEach((card) => {
        setCardModels(card, parseModelListFromHidden(card.querySelector(".b-models-hidden")));
        setCardTargets(card, parseTargetListFromHidden(card.querySelector(".b-targets-hidden")));
        syncCardModelControls(card);
    });

    rulesWrap.addEventListener("change", (event) => {
        const target = event.target;
        if (!(target instanceof HTMLElement)) return;
        if (!target.classList.contains("b-all-models")) return;
        const card = target.closest(".budget-rule-card");
        if (!card) return;
        syncCardModelControls(card);
    });

    rulesWrap.addEventListener("click", (event) => {
        const button = event.target instanceof HTMLElement ? event.target.closest("button") : null;
        if (!button) return;
        const card = button.closest(".budget-rule-card");
        if (!card) return;

        if (button.classList.contains("b-model-add")) {
            const select = card.querySelector(".b-model-select");
            const value = String(select?.value || "").trim();
            if (!value) return;
            const modelsList = parseModelListFromHidden(card.querySelector(".b-models-hidden"));
            if (!modelsList.includes(value)) {
                modelsList.push(value);
                setCardModels(card, modelsList);
            }
            if (select) select.value = "";
            return;
        }

        if (button.classList.contains("b-target-add")) {
            const select = card.querySelector(".b-target-select");
            const value = String(select?.value || "").trim();
            const parsed = parseTargetValue(value);
            if (!parsed) return;
            const selectedOption = select && "selectedOptions" in select ? select.selectedOptions[0] : null;
            const selectedLabel = String(selectedOption?.textContent || "").trim();
            const targetList = parseTargetListFromHidden(card.querySelector(".b-targets-hidden"));
            const key = targetKey(parsed);
            if (!targetList.some((item) => targetKey(item) === key)) {
                targetList.push({
                    adapter: parsed.adapter,
                    user_id: parsed.user_id,
                    label: selectedLabel || resolveTargetLabel(parsed),
                });
                setCardTargets(card, targetList);
            }
            if (select) select.value = "";
            return;
        }

        if (button.classList.contains("budget-chip-del")) {
            const chipKind = String(button.getAttribute("data-chip-kind") || "");
            const chipValue = String(button.getAttribute("data-chip-value") || "");
            if (!chipValue) return;
            if (chipKind === "model") {
                const modelsList = parseModelListFromHidden(card.querySelector(".b-models-hidden"))
                    .filter((item) => item !== chipValue);
                setCardModels(card, modelsList);
                return;
            }
            if (chipKind === "target") {
                const targetList = parseTargetListFromHidden(card.querySelector(".b-targets-hidden"))
                    .filter((item) => targetKey(item) !== chipValue);
                setCardTargets(card, targetList);
            }
        }
    });
}

function fetchAndRender() {
    Promise.all([
        fetch("/api/analytics/usage" + query()).then((r) => r.json()),
        fetch("/api/analytics/costs" + query()).then((r) => r.json()),
        fetch("/api/analytics/suggestions").then((r) => r.json()),
        fetch("/api/analytics/pricing").then((r) => r.json()),
    ]).then((res) => {
        lastResults = res;
        render(res[0], res[1], res[2], res[3]);
    }).catch((e) => {
        const loading = document.getElementById("loading");
        if (loading) loading.textContent = t("analytics.loadFailed") + ": " + e.message;
    });
}

async function init() {
    await initLayout({ activePath: "/analytics" });
    await loadMeta();
    fetchAndRender();
    onLocaleChange(() => {
        if (lastResults) render(lastResults[0], lastResults[1], lastResults[2], lastResults[3]);
    });
    KuroPlugins.initAll();
    KuroPlugins.emit("onPageLoad", "analytics");
}

init();
