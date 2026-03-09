/**
 * Kuro - Config Page Module
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml, showToast } from "./utils.js";
import KuroPlugins from "./plugins.js";

let isDirty = false;
let currentConfig = {};

function _addModel(list, seen, rawModel) {
    const model = String(rawModel || "").trim();
    if (!model || seen.has(model)) return;
    seen.add(model);
    list.push(model);
}

/**
 * Build model options from config + runtime model catalog.
 * Returns { models: string[], oauthModels: Set<string> }.
 */
async function getModelOptions(cfg) {
    const models = [];
    const seen = new Set();
    const oauthModels = new Set();

    const providers = (cfg.models && cfg.models.providers) || {};
    for (const [, providerCfg] of Object.entries(providers)) {
        if (Array.isArray(providerCfg.known_models)) {
            for (const m of providerCfg.known_models) {
                _addModel(models, seen, m);
            }
        }
    }

    try {
        const resp = await fetch("/api/models");
        if (resp.ok) {
            const data = await resp.json();
            const groups = data.groups || {};
            for (const arr of Object.values(groups)) {
                if (!Array.isArray(arr)) continue;
                for (const m of arr) {
                    _addModel(models, seen, m);
                }
            }

            const catalog = Array.isArray(data.catalog) ? data.catalog : [];
            for (const item of catalog) {
                const model = String(item && item.model ? item.model : "").trim();
                if (!model) continue;
                _addModel(models, seen, model);
                if (item && item.auth === "oauth") {
                    oauthModels.add(model);
                }
            }

            const available = Array.isArray(data.available) ? data.available : [];
            for (const m of available) {
                _addModel(models, seen, m);
            }
        }
    } catch (e) {
        // Fallback to config-known models only.
    }

    return { models, oauthModels };
}

/**
 * Populate a <select> element with model options.
 * Includes an empty "Auto / Default" option at the top.
 */
function populateModelSelect(selectId, models, currentValue, oauthModels = new Set()) {
    const el = document.getElementById(selectId);
    if (!el) return;

    // Preserve current value
    const val = currentValue || "";

    // Clear existing options
    el.innerHTML = "";

    // Add empty/auto option
    const autoOpt = document.createElement("option");
    autoOpt.value = "";
    autoOpt.textContent = t("config.modelAuto") || "Auto / Default";
    el.appendChild(autoOpt);

    // Add known models
    for (const model of models) {
        const opt = document.createElement("option");
        opt.value = model;
        opt.textContent = oauthModels.has(model) ? (model + " [OAuth]") : model;
        el.appendChild(opt);
    }

    // If current value is not in the list but is non-empty, add it as a custom option
    if (val && !models.includes(val)) {
        const customOpt = document.createElement("option");
        customOpt.value = val;
        customOpt.textContent = val + " (custom)";
        el.appendChild(customOpt);
    }

    el.value = val;
}

/**
 * Populate diagnostics repair model selector with explicit API/OAuth variants.
 * Stores OAuth choice as `oauth:<model>` so backend can preserve auth mode.
 */
function populateRepairModelSelect(selectId, models, currentValue, oauthModels = new Set()) {
    const el = document.getElementById(selectId);
    if (!el) return;

    const val = currentValue || "main";
    el.innerHTML = "";

    const autoOpt = document.createElement("option");
    autoOpt.value = "";
    autoOpt.textContent = t("config.modelAuto") || "Auto / Default";
    el.appendChild(autoOpt);

    for (const model of models) {
        if (!model) continue;
        if (model === "main") {
            const opt = document.createElement("option");
            opt.value = "main";
            opt.textContent = "main";
            el.appendChild(opt);
            continue;
        }
        if (oauthModels.has(model)) {
            const apiOpt = document.createElement("option");
            apiOpt.value = model;
            apiOpt.textContent = model + " [API]";
            el.appendChild(apiOpt);

            const oauthOpt = document.createElement("option");
            oauthOpt.value = "oauth:" + model;
            oauthOpt.textContent = model + " [OAuth]";
            el.appendChild(oauthOpt);
            continue;
        }
        const opt = document.createElement("option");
        opt.value = model;
        opt.textContent = model;
        el.appendChild(opt);
    }

    const hasValue = Array.from(el.options).some((o) => o.value === val);
    if (val && !hasValue) {
        const customOpt = document.createElement("option");
        customOpt.value = val;
        customOpt.textContent = val + " (custom)";
        el.appendChild(customOpt);
    }

    el.value = val;
}

