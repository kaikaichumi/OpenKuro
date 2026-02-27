/**
 * Kuro - Config Page Module
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml, showToast } from "./utils.js";
import KuroPlugins from "./plugins.js";

let isDirty = false;
let currentConfig = {};

function markDirty() {
    isDirty = true;
    document.getElementById("save-btn").disabled = false;
    document.getElementById("save-status").textContent = t("config.unsaved");
    updateBadges();
}

function updateBadges() {
    setBadge("badge-compression", document.getElementById("cc-enabled").checked);
    setBadge("badge-lifecycle", document.getElementById("ml-enabled").checked);
    setBadge("badge-learning", document.getElementById("le-enabled").checked);
    setBadge("badge-code", document.getElementById("cf-enabled").checked);
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
        populateForm(currentConfig);
    } catch (e) {
        showToast(t("config.loadFailed"), "error");
    }
}

function populateForm(cfg) {
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

    updateBadges();
}

function collectForm() {
    return {
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
    // All toggle/input changes trigger markDirty
    document.querySelectorAll("#app input, #app select").forEach(el => {
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
