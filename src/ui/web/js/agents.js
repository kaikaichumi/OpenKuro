/**
 * Kuro - Agent Instance Management Page
 *
 * CRUD for Primary Agent instances with sub-agent management,
 * memory stats, and personality editing.
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";

const API = "/api/agents/instances";
let instances = [];
let mainSubAgents = [];
const ENV_VAR_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
let availableModels = [];

document.addEventListener("DOMContentLoaded", async () => {
    await initLayout({ activePath: "/agents" });
    onLocaleChange(() => render());
    await loadModels();
    await reloadData();
    initModals();
});

// ─── Data ────────────────────────────────────────────────────

async function loadInstances() {
    const res = await fetch(API);
    const data = await res.json();
    instances = data.instances || [];
}

async function loadMainSubAgents() {
    const res = await fetch("/api/agents/main/sub-agents");
    const data = await res.json();
    mainSubAgents = Array.isArray(data.definitions) ? data.definitions : [];
}

async function reloadData() {
    try {
        await Promise.all([loadInstances(), loadMainSubAgents()]);
        render();
    } catch (_e) {
        document.getElementById("loading").textContent = t("agents.loadFailed") || "Failed to load agents";
    }
}

async function loadModels() {
    try {
        const res = await fetch("/api/models");
        if (!res.ok) return;
        const data = await res.json();
        availableModels = Array.isArray(data.available) ? data.available : [];
        populateModelSelect("edit-model", "(inherit from main)");
        populateModelSelect("edit-feature-context-model", "(inherit from main)");
    } catch {
        availableModels = [];
    }
}

function populateModelSelect(selectId, defaultLabel) {
    const select = document.getElementById(selectId);
    if (!select) return;

    const current = select.value || "";
    select.innerHTML = "";

    const inheritOpt = document.createElement("option");
    inheritOpt.value = "";
    inheritOpt.textContent = defaultLabel;
    select.appendChild(inheritOpt);

    for (const model of availableModels) {
        const opt = document.createElement("option");
        opt.value = model;
        opt.textContent = model;
        select.appendChild(opt);
    }

    if (current && !availableModels.includes(current)) {
        const custom = document.createElement("option");
        custom.value = current;
        custom.textContent = `${current} (custom)`;
        select.appendChild(custom);
    }
    select.value = current;
}

function setSelectValueSafe(selectId, value) {
    const select = document.getElementById(selectId);
    if (!select) return;
    const target = value ?? "";
    if (![...select.options].some(o => o.value === target)) {
        const custom = document.createElement("option");
        custom.value = target;
        custom.textContent = target ? `${target} (custom)` : "(inherit from main)";
        select.appendChild(custom);
    }
    select.value = target;
}

function boolOverrideToSelect(value) {
    if (value === true) return "enabled";
    if (value === false) return "disabled";
    return "inherit";
}

function selectToBoolOverride(id) {
    const value = document.getElementById(id)?.value || "inherit";
    if (value === "enabled") return true;
    if (value === "disabled") return false;
    return null;
}

function getInstanceSubAgentDefs(inst) {
    if (Array.isArray(inst.sub_agent_defs) && inst.sub_agent_defs.length > 0) {
        return inst.sub_agent_defs;
    }
    return (inst.sub_agents || []).map(name => ({ name }));
}

function renderSubAgentTags(ownerId, defs) {
    return (defs || []).map((sa) => {
        const encoded = encodeURIComponent(sa.name || "");
        const tier = sa.complexity_tier || "moderate";
        const tierLabel = t("agents.complexityTier") || "Complexity Tier";
        const editLabel = t("common.edit") || "Edit";
        return `<span class="sub-agent-tag" title="${tierLabel}: ${tier}">${sa.name} (${tier})
            <button class="btn btn-xs" data-sub-edit="1" data-owner="${ownerId}" data-name="${encoded}" title="${editLabel}">${editLabel}</button>
            <button class="tag-remove" data-sub-del="1" data-owner="${ownerId}" data-name="${encoded}" title="Remove">&times;</button>
        </span>`;
    }).join(" ");
}

function findSubAgentDefinition(ownerId, name) {
    if (ownerId === "main") {
        return (mainSubAgents || []).find(sa => sa.name === name) || null;
    }
    const inst = instances.find(i => i.id === ownerId);
    if (!inst) return null;
    return getInstanceSubAgentDefs(inst).find(sa => sa.name === name) || null;
}

// ─── Render ──────────────────────────────────────────────────

function render() {
    const dashboard = document.getElementById("dashboard");

    // Stats bar
    const enabled = instances.filter(i => i.enabled).length;
    const withBot = instances.filter(i => i.bot_binding).length;
    const totalSubs = (mainSubAgents?.length || 0)
        + instances.reduce((s, i) => s + getInstanceSubAgentDefs(i).length, 0);

    let html = `
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value">${instances.length}</div>
                <div class="stat-label">${t("agents.totalInstances") || "Total Instances"}</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">${enabled}</div>
                <div class="stat-label">${t("agents.enabledCount") || "Enabled"}</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">${withBot}</div>
                <div class="stat-label">${t("agents.withBot") || "With Bot"}</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">${totalSubs}</div>
                <div class="stat-label">${t("agents.totalSubAgents") || "Sub-Agents"}</div>
            </div>
        </div>

        <div class="section">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
                <h2>Main Sub-Agents</h2>
                <button class="btn btn-primary" id="add-sub-main">+ Add</button>
            </div>
            <div class="sub-agents-list">
                ${renderSubAgentTags("main", mainSubAgents) || `<span class="empty-hint">${t("agents.noSubAgents") || "None"}</span>`}
            </div>
        </div>

        <div class="section">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
                <h2>${t("agents.instanceList") || "Agent Instances"}</h2>
                <button class="btn btn-primary" id="btn-create">${t("agents.create") || "Create Instance"}</button>
            </div>
    `;

    if (instances.length === 0) {
        html += `<p class="empty-state">${t("agents.noInstances") || "No agent instances configured. Create one to get started."}</p>`;
    } else {
        html += `<p style="color:var(--text-dim);font-size:0.85rem;margin-bottom:0.75rem">
            💡 ${t("agents.usageTip") || "To chat with an agent: go to Chat, use split layout, then select the agent from the panel dropdown."}
        </p>`;
        html += `<div class="agent-cards">`;
        for (const inst of instances) {
            html += renderCard(inst);
        }
        html += `</div>`;
    }

    html += `</div>`;
    dashboard.innerHTML = html;

    // Bind events
    document.getElementById("btn-create")?.addEventListener("click", () => openCreateModal());
    document.getElementById("add-sub-main")?.addEventListener("click", () => openSubAgentModal("main"));

    for (const inst of instances) {
        document.getElementById(`toggle-${inst.id}`)?.addEventListener("click", () => toggleInstance(inst));
        document.getElementById(`edit-${inst.id}`)?.addEventListener("click", () => openEditModal(inst));
        document.getElementById(`delete-${inst.id}`)?.addEventListener("click", () => deleteInstance(inst.id));
        document.getElementById(`personality-${inst.id}`)?.addEventListener("click", () => openPersonalityModal(inst.id));
        document.getElementById(`add-sub-${inst.id}`)?.addEventListener("click", () => openSubAgentModal(inst.id));
    }

    document.querySelectorAll("[data-sub-del]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const owner = btn.getAttribute("data-owner") || "";
            const name = decodeURIComponent(btn.getAttribute("data-name") || "");
            await deleteSubAgent(owner, name);
        });
    });

    document.querySelectorAll("[data-sub-edit]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const owner = btn.getAttribute("data-owner") || "";
            const name = decodeURIComponent(btn.getAttribute("data-name") || "");
            const def = findSubAgentDefinition(owner, name) || { name };
            openSubAgentModal(owner, def);
        });
    });
}

function renderCard(inst) {
    const statusClass = inst.enabled ? "status-on" : "status-off";

    const memoryBadge = `<span class="badge badge-${inst.memory_mode}">${inst.memory_mode}</span>`;
    const botBadge = inst.bot_binding
        ? `<span class="badge badge-bot">${inst.bot_binding.adapter_type}</span>`
        : "";

    const subDefs = getInstanceSubAgentDefs(inst);
    const subList = renderSubAgentTags(inst.id, subDefs);

    return `
        <div class="agent-card">
            <div class="agent-card-header">
                <div>
                    <span class="status-dot ${statusClass}"></span>
                    <strong>${inst.name}</strong>
                    <span class="agent-id">${inst.id}</span>
                </div>
                <div class="agent-card-actions">
                    <button class="btn btn-sm" id="toggle-${inst.id}" title="Enable/Disable">
                        ${inst.enabled ? "Disable" : "Enable"}
                    </button>
                    <button class="btn btn-sm" id="edit-${inst.id}" title="Edit">${t("common.edit") || "Edit"}</button>
                    <button class="btn btn-sm btn-danger" id="delete-${inst.id}" title="Delete">&times;</button>
                </div>
            </div>

            <div class="agent-card-body">
                <div class="agent-meta">
                    <span>${t("agents.model") || "Model"}: <code>${inst.model || "(inherit)"}</code></span>
                    <span>${t("agents.memory") || "Memory"}: ${memoryBadge}</span>
                    ${inst.memory_mode === "linked" && inst.memory_linked_agents?.length
                        ? `<span>${t("agents.linkedTo") || "Linked to"}: ${inst.memory_linked_agents.join(", ")}</span>`
                        : ""}
                    <span>${t("agents.personality") || "Personality"}: ${inst.personality_mode}
                        ${inst.personality_mode === "independent"
                            ? `<button class="btn btn-xs" id="personality-${inst.id}">${t("common.edit") || "Edit"}</button>`
                            : ""}
                    </span>
                    ${botBadge ? `<span>${t("agents.bot") || "Bot"}: ${botBadge}</span>` : ""}
                    <span>${t("agents.sessions") || "Sessions"}: ${inst.active_sessions || 0}</span>
                </div>

                <div class="sub-agents-section">
                    <div style="display:flex;justify-content:space-between;align-items:center">
                        <span class="sub-agents-label">${t("agents.subAgents") || "Sub-Agents"}</span>
                        <button class="btn btn-xs" id="add-sub-${inst.id}">+ Add</button>
                    </div>
                    <div class="sub-agents-list">
                        ${subList || `<span class="empty-hint">${t("agents.noSubAgents") || "None"}</span>`}
                    </div>
                </div>
            </div>
        </div>
    `;
}

// ─── Instance CRUD ───────────────────────────────────────────

async function deleteInstance(id) {
    if (!confirm(`Delete agent instance "${id}"? This cannot be undone.`)) return;
    try {
        await fetch(`${API}/${id}`, { method: "DELETE" });
        await reloadData();
    } catch (e) {
        alert("Delete failed: " + e.message);
    }
}

async function toggleInstance(inst) {
    const enable = !inst.enabled;
    const action = enable ? "enable" : "disable";
    if (!confirm(`${action} agent instance "${inst.id}" now?`)) return;
    try {
        const res = await fetch(`${API}/${inst.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: enable }),
        });
        const data = await res.json();
        if (data.status === "error") {
            alert(data.message || `Failed to ${action} instance.`);
            return;
        }
        await reloadData();
    } catch (e) {
        alert(`${action} failed: ` + e.message);
    }
}

// ─── Sub-Agent CRUD ──────────────────────────────────────────

async function deleteSubAgent(ownerId, name) {
    if (!confirm(`Remove sub-agent "${name}" from ${ownerId}?`)) return;
    const base = ownerId === "main"
        ? "/api/agents/main/sub-agents"
        : `${API}/${ownerId}/sub-agents`;
    try {
        const res = await fetch(`${base}/${encodeURIComponent(name)}`, { method: "DELETE" });
        const data = await res.json();
        if (data.status === "error") {
            alert(data.message || "Delete failed");
            return;
        }
        await reloadData();
    } catch (e) {
        alert("Delete failed: " + e.message);
    }
}

// ─── Modals ──────────────────────────────────────────────────

function initModals() {
    // Instance modal
    document.getElementById("modal-cancel")?.addEventListener("click", () => closeModal("instance-modal"));
    document.getElementById("modal-save")?.addEventListener("click", saveInstance);

    // Memory mode toggle for linked agents field
    document.getElementById("edit-memory-mode")?.addEventListener("change", (e) => {
        const group = document.getElementById("linked-agents-group");
        if (group) group.style.display = e.target.value === "linked" ? "" : "none";
    });

    // Bot adapter toggle for token env field
    document.getElementById("edit-bot-adapter")?.addEventListener("change", (e) => {
        const group = document.getElementById("bot-token-group");
        if (group) group.style.display = e.target.value ? "" : "none";
    });

    // Personality modal
    document.getElementById("personality-cancel")?.addEventListener("click", () => closeModal("personality-modal"));
    document.getElementById("personality-save")?.addEventListener("click", savePersonality);

    // Sub-agent modal
    document.getElementById("subagent-cancel")?.addEventListener("click", () => closeModal("subagent-modal"));
    document.getElementById("subagent-save")?.addEventListener("click", saveSubAgent);
}

function openCreateModal() {
    document.getElementById("modal-title").textContent = t("agents.createInstance") || "Create Agent Instance";
    document.getElementById("edit-instance-id").value = "";
    document.getElementById("edit-id").value = "";
    document.getElementById("edit-id").disabled = false;
    document.getElementById("edit-name").value = "";
    document.getElementById("edit-enabled").checked = true;
    setSelectValueSafe("edit-model", "");
    document.getElementById("edit-temperature").value = "";
    document.getElementById("edit-personality-mode").value = "independent";
    document.getElementById("edit-memory-mode").value = "independent";
    document.getElementById("edit-linked-agents").value = "";
    document.getElementById("linked-agents-group").style.display = "none";
    document.getElementById("edit-bot-adapter").value = "";
    document.getElementById("edit-bot-token-env").value = "";
    document.getElementById("bot-token-group").style.display = "none";
    document.getElementById("edit-system-prompt").value = "";
    // Security fields
    document.getElementById("edit-max-risk-level").value = "inherit";
    document.getElementById("edit-auto-approve").value = "";
    document.getElementById("edit-allowed-dirs").value = "";
    document.getElementById("edit-blocked-cmds").value = "";
    document.getElementById("edit-allowed-tools").value = "";
    document.getElementById("edit-denied-tools").value = "";
    document.getElementById("edit-feature-context-compression").value = "inherit";
    setSelectValueSafe("edit-feature-context-model", "");
    document.getElementById("edit-feature-context-threshold").value = "";
    document.getElementById("edit-feature-memory-lifecycle").value = "inherit";
    document.getElementById("edit-feature-learning").value = "inherit";
    document.getElementById("edit-feature-code-feedback").value = "inherit";
    document.getElementById("edit-feature-task-complexity").value = "inherit";
    document.getElementById("edit-feature-vision-mode").value = "inherit";
    document.getElementById("instance-modal").style.display = "";
}

function openEditModal(inst) {
    document.getElementById("modal-title").textContent = t("agents.editInstance") || "Edit Agent Instance";
    document.getElementById("edit-instance-id").value = inst.id;
    document.getElementById("edit-id").value = inst.id;
    document.getElementById("edit-id").disabled = true;
    document.getElementById("edit-name").value = inst.name;
    document.getElementById("edit-enabled").checked = !!inst.enabled;
    setSelectValueSafe("edit-model", inst.model || "");
    document.getElementById("edit-temperature").value = inst.temperature ?? "";
    document.getElementById("edit-personality-mode").value = inst.personality_mode;
    document.getElementById("edit-memory-mode").value = inst.memory_mode;
    document.getElementById("edit-linked-agents").value = (inst.memory_linked_agents || []).join(", ");
    document.getElementById("linked-agents-group").style.display =
        inst.memory_mode === "linked" ? "" : "none";
    const bot = inst.bot_binding || {};
    document.getElementById("edit-bot-adapter").value = bot.adapter_type || "";
    document.getElementById("edit-bot-token-env").value = bot.bot_token_env || "";
    document.getElementById("bot-token-group").style.display =
        bot.adapter_type ? "" : "none";
    document.getElementById("edit-system-prompt").value = "";
    // Security fields
    const sec = inst.security || {};
    document.getElementById("edit-max-risk-level").value =
        sec.max_risk_level || "inherit";
    document.getElementById("edit-auto-approve").value = (sec.auto_approve_levels || []).join(", ");
    document.getElementById("edit-allowed-dirs").value = (sec.allowed_directories || []).join(", ");
    document.getElementById("edit-blocked-cmds").value = (sec.blocked_commands || []).join(", ");
    document.getElementById("edit-allowed-tools").value = (inst.allowed_tools || []).join(", ");
    document.getElementById("edit-denied-tools").value = (inst.denied_tools || []).join(", ");
    const feat = inst.feature_overrides || {};
    document.getElementById("edit-feature-context-compression").value =
        boolOverrideToSelect(feat.context_compression_enabled);
    setSelectValueSafe("edit-feature-context-model", feat.context_compression_summarize_model || "");
    document.getElementById("edit-feature-context-threshold").value =
        feat.context_compression_trigger_threshold ?? "";
    document.getElementById("edit-feature-memory-lifecycle").value =
        boolOverrideToSelect(feat.memory_lifecycle_enabled);
    document.getElementById("edit-feature-learning").value =
        boolOverrideToSelect(feat.learning_enabled);
    document.getElementById("edit-feature-code-feedback").value =
        boolOverrideToSelect(feat.code_feedback_enabled);
    document.getElementById("edit-feature-task-complexity").value =
        boolOverrideToSelect(feat.task_complexity_enabled);
    document.getElementById("edit-feature-vision-mode").value =
        feat.vision_image_analysis_mode || "inherit";
    document.getElementById("instance-modal").style.display = "";
}

async function saveInstance() {
    const existingId = document.getElementById("edit-instance-id").value;
    const isEdit = !!existingId;

    const csvToList = (id) => (document.getElementById(id)?.value || "")
        .split(",").map(s => s.trim()).filter(Boolean);

    const body = {
        id: document.getElementById("edit-id").value.trim(),
        name: document.getElementById("edit-name").value.trim(),
        enabled: !!document.getElementById("edit-enabled").checked,
        model: document.getElementById("edit-model").value.trim() || null,
        temperature: document.getElementById("edit-temperature").value
            ? parseFloat(document.getElementById("edit-temperature").value) : null,
        personality_mode: document.getElementById("edit-personality-mode").value,
        memory: {
            mode: document.getElementById("edit-memory-mode").value,
            linked_agents: csvToList("edit-linked-agents"),
        },
        bot_binding: {
            adapter_type: document.getElementById("edit-bot-adapter").value,
            bot_token_env: document.getElementById("edit-bot-token-env").value.trim(),
        },
        system_prompt: document.getElementById("edit-system-prompt").value.trim() || null,
        // Security
        allowed_tools: csvToList("edit-allowed-tools"),
        denied_tools: csvToList("edit-denied-tools"),
        security: {
            max_risk_level: (() => {
                const value = document.getElementById("edit-max-risk-level").value;
                return value === "inherit" ? "" : value;
            })(),
            auto_approve_levels: csvToList("edit-auto-approve"),
            allowed_directories: csvToList("edit-allowed-dirs"),
            blocked_commands: csvToList("edit-blocked-cmds"),
        },
        feature_overrides: {
            context_compression_enabled: selectToBoolOverride("edit-feature-context-compression"),
            context_compression_summarize_model:
                document.getElementById("edit-feature-context-model").value.trim() || null,
            context_compression_trigger_threshold: document.getElementById("edit-feature-context-threshold").value
                ? parseFloat(document.getElementById("edit-feature-context-threshold").value)
                : null,
            memory_lifecycle_enabled: selectToBoolOverride("edit-feature-memory-lifecycle"),
            learning_enabled: selectToBoolOverride("edit-feature-learning"),
            code_feedback_enabled: selectToBoolOverride("edit-feature-code-feedback"),
            task_complexity_enabled: selectToBoolOverride("edit-feature-task-complexity"),
            vision_image_analysis_mode: (() => {
                const v = document.getElementById("edit-feature-vision-mode").value;
                return v === "inherit" ? null : v;
            })(),
        },
    };

    if (!body.id || !body.name) {
        alert("ID and Name are required.");
        return;
    }
    if (body.bot_binding.adapter_type) {
        const tokenEnv = (body.bot_binding.bot_token_env || "").trim();
        if (!tokenEnv) {
            alert("Bot Token Env Var is required when Bot Adapter is enabled.");
            return;
        }
        if (!ENV_VAR_NAME_RE.test(tokenEnv)) {
            alert(
                "Bot Token Env Var must be an environment variable name (e.g. KURO_DISCORD_TOKEN_CS), not the token itself."
            );
            return;
        }
    }

    try {
        const url = isEdit ? `${API}/${existingId}` : API;
        const method = isEdit ? "PUT" : "POST";
        const res = await fetch(url, {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.status === "error") {
            alert(data.message);
            return;
        }
        closeModal("instance-modal");
        await reloadData();
    } catch (e) {
        alert("Save failed: " + e.message);
    }
}

// ─── Personality ─────────────────────────────────────────────

async function openPersonalityModal(instanceId) {
    document.getElementById("personality-instance-id").value = instanceId;
    document.getElementById("personality-title").textContent = `Personality: ${instanceId}`;
    try {
        const res = await fetch(`${API}/${instanceId}/personality`);
        const data = await res.json();
        document.getElementById("personality-content").value = data.content || "";
    } catch {
        document.getElementById("personality-content").value = "";
    }
    document.getElementById("personality-modal").style.display = "";
}

async function savePersonality() {
    const instanceId = document.getElementById("personality-instance-id").value;
    const content = document.getElementById("personality-content").value;
    try {
        await fetch(`${API}/${instanceId}/personality`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content }),
        });
        closeModal("personality-modal");
    } catch (e) {
        alert("Save failed: " + e.message);
    }
}

// ─── Sub-Agent ───────────────────────────────────────────────

function openSubAgentModal(ownerId, existing = null) {
    document.getElementById("subagent-instance-id").value = ownerId;
    document.getElementById("subagent-original-name").value = existing?.name || "";
    document.getElementById("subagent-title").textContent = existing
        ? (t("agents.editSubAgent") || "Edit Sub-Agent")
        : (t("agents.addSubAgent") || "Add Sub-Agent");
    document.getElementById("subagent-name").value = existing?.name || "";
    document.getElementById("subagent-model").value = existing?.model || "";
    document.getElementById("subagent-system-prompt").value = existing?.system_prompt || "";
    document.getElementById("subagent-max-rounds").value = String(existing?.max_tool_rounds || 5);
    document.getElementById("subagent-complexity-tier").value = existing?.complexity_tier || "moderate";
    document.getElementById("subagent-modal").style.display = "";
}

async function saveSubAgent() {
    const ownerId = document.getElementById("subagent-instance-id").value;
    const originalName = document.getElementById("subagent-original-name").value.trim();
    const body = {
        name: document.getElementById("subagent-name").value.trim(),
        model: document.getElementById("subagent-model").value.trim(),
        system_prompt: document.getElementById("subagent-system-prompt").value.trim(),
        max_tool_rounds: parseInt(document.getElementById("subagent-max-rounds").value) || 5,
        complexity_tier: document.getElementById("subagent-complexity-tier").value || "moderate",
    };
    if (!body.name) {
        alert("Name is required.");
        return;
    }
    try {
        const base = ownerId === "main"
            ? "/api/agents/main/sub-agents"
            : `${API}/${ownerId}/sub-agents`;
        const isEdit = !!originalName;
        const url = isEdit ? `${base}/${encodeURIComponent(originalName)}` : base;
        const res = await fetch(url, {
            method: isEdit ? "PUT" : "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.status === "error") {
            alert(data.message);
            return;
        }
        closeModal("subagent-modal");
        await reloadData();
    } catch (e) {
        alert("Save failed: " + e.message);
    }
}

// ─── Helpers ─────────────────────────────────────────────────

function closeModal(id) {
    document.getElementById(id).style.display = "none";
}