function markDirty() {
    isDirty = true;
    document.getElementById("save-btn").disabled = false;
    document.getElementById("save-status").textContent = t("config.unsaved");
    updateBadges();
}

function updateBadges() {
    setBadge("badge-full-access", document.getElementById("sec-full-access").checked);
    setBadge("badge-compression", document.getElementById("cc-enabled").checked);
    setBadge("badge-lifecycle", document.getElementById("ml-enabled").checked);
    setBadge("badge-learning", document.getElementById("le-enabled").checked);
    setBadge("badge-code", document.getElementById("cf-enabled").checked);
    setBadge("badge-vision", document.getElementById("vi-mode").value !== "disabled");
    setBadge("badge-diagnostics", document.getElementById("diag-enabled").checked);
    setBadge("badge-complexity", document.getElementById("tc-enabled").checked);
    setBadge("badge-ml", document.getElementById("tc-ml-enabled").checked);
    setBadge("badge-delegation-complexity", document.getElementById("dc-enabled").checked);
}

function setBadge(id, enabled) {
    const el = document.getElementById(id);
    el.textContent = enabled ? t("common.on") : t("common.off");
    el.className = "badge" + (enabled ? "" : " off");
}

// Load config from API
async function loadConfig() {
    try {
        const resp = await fetch("/api/config");
        const data = await resp.json();
        currentConfig = data.config;
        const modelOptions = await getModelOptions(currentConfig);
        populateForm(currentConfig, modelOptions);
    } catch (e) {
        showToast(t("config.loadFailed"), "error");
    }
}

