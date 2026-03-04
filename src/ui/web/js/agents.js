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

document.addEventListener("DOMContentLoaded", async () => {
    await initLayout({ activePath: "/agents" });
    onLocaleChange(() => render());
    await loadInstances();
    initModals();
});

// ─── Data ────────────────────────────────────────────────────

async function loadInstances() {
    try {
        const res = await fetch(API);
        const data = await res.json();
        instances = data.instances || [];
        render();
    } catch (e) {
        document.getElementById("loading").textContent = t("agents.loadFailed") || "Failed to load agents";
    }
}

// ─── Render ──────────────────────────────────────────────────

function render() {
    const dashboard = document.getElementById("dashboard");

    // Stats bar
    const enabled = instances.filter(i => i.enabled).length;
    const withBot = instances.filter(i => i.bot_binding).length;
    const totalSubs = instances.reduce((s, i) => s + (i.sub_agents?.length || 0), 0);

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

    for (const inst of instances) {
        document.getElementById(`edit-${inst.id}`)?.addEventListener("click", () => openEditModal(inst));
        document.getElementById(`delete-${inst.id}`)?.addEventListener("click", () => deleteInstance(inst.id));
        document.getElementById(`personality-${inst.id}`)?.addEventListener("click", () => openPersonalityModal(inst.id));
        document.getElementById(`add-sub-${inst.id}`)?.addEventListener("click", () => openSubAgentModal(inst.id));

        // Delete sub-agent buttons
        for (const sa of (inst.sub_agents || [])) {
            document.getElementById(`del-sub-${inst.id}-${sa}`)?.addEventListener("click", () => deleteSubAgent(inst.id, sa));
        }
    }
}

function renderCard(inst) {
    const statusClass = inst.enabled ? "status-on" : "status-off";
    const statusText = inst.enabled ? (t("common.on") || "ON") : (t("common.off") || "OFF");

    const memoryBadge = `<span class="badge badge-${inst.memory_mode}">${inst.memory_mode}</span>`;
    const botBadge = inst.bot_binding
        ? `<span class="badge badge-bot">${inst.bot_binding.adapter_type}</span>`
        : "";

    const subList = (inst.sub_agents || []).map(name =>
        `<span class="sub-agent-tag">${name}
            <button class="tag-remove" id="del-sub-${inst.id}-${name}" title="Remove">&times;</button>
        </span>`
    ).join(" ");

    return `
        <div class="agent-card">
            <div class="agent-card-header">
                <div>
                    <span class="status-dot ${statusClass}"></span>
                    <strong>${inst.name}</strong>
                    <span class="agent-id">${inst.id}</span>
                </div>
                <div class="agent-card-actions">
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
        await loadInstances();
    } catch (e) {
        alert("Delete failed: " + e.message);
    }
}

// ─── Sub-Agent CRUD ──────────────────────────────────────────

async function deleteSubAgent(instanceId, name) {
    if (!confirm(`Remove sub-agent "${name}" from ${instanceId}?`)) return;
    try {
        await fetch(`${API}/${instanceId}/sub-agents/${name}`, { method: "DELETE" });
        await loadInstances();
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
    document.getElementById("edit-model").value = "";
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
    document.getElementById("edit-auto-approve").value = "";
    document.getElementById("edit-allowed-dirs").value = "";
    document.getElementById("edit-blocked-cmds").value = "";
    document.getElementById("edit-allowed-tools").value = "";
    document.getElementById("edit-denied-tools").value = "";
    document.getElementById("instance-modal").style.display = "";
}

function openEditModal(inst) {
    document.getElementById("modal-title").textContent = t("agents.editInstance") || "Edit Agent Instance";
    document.getElementById("edit-instance-id").value = inst.id;
    document.getElementById("edit-id").value = inst.id;
    document.getElementById("edit-id").disabled = true;
    document.getElementById("edit-name").value = inst.name;
    document.getElementById("edit-model").value = inst.model || "";
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
    document.getElementById("edit-auto-approve").value = (sec.auto_approve_levels || []).join(", ");
    document.getElementById("edit-allowed-dirs").value = (sec.allowed_directories || []).join(", ");
    document.getElementById("edit-blocked-cmds").value = (sec.blocked_commands || []).join(", ");
    document.getElementById("edit-allowed-tools").value = (inst.allowed_tools || []).join(", ");
    document.getElementById("edit-denied-tools").value = (inst.denied_tools || []).join(", ");
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
            auto_approve_levels: csvToList("edit-auto-approve"),
            allowed_directories: csvToList("edit-allowed-dirs"),
            blocked_commands: csvToList("edit-blocked-cmds"),
        },
    };

    if (!body.id || !body.name) {
        alert("ID and Name are required.");
        return;
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
        await loadInstances();
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

function openSubAgentModal(instanceId) {
    document.getElementById("subagent-instance-id").value = instanceId;
    document.getElementById("subagent-name").value = "";
    document.getElementById("subagent-model").value = "";
    document.getElementById("subagent-system-prompt").value = "";
    document.getElementById("subagent-max-rounds").value = "5";
    document.getElementById("subagent-modal").style.display = "";
}

async function saveSubAgent() {
    const instanceId = document.getElementById("subagent-instance-id").value;
    const body = {
        name: document.getElementById("subagent-name").value.trim(),
        model: document.getElementById("subagent-model").value.trim(),
        system_prompt: document.getElementById("subagent-system-prompt").value.trim(),
        max_tool_rounds: parseInt(document.getElementById("subagent-max-rounds").value) || 5,
    };
    if (!body.name) {
        alert("Name is required.");
        return;
    }
    try {
        const res = await fetch(`${API}/${instanceId}/sub-agents`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.status === "error") {
            alert(data.message);
            return;
        }
        closeModal("subagent-modal");
        await loadInstances();
    } catch (e) {
        alert("Save failed: " + e.message);
    }
}

// ─── Helpers ─────────────────────────────────────────────────

function closeModal(id) {
    document.getElementById(id).style.display = "none";
}
