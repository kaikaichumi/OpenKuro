/**
 * Kuro - Agent Instance Management Page
 *
 * CRUD for Primary Agent instances with sub-agent management,
 * memory stats, and personality editing.
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { initPanelNav, refreshPanelNav } from "./panel_nav.js";
import { escapeHtml } from "./utils.js";

const API = "/api/agents/instances";
let instances = [];
let mainSubAgents = [];
let availableModels = [];
let agentsUiSchema = null;
let instanceFormSchema = null;
let subAgentFormSchema = null;
let instanceFormValues = {};
let instanceFieldBindings = [];
let instanceIsEditMode = false;
let subAgentFormValues = {};

document.addEventListener("DOMContentLoaded", async () => {
    await initLayout({ activePath: "/agents" });
    await initPanelNav("agents");
    onLocaleChange(() => {
        render();
        refreshPanelNav();
        if (document.getElementById("instance-modal")?.style.display !== "none") {
            renderInstanceForm();
        }
        if (document.getElementById("subagent-modal")?.style.display !== "none") {
            renderSubAgentForm();
        }
    });
    initModals();
    const schemaPromise = loadAgentPageSchema();
    const dataPromise = reloadData();
    void loadModels();
    await Promise.all([schemaPromise, dataPromise]);
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

async function loadAgentPageSchema() {
    agentsUiSchema = null;
    instanceFormSchema = null;
    subAgentFormSchema = null;
    try {
        const res = await fetch("/api/ui/schema/agents");
        if (!res.ok) return;
        const data = await res.json();
        agentsUiSchema = data.schema || null;
        const forms = Array.isArray(agentsUiSchema?.forms) ? agentsUiSchema.forms : [];
        instanceFormSchema = forms.find((f) => String(f.id || "") === "instance-editor") || null;
        subAgentFormSchema = forms.find((f) => String(f.id || "") === "subagent-editor") || null;
    } catch {
        agentsUiSchema = null;
        instanceFormSchema = null;
        subAgentFormSchema = null;
    }
}

function deepClone(value) {
    return JSON.parse(JSON.stringify(value ?? {}));
}

function getByPath(obj, path, fallback = undefined) {
    const parts = String(path || "").split(".").filter(Boolean);
    let cur = obj;
    for (const part of parts) {
        if (!cur || typeof cur !== "object" || !(part in cur)) return fallback;
        cur = cur[part];
    }
    return cur;
}

function setByPath(obj, path, value) {
    const parts = String(path || "").split(".").filter(Boolean);
    if (parts.length === 0) return;
    let cur = obj;
    for (let i = 0; i < parts.length - 1; i++) {
        const p = parts[i];
        if (!cur[p] || typeof cur[p] !== "object") cur[p] = {};
        cur = cur[p];
    }
    cur[parts[parts.length - 1]] = value;
}

function csvToArray(value) {
    if (Array.isArray(value)) {
        return value.map((s) => String(s).trim()).filter(Boolean);
    }
    return String(value || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
}

function arrayToCsv(value) {
    if (!Array.isArray(value)) return "";
    return value.map((s) => String(s).trim()).filter(Boolean).join(", ");
}

function boolOverrideToSelect(value) {
    if (value === true) return "enabled";
    if (value === false) return "disabled";
    return "inherit";
}

function selectToBoolOverrideValue(value) {
    if (value === "enabled") return true;
    if (value === "disabled") return false;
    return null;
}

async function loadModels() {
    try {
        const res = await fetch("/api/models");
        if (!res.ok) return;
        const data = await res.json();
        availableModels = Array.isArray(data.available) ? data.available : [];
    } catch {
        availableModels = [];
    } finally {
        if (document.getElementById("instance-modal")?.style.display !== "none") {
            renderInstanceForm();
        }
        if (document.getElementById("subagent-modal")?.style.display !== "none") {
            renderSubAgentForm();
        }
    }
}

function getInstanceSubAgentDefs(inst) {
    if (Array.isArray(inst.sub_agent_defs) && inst.sub_agent_defs.length > 0) {
        return inst.sub_agent_defs;
    }
    return (inst.sub_agents || []).map(name => ({ name }));
}

function renderSubAgentCards(ownerId, defs) {
    return (defs || []).map((sa) => {
        const encoded = encodeURIComponent(sa.name || "");
        const tier = sa.complexity_tier || "moderate";
        const editLabel = t("common.edit") || "Edit";
        const model = sa.model ? escapeHtml(sa.model) : (t("common.default") || "Default");
        const rounds = Number.isFinite(sa.max_tool_rounds) ? sa.max_tool_rounds : "-";
        const name = escapeHtml(sa.name || "");
        return `<div class="subagent-card">
            <div class="subagent-head">
                <span class="subagent-name">${name}</span>
                <span class="subagent-tier">${escapeHtml(tier)}</span>
            </div>
            <div class="subagent-meta">
                <span>${t("agents.model") || "Model"}: <code>${model}</code></span>
                <span>${t("agents.maxToolRounds") || "Max Tool Rounds"}: ${rounds}</span>
            </div>
            <div class="subagent-actions">
                <button class="btn btn-xs" data-sub-edit="1" data-owner="${ownerId}" data-name="${encoded}" title="${editLabel}">${editLabel}</button>
                <button class="btn btn-xs btn-danger" data-sub-del="1" data-owner="${ownerId}" data-name="${encoded}" title="Remove">${t("common.delete", "Delete")}</button>
            </div>
        </div>`;
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

function formLabel(item, fallback = "") {
    if (item && item.label_i18n) return t(item.label_i18n, item.label || fallback);
    return (item && item.label) || fallback;
}

function formatTemplate(template, vars = {}) {
    let output = String(template ?? "");
    for (const [key, value] of Object.entries(vars)) {
        output = output.replaceAll(`{${key}}`, String(value));
    }
    return output;
}

function formSectionTitle(section) {
    if (section && section.title_i18n) return t(section.title_i18n, section.title || section.id || "");
    if (section && section.label_i18n) return t(section.label_i18n, section.title || section.id || "");
    return (section && (section.title || section.label)) || "";
}

function parseFormFieldValue(field, el) {
    const type = String(field.type || "text");
    if (type === "boolean") return el.checked === true;
    if (type === "number") {
        if (el.value === "") return null;
        const n = parseFloat(el.value);
        return Number.isFinite(n) ? n : null;
    }
    if (type === "csv") return csvToArray(el.value);
    if (type === "textarea" || type === "text" || type === "model") {
        const raw = String(el.value ?? "");
        if (field.nullable && raw.trim() === "") return null;
        return raw;
    }
    return el.value;
}

function createModelSelect(field, currentValue) {
    const select = document.createElement("select");
    const current = currentValue ?? "";
    const emptyLabel = String(field.empty_label || "(inherit from main)");
    if (field.allow_empty !== false) {
        const inheritOpt = document.createElement("option");
        inheritOpt.value = "";
        inheritOpt.textContent = emptyLabel;
        select.appendChild(inheritOpt);
    }
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
    return select;
}

function createFormInput(field, value) {
    const type = String(field.type || "text");
    if (type === "boolean") {
        const wrap = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = value === true;
        wrap.appendChild(checkbox);
        wrap.append(" ");
        wrap.append(document.createTextNode(formLabel(field, field.path || "Enabled")));
        return { input: checkbox, node: wrap };
    }
    if (type === "select") {
        const select = document.createElement("select");
        for (const optInfo of field.options || []) {
            const opt = document.createElement("option");
            opt.value = String(optInfo.value ?? "");
            if (optInfo.label_i18n) opt.textContent = t(optInfo.label_i18n, optInfo.label || opt.value);
            else opt.textContent = String(optInfo.label ?? opt.value);
            select.appendChild(opt);
        }
        if (value !== undefined && value !== null) {
            const normalized = String(value);
            if (![...select.options].some((o) => o.value === normalized)) {
                const custom = document.createElement("option");
                custom.value = normalized;
                custom.textContent = normalized;
                select.appendChild(custom);
            }
            select.value = normalized;
        }
        return { input: select, node: select };
    }
    if (type === "model") {
        const select = createModelSelect(field, value ?? "");
        return { input: select, node: select };
    }
    if (type === "textarea") {
        const textarea = document.createElement("textarea");
        textarea.rows = Number(field.rows || 3);
        textarea.value = value ?? "";
        if (field.placeholder) textarea.placeholder = String(field.placeholder);
        return { input: textarea, node: textarea };
    }
    const input = document.createElement("input");
    input.type = type === "number" ? "number" : "text";
    if (type === "number") {
        if (field.min !== undefined) input.min = String(field.min);
        if (field.max !== undefined) input.max = String(field.max);
        if (field.step !== undefined) input.step = String(field.step);
        input.value = value ?? "";
    } else if (type === "csv") {
        input.value = arrayToCsv(value);
    } else {
        input.value = value ?? "";
        if (field.pattern) input.pattern = String(field.pattern);
    }
    if (field.placeholder) input.placeholder = String(field.placeholder);
    return { input, node: input };
}

function isFieldVisibleForValues(field, values) {
    const visibleIf = field.visible_if;
    if (!visibleIf || typeof visibleIf !== "object") return true;
    const current = getByPath(values, visibleIf.path, undefined);
    if ("equals" in visibleIf) return current === visibleIf.equals;
    if ("not_equals" in visibleIf) return current !== visibleIf.not_equals;
    if ("in" in visibleIf && Array.isArray(visibleIf.in)) return visibleIf.in.includes(current);
    return true;
}

function isFieldVisible(field) {
    return isFieldVisibleForValues(field, instanceFormValues);
}

function applyInstanceFormVisibility() {
    for (const binding of instanceFieldBindings) {
        const visible = isFieldVisible(binding.field);
        binding.row.style.display = visible ? "" : "none";
    }
}

function getFormFields(formSchema) {
    const fields = [];
    const sections = Array.isArray(formSchema?.sections) ? formSchema.sections : [];
    for (const section of sections) {
        for (const field of section.fields || []) {
            if (!field || !field.path) continue;
            fields.push(field);
        }
    }
    return fields;
}

function isEmptyValue(value, fieldType) {
    if (value === null || value === undefined) return true;
    if (fieldType === "boolean") return false;
    if (Array.isArray(value)) return value.length === 0;
    if (typeof value === "string") return value.trim() === "";
    return false;
}

function validateFormValues(formSchema, values) {
    const fields = getFormFields(formSchema);
    const errors = [];
    for (const field of fields) {
        const type = String(field.type || "text");
        if (!isFieldVisibleForValues(field, values)) continue;
        const label = formLabel(field, field.path || "Field");
        const value = getByPath(values, field.path, undefined);
        const required = field.required === true;
        const empty = isEmptyValue(value, type);
        if (required && empty) {
            errors.push(formatTemplate(
                t("agents.validationRequired", "{field} is required."),
                { field: label },
            ));
            continue;
        }
        if (empty) continue;

        if (type === "number") {
            const n = typeof value === "number" ? value : parseFloat(String(value));
            if (!Number.isFinite(n)) {
                errors.push(formatTemplate(
                    t("agents.validationInvalidNumber", "{field} must be a valid number."),
                    { field: label },
                ));
                continue;
            }
            if (field.min !== undefined && n < Number(field.min)) {
                errors.push(formatTemplate(
                    t("agents.validationMin", "{field} must be >= {min}."),
                    { field: label, min: field.min },
                ));
            }
            if (field.max !== undefined && n > Number(field.max)) {
                errors.push(formatTemplate(
                    t("agents.validationMax", "{field} must be <= {max}."),
                    { field: label, max: field.max },
                ));
            }
            continue;
        }

        if (type === "select") {
            const options = Array.isArray(field.options) ? field.options : [];
            if (options.length > 0 && field.allow_custom !== true) {
                const current = String(value);
                const allowed = new Set(options.map((o) => String(o.value ?? "")));
                if (!allowed.has(current)) {
                    errors.push(formatTemplate(
                        t("agents.validationInvalidChoice", "{field} has an invalid option."),
                        { field: label },
                    ));
                }
            }
        }

        if ((type === "text" || type === "textarea" || type === "model") && field.pattern) {
            try {
                const pattern = new RegExp(String(field.pattern));
                if (!pattern.test(String(value))) {
                    errors.push(formatTemplate(
                        t("agents.validationInvalidFormat", "{field} has invalid format."),
                        { field: label },
                    ));
                }
            } catch {
                // Ignore invalid pattern definitions.
            }
        }

        if (type === "csv") {
            const arr = Array.isArray(value) ? value : csvToArray(value);
            if (field.min_items !== undefined && arr.length < Number(field.min_items)) {
                errors.push(formatTemplate(
                    t("agents.validationMinItems", "{field} requires at least {min} items."),
                    { field: label, min: field.min_items },
                ));
            }
            if (field.max_items !== undefined && arr.length > Number(field.max_items)) {
                errors.push(formatTemplate(
                    t("agents.validationMaxItems", "{field} allows at most {max} items."),
                    { field: label, max: field.max_items },
                ));
            }
        }
    }
    return errors;
}

function renderInstanceForm() {
    const root = document.getElementById("instance-form-root");
    if (!root) return;
    const schema = instanceFormSchema;
    if (!schema) {
        root.innerHTML = `<p class="empty-hint">${t("agents.loadFailed", "Failed to load agents")}</p>`;
        return;
    }

    root.innerHTML = "";
    instanceFieldBindings = [];
    const sections = Array.isArray(schema.sections) ? schema.sections : [];
    for (const section of sections) {
        const sectionWrap = section.collapsible
            ? document.createElement("details")
            : document.createElement("div");
        sectionWrap.className = section.collapsible ? "form-section-collapsible" : "";
        if (section.collapsible) {
            sectionWrap.style.margin = "0.5rem 0";
            const summary = document.createElement("summary");
            summary.style.cursor = "pointer";
            summary.style.color = "var(--primary)";
            summary.style.fontWeight = "600";
            summary.textContent = formSectionTitle(section);
            sectionWrap.appendChild(summary);
        } else {
            const h = document.createElement("h4");
            h.style.margin = "0.65rem 0 0.45rem";
            h.style.fontSize = "0.9rem";
            h.textContent = formSectionTitle(section);
            sectionWrap.appendChild(h);
        }

        const sectionBody = document.createElement("div");
        if (section.collapsible) sectionBody.style.padding = "0.5rem 0";

        for (const field of section.fields || []) {
            const row = document.createElement("div");
            row.className = "form-group";
            const type = String(field.type || "text");
            if (type !== "boolean") {
                const label = document.createElement("label");
                label.textContent = formLabel(field, field.path || "");
                row.appendChild(label);
            }
            const value = getByPath(instanceFormValues, field.path, field.default ?? (type === "csv" ? [] : ""));
            const { input, node } = createFormInput(field, value);
            if (instanceIsEditMode && field.path === "id") {
                input.disabled = true;
            }
            const eventName = (type === "text" || type === "textarea" || type === "csv") ? "input" : "change";
            input.addEventListener(eventName, () => {
                setByPath(instanceFormValues, field.path, parseFormFieldValue(field, input));
                applyInstanceFormVisibility();
            });
            row.appendChild(node);
            sectionBody.appendChild(row);
            instanceFieldBindings.push({ field, row });
        }

        sectionWrap.appendChild(sectionBody);
        root.appendChild(sectionWrap);
    }
    applyInstanceFormVisibility();
}

function renderSubAgentForm() {
    const root = document.getElementById("subagent-form-root");
    if (!root) return;
    const schema = subAgentFormSchema;
    if (!schema) {
        root.innerHTML = `<p class="empty-hint">${t("agents.loadFailed", "Failed to load agents")}</p>`;
        return;
    }

    root.innerHTML = "";
    const sections = Array.isArray(schema.sections) ? schema.sections : [];
    for (const section of sections) {
        const sectionWrap = document.createElement("div");
        const h = document.createElement("h4");
        h.style.margin = "0.65rem 0 0.45rem";
        h.style.fontSize = "0.9rem";
        h.textContent = formSectionTitle(section);
        sectionWrap.appendChild(h);

        const sectionBody = document.createElement("div");
        for (const field of section.fields || []) {
            const row = document.createElement("div");
            row.className = "form-group";
            const type = String(field.type || "text");
            if (type !== "boolean") {
                const label = document.createElement("label");
                label.textContent = formLabel(field, field.path || "");
                row.appendChild(label);
            }
            const value = getByPath(subAgentFormValues, field.path, field.default ?? (type === "csv" ? [] : ""));
            const { input, node } = createFormInput(field, value);
            const eventName = (type === "text" || type === "textarea" || type === "csv") ? "input" : "change";
            input.addEventListener(eventName, () => {
                setByPath(subAgentFormValues, field.path, parseFormFieldValue(field, input));
            });
            row.appendChild(node);
            sectionBody.appendChild(row);
        }
        sectionWrap.appendChild(sectionBody);
        root.appendChild(sectionWrap);
    }
}

function buildDefaultsFromSchema(formSchema) {
    const values = {};
    const sections = Array.isArray(formSchema?.sections)
        ? formSchema.sections
        : [];
    for (const section of sections) {
        for (const field of section.fields || []) {
            if (!field.path) continue;
            if (field.default !== undefined) {
                setByPath(values, field.path, deepClone(field.default));
                continue;
            }
            if (field.type === "boolean") setByPath(values, field.path, false);
            else if (field.type === "csv") setByPath(values, field.path, []);
            else setByPath(values, field.path, "");
        }
    }
    return values;
}

function buildInstanceDefaults() {
    return buildDefaultsFromSchema(instanceFormSchema);
}

function buildSubAgentDefaults() {
    return buildDefaultsFromSchema(subAgentFormSchema);
}

function toStringList(value) {
    return csvToArray(value);
}

function normalizeNullableString(value) {
    const s = String(value ?? "").trim();
    return s ? s : null;
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
        <div id="agents-overview" class="stats-grid">
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

        <div class="section" id="main-subagents">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
                <h2>Main Sub-Agents</h2>
                <button class="btn btn-primary" id="add-sub-main">+ Add</button>
            </div>
            <div class="subagent-grid">
                ${renderSubAgentCards("main", mainSubAgents) || `<span class="empty-hint">${t("agents.noSubAgents") || "None"}</span>`}
            </div>
        </div>

        <div class="section" id="instance-management">
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
    refreshPanelNav();

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
    const subList = renderSubAgentCards(inst.id, subDefs);

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
                    <div class="subagent-grid">
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

    // Personality modal
    document.getElementById("personality-cancel")?.addEventListener("click", () => closeModal("personality-modal"));
    document.getElementById("personality-save")?.addEventListener("click", savePersonality);

    // Sub-agent modal
    document.getElementById("subagent-cancel")?.addEventListener("click", () => closeModal("subagent-modal"));
    document.getElementById("subagent-save")?.addEventListener("click", saveSubAgent);
}

function openCreateModal() {
    if (!instanceFormSchema) {
        alert(t("agents.loadFailed") || "Failed to load agents");
        return;
    }
    document.getElementById("modal-title").textContent = t("agents.createInstance") || "Create Agent Instance";
    document.getElementById("edit-instance-id").value = "";
    instanceIsEditMode = false;
    instanceFormValues = buildInstanceDefaults();
    setByPath(instanceFormValues, "enabled", true);
    setByPath(instanceFormValues, "personality_mode", "independent");
    setByPath(instanceFormValues, "memory.mode", "independent");
    setByPath(instanceFormValues, "security.max_risk_level", "inherit");
    setByPath(instanceFormValues, "feature_overrides.context_compression_enabled_mode", "inherit");
    setByPath(instanceFormValues, "feature_overrides.memory_lifecycle_enabled_mode", "inherit");
    setByPath(instanceFormValues, "feature_overrides.learning_enabled_mode", "inherit");
    setByPath(instanceFormValues, "feature_overrides.code_feedback_enabled_mode", "inherit");
    setByPath(instanceFormValues, "feature_overrides.task_complexity_enabled_mode", "inherit");
    setByPath(instanceFormValues, "feature_overrides.vision_image_analysis_mode_mode", "inherit");
    renderInstanceForm();
    document.getElementById("instance-modal").style.display = "";
}

function openEditModal(inst) {
    if (!instanceFormSchema) {
        alert(t("agents.loadFailed") || "Failed to load agents");
        return;
    }
    document.getElementById("modal-title").textContent = t("agents.editInstance") || "Edit Agent Instance";
    document.getElementById("edit-instance-id").value = inst.id;
    instanceIsEditMode = true;
    const bot = inst.bot_binding || {};
    const sec = inst.security || {};
    const feat = inst.feature_overrides || {};
    instanceFormValues = buildInstanceDefaults();
    setByPath(instanceFormValues, "id", inst.id || "");
    setByPath(instanceFormValues, "name", inst.name || "");
    setByPath(instanceFormValues, "enabled", !!inst.enabled);
    setByPath(instanceFormValues, "model", inst.model || "");
    setByPath(instanceFormValues, "temperature", inst.temperature ?? null);
    setByPath(instanceFormValues, "personality_mode", inst.personality_mode || "independent");
    setByPath(instanceFormValues, "memory.mode", inst.memory_mode || "independent");
    setByPath(instanceFormValues, "memory.linked_agents", inst.memory_linked_agents || []);
    setByPath(instanceFormValues, "bot_binding.adapter_type", bot.adapter_type || "");
    setByPath(instanceFormValues, "bot_binding.bot_token_env", bot.bot_token_env || "");
    setByPath(instanceFormValues, "system_prompt", "");
    setByPath(instanceFormValues, "allowed_tools", inst.allowed_tools || []);
    setByPath(instanceFormValues, "denied_tools", inst.denied_tools || []);
    setByPath(instanceFormValues, "security.max_risk_level", sec.max_risk_level || "inherit");
    setByPath(instanceFormValues, "security.auto_approve_levels", sec.auto_approve_levels || []);
    setByPath(instanceFormValues, "security.allowed_directories", sec.allowed_directories || []);
    setByPath(instanceFormValues, "security.blocked_commands", sec.blocked_commands || []);
    setByPath(
        instanceFormValues,
        "feature_overrides.context_compression_enabled_mode",
        boolOverrideToSelect(feat.context_compression_enabled),
    );
    setByPath(
        instanceFormValues,
        "feature_overrides.context_compression_summarize_model",
        feat.context_compression_summarize_model || "",
    );
    setByPath(
        instanceFormValues,
        "feature_overrides.context_compression_trigger_threshold",
        feat.context_compression_trigger_threshold ?? null,
    );
    setByPath(
        instanceFormValues,
        "feature_overrides.memory_lifecycle_enabled_mode",
        boolOverrideToSelect(feat.memory_lifecycle_enabled),
    );
    setByPath(
        instanceFormValues,
        "feature_overrides.learning_enabled_mode",
        boolOverrideToSelect(feat.learning_enabled),
    );
    setByPath(
        instanceFormValues,
        "feature_overrides.code_feedback_enabled_mode",
        boolOverrideToSelect(feat.code_feedback_enabled),
    );
    setByPath(
        instanceFormValues,
        "feature_overrides.task_complexity_enabled_mode",
        boolOverrideToSelect(feat.task_complexity_enabled),
    );
    setByPath(
        instanceFormValues,
        "feature_overrides.vision_image_analysis_mode_mode",
        feat.vision_image_analysis_mode || "inherit",
    );
    renderInstanceForm();
    document.getElementById("instance-modal").style.display = "";
}

async function saveInstance() {
    const existingId = document.getElementById("edit-instance-id").value;
    const isEdit = !!existingId;
    const values = deepClone(instanceFormValues);
    const validationErrors = validateFormValues(instanceFormSchema, values);
    if (validationErrors.length > 0) {
        alert(validationErrors[0]);
        return;
    }

    const body = {
        id: String(getByPath(values, "id", "")).trim(),
        name: String(getByPath(values, "name", "")).trim(),
        enabled: getByPath(values, "enabled", true) === true,
        model: normalizeNullableString(getByPath(values, "model", "")),
        temperature: (() => {
            const n = getByPath(values, "temperature", null);
            return Number.isFinite(n) ? n : null;
        })(),
        personality_mode: String(getByPath(values, "personality_mode", "independent")),
        memory: {
            mode: String(getByPath(values, "memory.mode", "independent")),
            linked_agents: toStringList(getByPath(values, "memory.linked_agents", [])),
        },
        bot_binding: {
            adapter_type: String(getByPath(values, "bot_binding.adapter_type", "")),
            bot_token_env: String(getByPath(values, "bot_binding.bot_token_env", "")).trim(),
        },
        system_prompt: normalizeNullableString(getByPath(values, "system_prompt", "")),
        // Security
        allowed_tools: toStringList(getByPath(values, "allowed_tools", [])),
        denied_tools: toStringList(getByPath(values, "denied_tools", [])),
        security: {
            max_risk_level: (() => {
                const value = String(getByPath(values, "security.max_risk_level", "inherit"));
                return value === "inherit" ? "" : value;
            })(),
            auto_approve_levels: toStringList(getByPath(values, "security.auto_approve_levels", [])),
            allowed_directories: toStringList(getByPath(values, "security.allowed_directories", [])),
            blocked_commands: toStringList(getByPath(values, "security.blocked_commands", [])),
        },
        feature_overrides: {
            context_compression_enabled: selectToBoolOverrideValue(
                String(
                    getByPath(
                        values,
                        "feature_overrides.context_compression_enabled_mode",
                        "inherit",
                    ),
                ),
            ),
            context_compression_summarize_model: normalizeNullableString(
                getByPath(values, "feature_overrides.context_compression_summarize_model", ""),
            ),
            context_compression_trigger_threshold: (() => {
                const n = getByPath(
                    values,
                    "feature_overrides.context_compression_trigger_threshold",
                    null,
                );
                return Number.isFinite(n) ? n : null;
            })(),
            memory_lifecycle_enabled: selectToBoolOverrideValue(
                String(getByPath(values, "feature_overrides.memory_lifecycle_enabled_mode", "inherit")),
            ),
            learning_enabled: selectToBoolOverrideValue(
                String(getByPath(values, "feature_overrides.learning_enabled_mode", "inherit")),
            ),
            code_feedback_enabled: selectToBoolOverrideValue(
                String(getByPath(values, "feature_overrides.code_feedback_enabled_mode", "inherit")),
            ),
            task_complexity_enabled: selectToBoolOverrideValue(
                String(getByPath(values, "feature_overrides.task_complexity_enabled_mode", "inherit")),
            ),
            vision_image_analysis_mode: (() => {
                const v = String(
                    getByPath(
                        values,
                        "feature_overrides.vision_image_analysis_mode_mode",
                        "inherit",
                    ),
                );
                return v === "inherit" ? null : v;
            })(),
        },
    };

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
    if (!subAgentFormSchema) {
        alert(t("agents.loadFailed") || "Failed to load agents");
        return;
    }
    document.getElementById("subagent-instance-id").value = ownerId;
    document.getElementById("subagent-original-name").value = existing?.name || "";
    document.getElementById("subagent-title").textContent = existing
        ? (t("agents.editSubAgent") || "Edit Sub-Agent")
        : (t("agents.addSubAgent") || "Add Sub-Agent");
    subAgentFormValues = buildSubAgentDefaults();
    setByPath(subAgentFormValues, "name", existing?.name || "");
    setByPath(subAgentFormValues, "model", existing?.model || "");
    setByPath(subAgentFormValues, "system_prompt", existing?.system_prompt || "");
    setByPath(subAgentFormValues, "max_tool_rounds", Number(existing?.max_tool_rounds || 5));
    setByPath(subAgentFormValues, "complexity_tier", existing?.complexity_tier || "moderate");
    renderSubAgentForm();
    document.getElementById("subagent-modal").style.display = "";
}

async function saveSubAgent() {
    const ownerId = document.getElementById("subagent-instance-id").value;
    const originalName = document.getElementById("subagent-original-name").value.trim();
    const values = deepClone(subAgentFormValues);
    const validationErrors = validateFormValues(subAgentFormSchema, values);
    if (validationErrors.length > 0) {
        alert(validationErrors[0]);
        return;
    }
    const body = {
        name: String(getByPath(values, "name", "")).trim(),
        model: String(getByPath(values, "model", "")).trim(),
        system_prompt: String(getByPath(values, "system_prompt", "")).trim(),
        max_tool_rounds: (() => {
            const n = getByPath(values, "max_tool_rounds", 5);
            return Number.isFinite(n) ? Math.max(1, Math.floor(n)) : 5;
        })(),
        complexity_tier: String(getByPath(values, "complexity_tier", "moderate")) || "moderate",
    };
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