function populateForm(cfg, modelOptions) {
    const sec = cfg.security || {};
    document.getElementById("sec-full-access").checked = sec.full_access_mode === true;

    const cc = cfg.context_compression || {};
    document.getElementById("cc-enabled").checked = cc.enabled !== false;
    document.getElementById("cc-budget").value = cc.token_budget || 100000;
    document.getElementById("cc-threshold").value = cc.trigger_threshold || 0.8;
    document.getElementById("cc-recent").value = cc.keep_recent_turns || 10;
    document.getElementById("cc-model").value = cc.summarize_model || "gemini/gemini-2.0-flash";
    document.getElementById("cc-extract").checked = cc.extract_facts !== false;

    const ml = cfg.memory_lifecycle || {};
    document.getElementById("ml-enabled").checked = ml.enabled !== false;
    document.getElementById("ml-decay").value = ml.decay_lambda || 0.01;
    document.getElementById("ml-prune").value = ml.prune_threshold || 0.1;
    document.getElementById("ml-consolidation").value = ml.consolidation_distance || 0.15;
    document.getElementById("ml-daily-time").value = ml.daily_maintenance_time || "03:00";
    document.getElementById("ml-md-max").value = ml.memory_md_max_lines || 200;
    document.getElementById("ml-pin").checked = ml.pin_user_memories !== false;

    const le = cfg.learning || {};
    document.getElementById("le-enabled").checked = le.enabled !== false;
    document.getElementById("le-max").value = le.max_lessons || 20;
    document.getElementById("le-topk").value = le.inject_top_k || 5;
    document.getElementById("le-err").value = le.error_threshold || 3;
    document.getElementById("le-time").value = le.analysis_time || "04:00";
    document.getElementById("le-track").checked = le.track_model_performance !== false;

    const cf = cfg.code_feedback || {};
    document.getElementById("cf-enabled").checked = cf.enabled === true;
    document.getElementById("cf-lint").checked = cf.lint_on_write !== false;
    document.getElementById("cf-type").checked = cf.type_check_on_write === true;
    document.getElementById("cf-test").checked = cf.test_on_write === true;
    document.getElementById("cf-rounds").value = cf.max_auto_fix_rounds || 3;

    const vi = cfg.vision || {};
    document.getElementById("vi-mode").value = vi.image_analysis_mode || "auto";
    document.getElementById("vi-format").value = vi.fallback_format || "text";
    document.getElementById("vi-detail").value = vi.fallback_detail_level || "standard";
    document.getElementById("vi-grid").value = vi.grid_size || 4;
    document.getElementById("vi-max-elements").value = vi.max_elements || 50;
    document.getElementById("vi-vision-models").value = (vi.vision_models || []).join(", ");
    document.getElementById("vi-text-only-models").value = (vi.text_only_models || []).join(", ");

    const diag = cfg.diagnostics || {};
    document.getElementById("diag-enabled").checked = diag.enabled !== false;
    document.getElementById("diag-auto").checked = diag.auto_diagnose_on_error !== false;
    document.getElementById("diag-error-threshold").value = diag.error_threshold || 3;
    document.getElementById("diag-agents").checked = diag.include_in_agents !== false;
    document.getElementById("diag-matching").checked = diag.only_matching_model === true;

    // Populate model dropdowns from config + runtime model catalog.
    const knownModels = (modelOptions && Array.isArray(modelOptions.models))
        ? modelOptions.models
        : [];
    const oauthModels = (modelOptions && modelOptions.oauthModels instanceof Set)
        ? modelOptions.oauthModels
        : new Set();

    // Diagnostics repair model: "main" is a special value + known models
    const repairModels = ["main", ...knownModels];
    populateRepairModelSelect(
        "diag-repair-model",
        repairModels,
        diag.repair_model || "main",
        oauthModels,
    );

    const tc = cfg.task_complexity || {};
    document.getElementById("tc-enabled").checked = tc.enabled !== false;
    document.getElementById("tc-trigger").value = tc.trigger_mode || "auto";
    document.getElementById("tc-llm-refine").checked = tc.llm_refinement !== false;

    populateModelSelect("tc-refine-model", knownModels, tc.refinement_model || "", oauthModels);
    populateModelSelect("tc-fast-model", knownModels, tc.fast_model || "", oauthModels);
    populateModelSelect("tc-standard-model", knownModels, tc.standard_model || "", oauthModels);
    populateModelSelect("tc-frontier-model", knownModels, tc.frontier_model || "", oauthModels);

    document.getElementById("tc-decompose").checked = tc.decomposition_enabled !== false;
    document.getElementById("tc-decompose-threshold").value = tc.decomposition_threshold || 0.80;
    document.getElementById("tc-max-subtasks").value = tc.max_subtasks || 5;
    document.getElementById("tc-parallel").checked = tc.parallel_subtasks !== false;

    // ML model settings
    document.getElementById("tc-ml-enabled").checked = tc.ml_model_enabled === true;
    document.getElementById("tc-ml-mode").value = tc.ml_estimation_mode || "hybrid";
    document.getElementById("tc-ml-model-path").value = tc.ml_model_path || "";
    document.getElementById("tc-ml-tokenizer-path").value = tc.ml_tokenizer_path || "";

    const dc = cfg.delegation_complexity || {};
    const dct = dc.tier_boundaries || {};
    const dcm = dc.tier_models || {};
    document.getElementById("dc-enabled").checked = dc.enabled === true;
    document.getElementById("dc-default-use").checked = dc.default_use_complexity === true;
    document.getElementById("dc-auto-select").checked = dc.allow_auto_select !== false;
    document.getElementById("dc-enforce-tier").checked = dc.enforce_min_tier !== false;
    populateModelSelect("dc-model-trivial", knownModels, dcm.trivial || "", oauthModels);
    populateModelSelect("dc-model-simple", knownModels, dcm.simple || "", oauthModels);
    populateModelSelect("dc-model-moderate", knownModels, dcm.moderate || "", oauthModels);
    populateModelSelect("dc-model-complex", knownModels, dcm.complex || "", oauthModels);
    document.getElementById("dc-tier-trivial").value = dct.trivial ?? 0.15;
    document.getElementById("dc-tier-simple").value = dct.simple ?? 0.35;
    document.getElementById("dc-tier-moderate").value = dct.moderate ?? 0.60;
    document.getElementById("dc-tier-complex").value = dct.complex ?? 0.85;

    updateBadges();
}

function collectForm() {
    const dcTrivial = parseFloat(document.getElementById("dc-tier-trivial").value);
    const dcSimple = parseFloat(document.getElementById("dc-tier-simple").value);
    const dcModerate = parseFloat(document.getElementById("dc-tier-moderate").value);
    const dcComplex = parseFloat(document.getElementById("dc-tier-complex").value);

    return {
        security: {
            full_access_mode: document.getElementById("sec-full-access").checked,
        },
        context_compression: {
            enabled: document.getElementById("cc-enabled").checked,
            token_budget: parseInt(document.getElementById("cc-budget").value),
            trigger_threshold: parseFloat(document.getElementById("cc-threshold").value),
            keep_recent_turns: parseInt(document.getElementById("cc-recent").value),
            summarize_model: document.getElementById("cc-model").value,
            extract_facts: document.getElementById("cc-extract").checked,
        },
        memory_lifecycle: {
            enabled: document.getElementById("ml-enabled").checked,
            decay_lambda: parseFloat(document.getElementById("ml-decay").value),
            prune_threshold: parseFloat(document.getElementById("ml-prune").value),
            consolidation_distance: parseFloat(document.getElementById("ml-consolidation").value),
            daily_maintenance_time: document.getElementById("ml-daily-time").value,
            memory_md_max_lines: parseInt(document.getElementById("ml-md-max").value),
            pin_user_memories: document.getElementById("ml-pin").checked,
        },
        learning: {
            enabled: document.getElementById("le-enabled").checked,
            max_lessons: parseInt(document.getElementById("le-max").value),
            inject_top_k: parseInt(document.getElementById("le-topk").value),
            error_threshold: parseInt(document.getElementById("le-err").value),
            analysis_time: document.getElementById("le-time").value,
            track_model_performance: document.getElementById("le-track").checked,
        },
        code_feedback: {
            enabled: document.getElementById("cf-enabled").checked,
            lint_on_write: document.getElementById("cf-lint").checked,
            type_check_on_write: document.getElementById("cf-type").checked,
            test_on_write: document.getElementById("cf-test").checked,
            max_auto_fix_rounds: parseInt(document.getElementById("cf-rounds").value),
        },
        vision: {
            image_analysis_mode: document.getElementById("vi-mode").value,
            fallback_format: document.getElementById("vi-format").value,
            fallback_detail_level: document.getElementById("vi-detail").value,
            grid_size: parseInt(document.getElementById("vi-grid").value),
            max_elements: parseInt(document.getElementById("vi-max-elements").value),
            vision_models: document.getElementById("vi-vision-models").value.split(",").map(s => s.trim()).filter(Boolean),
            text_only_models: document.getElementById("vi-text-only-models").value.split(",").map(s => s.trim()).filter(Boolean),
        },
        diagnostics: {
            enabled: document.getElementById("diag-enabled").checked,
            repair_model: document.getElementById("diag-repair-model").value || "main",
            auto_diagnose_on_error: document.getElementById("diag-auto").checked,
            error_threshold: parseInt(document.getElementById("diag-error-threshold").value),
            include_in_agents: document.getElementById("diag-agents").checked,
            only_matching_model: document.getElementById("diag-matching").checked,
        },
        task_complexity: {
            enabled: document.getElementById("tc-enabled").checked,
            trigger_mode: document.getElementById("tc-trigger").value,
            llm_refinement: document.getElementById("tc-llm-refine").checked,
            refinement_model: document.getElementById("tc-refine-model").value,
            fast_model: document.getElementById("tc-fast-model").value,
            standard_model: document.getElementById("tc-standard-model").value,
            frontier_model: document.getElementById("tc-frontier-model").value,
            decomposition_enabled: document.getElementById("tc-decompose").checked,
            decomposition_threshold: parseFloat(document.getElementById("tc-decompose-threshold").value),
            max_subtasks: parseInt(document.getElementById("tc-max-subtasks").value),
            parallel_subtasks: document.getElementById("tc-parallel").checked,
            ml_model_enabled: document.getElementById("tc-ml-enabled").checked,
            ml_estimation_mode: document.getElementById("tc-ml-mode").value,
            ml_model_path: document.getElementById("tc-ml-model-path").value,
            ml_tokenizer_path: document.getElementById("tc-ml-tokenizer-path").value,
        },
        delegation_complexity: {
            enabled: document.getElementById("dc-enabled").checked,
            default_use_complexity: document.getElementById("dc-default-use").checked,
            allow_auto_select: document.getElementById("dc-auto-select").checked,
            enforce_min_tier: document.getElementById("dc-enforce-tier").checked,
            tier_models: {
                trivial: document.getElementById("dc-model-trivial").value,
                simple: document.getElementById("dc-model-simple").value,
                moderate: document.getElementById("dc-model-moderate").value,
                complex: document.getElementById("dc-model-complex").value,
            },
            tier_boundaries: {
                trivial: Number.isFinite(dcTrivial) ? dcTrivial : 0.15,
                simple: Number.isFinite(dcSimple) ? dcSimple : 0.35,
                moderate: Number.isFinite(dcModerate) ? dcModerate : 0.60,
                complex: Number.isFinite(dcComplex) ? dcComplex : 0.85,
            },
        },
    };
}

async function saveConfig() {
    const updates = collectForm();
    try {
        const resp = await fetch("/api/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config: updates }),
        });
        const data = await resp.json();
        if (data.status === "ok") {
            isDirty = false;
            document.getElementById("save-btn").disabled = true;
            document.getElementById("save-status").textContent = "";
            showToast(t("config.saved") + " (" + (data.applied || []).join(", ") + ")");
            loadConfig();
        } else {
            showToast(data.message || t("config.saveFailed"), "error");
        }
    } catch (e) {
        showToast(t("config.networkError"), "error");
    }
}

async function loadStats() {
    try {
        const resp = await fetch("/api/config/memory-stats");
        const data = await resp.json();
        document.getElementById("stat-facts").textContent = data.facts || 0;
        document.getElementById("stat-md-size").textContent = data.memory_md_size || 0;
        if (data.lifecycle) {
            document.getElementById("stat-pinned").textContent = data.lifecycle.pinned || 0;
            document.getElementById("stat-importance").textContent = data.lifecycle.avg_importance || "-";
            document.getElementById("stat-below").textContent = data.lifecycle.below_threshold || 0;
        }
    } catch (e) { /* Silently fail */ }
}

async function loadLessons() {
    try {
        const resp = await fetch("/api/config/lessons");
        const data = await resp.json();
        const container = document.getElementById("lessons-list");

        if (!data.lessons || data.lessons.length === 0) {
            container.innerHTML = '<em style="color: var(--text-muted); font-size: 0.8rem;">' + t("config.noLessons") + '</em>';
            document.getElementById("stat-lessons").textContent = "0";
            return;
        }

        document.getElementById("stat-lessons").textContent = data.lessons.length;

        container.innerHTML = data.lessons.map(l =>
            `<div class="lesson-item">
                <span class="category">${l.category || "general"}</span>
                <span class="text">${escapeHtml(l.lesson)}</span>
                <span class="hits">${l.hit_count || 1}x</span>
            </div>`
        ).join("");
    } catch (e) {
        document.getElementById("lessons-list").innerHTML = '<em style="color: var(--text-muted);">' + t("config.loadFailed") + '</em>';
    }
}

async function runMaintenance(action) {
    try {
        showToast(t("config.running") + " " + action + "...");
        const resp = await fetch("/api/config/run-maintenance", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action }),
        });
        const data = await resp.json();
        if (data.status === "ok") {
            showToast(action + " completed!");
            loadStats();
            loadLessons();
        } else {
            showToast(data.message || t("config.saveFailed"), "error");
        }
    } catch (e) {
        showToast(t("config.networkError"), "error");
    }
}

// === Bind Events ===

function bindEvents() {
    const fullAccessToggle = document.getElementById("sec-full-access");
    fullAccessToggle?.addEventListener("change", () => {
        if (fullAccessToggle.checked) {
            const confirmed = window.confirm(
                t(
                    "config.fullAccessConfirm",
                    "Warning: Full Access Mode disables approval/sandbox protection. Continue?",
                ),
            );
            if (!confirmed) {
                fullAccessToggle.checked = false;
                showToast(
                    t("config.fullAccessCancelled", "Full Access Mode was cancelled."),
                    "error",
                );
                return;
            }
            showToast(
                t(
                    "config.fullAccessEnabledToast",
                    "Full Access Mode enabled. Save to apply.",
                ),
            );
        }
        markDirty();
    });

    // All toggle/input changes trigger markDirty
    document.querySelectorAll("#app input:not(#sec-full-access), #app select").forEach(el => {
        el.addEventListener("change", markDirty);
    });

    document.getElementById("save-btn").addEventListener("click", saveConfig);

    // Maintenance buttons
    document.querySelectorAll("[data-maintenance]").forEach(btn => {
        btn.addEventListener("click", () => {
            runMaintenance(btn.getAttribute("data-maintenance"));
        });
    });

    // Warn before leaving with unsaved changes
    window.addEventListener("beforeunload", e => {
        if (isDirty) {
            e.preventDefault();
            e.returnValue = "";
        }
    });
}

// === Init ===

async function init() {
    await initLayout({ activePath: "/config" });

    bindEvents();
    loadConfig();
    loadStats();
    loadLessons();

    onLocaleChange(() => {
        updateBadges();
        if (isDirty) {
            document.getElementById("save-status").textContent = t("config.unsaved");
        }
    });

    KuroPlugins.initAll();
    KuroPlugins.emit("onPageLoad", "config");
}

init();
