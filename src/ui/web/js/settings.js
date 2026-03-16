/**
 * Kuro - Schema-driven Settings Page
 */

import { initLayout } from "./layout.js";
import { onLocaleChange, t } from "./i18n.js";
import { showToast } from "./utils.js";
import KuroPlugins from "./plugins.js";

const state = {
    schema: null,
    originalValues: {},
    draftValues: {},
    mcpDiscovery: {},
    modelOptions: {
        plainModels: [],
        oauthModelSet: new Set(),
        authCatalog: [],
    },
    modelOptionsRequestId: 0,
    dirty: false,
    search: "",
};

function deepClone(obj) {
    return JSON.parse(JSON.stringify(obj || {}));
}

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function getByPath(obj, path, fallback = undefined) {
    const parts = String(path || "").split(".").filter(Boolean);
    let cur = obj;
    for (const p of parts) {
        if (!cur || typeof cur !== "object" || !(p in cur)) {
            return fallback;
        }
        cur = cur[p];
    }
    return cur;
}

function setByPath(obj, path, value) {
    const parts = String(path || "").split(".").filter(Boolean);
    if (parts.length === 0) return;
    let cur = obj;
    for (let i = 0; i < parts.length - 1; i++) {
        const p = parts[i];
        if (!cur[p] || typeof cur[p] !== "object") {
            cur[p] = {};
        }
        cur = cur[p];
    }
    cur[parts[parts.length - 1]] = value;
}

function normalizeCsvText(value) {
    if (Array.isArray(value)) {
        return value.join(", ");
    }
    if (typeof value === "string") {
        return value;
    }
    return "";
}

function parseCsvValue(value) {
    return String(value || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
}

function parseFieldValue(field, inputEl) {
    const type = String(field.type || "text");
    if (type === "boolean") {
        return inputEl.checked === true;
    }
    if (type === "number") {
        const v = parseFloat(inputEl.value);
        return Number.isFinite(v) ? v : null;
    }
    if (type === "csv") {
        return parseCsvValue(inputEl.value);
    }
    return inputEl.value;
}

function formatEnvMap(envMap) {
    if (!envMap || typeof envMap !== "object") return "";
    return Object.entries(envMap)
        .map(([k, v]) => `${k}=${v ?? ""}`)
        .join("\n");
}

function parseEnvMap(text) {
    const env = {};
    const lines = String(text || "").split(/\r?\n/);
    for (const lineRaw of lines) {
        const line = lineRaw.trim();
        if (!line || line.startsWith("#")) continue;
        const idx = line.indexOf("=");
        if (idx <= 0) continue;
        const key = line.slice(0, idx).trim();
        const value = line.slice(idx + 1);
        if (!key) continue;
        env[key] = value;
    }
    return env;
}

function normalizeMcpServer(server, index) {
    const source = server && typeof server === "object" ? server : {};
    const name = String(source.name || "").trim() || `server_${index + 1}`;
    const args = Array.isArray(source.args)
        ? source.args.map((x) => String(x || "").trim()).filter(Boolean)
        : parseCsvValue(source.args || "");
    const env = source.env && typeof source.env === "object"
        ? Object.fromEntries(
            Object.entries(source.env)
                .map(([k, v]) => [String(k || "").trim(), String(v ?? "")])
                .filter(([k]) => Boolean(k)),
        )
        : {};
    const enabledTools = Array.isArray(source.enabled_tools)
        ? source.enabled_tools.map((x) => String(x || "").trim()).filter(Boolean)
        : [];

    return {
        name,
        enabled: source.enabled !== false,
        transport: String(source.transport || "stdio").trim() || "stdio",
        command: String(source.command || "").trim(),
        args,
        env,
        startup_timeout: Number.isFinite(Number(source.startup_timeout))
            ? Number(source.startup_timeout)
            : 15,
        request_timeout: Number.isFinite(Number(source.request_timeout))
            ? Number(source.request_timeout)
            : 30,
        tool_prefix: String(source.tool_prefix || "").trim(),
        enabled_tools: enabledTools,
        risk_level: String(source.risk_level || "high").trim().toLowerCase() || "high",
    };
}

function normalizeMcpServers(value) {
    const list = Array.isArray(value) ? value : [];
    return list.map((item, idx) => normalizeMcpServer(item, idx));
}

function createMcpServersInput(field, value, onChange) {
    const root = document.createElement("div");
    root.className = "mcp-editor";

    let servers = normalizeMcpServers(value);

    const keyFor = (server, idx) => `${idx}:${server.name || ""}`;
    const getDiscovery = (server, idx) => state.mcpDiscovery[keyFor(server, idx)] || null;
    const setDiscovery = (server, idx, payload) => {
        state.mcpDiscovery[keyFor(server, idx)] = payload;
    };

    const commit = (nextServers) => {
        servers = normalizeMcpServers(nextServers);
        onChange(servers);
        render();
    };

    const renderServerCard = (server, idx) => {
        const card = document.createElement("div");
        card.className = "mcp-server-card";

        const header = document.createElement("div");
        header.className = "mcp-server-header";

        const title = document.createElement("strong");
        title.textContent = server.name || `Server ${idx + 1}`;
        header.appendChild(title);

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "btn btn-sm btn-secondary";
        removeBtn.textContent = t("config.mcpRemoveServer", "Remove");
        removeBtn.addEventListener("click", () => {
            const next = servers.slice();
            next.splice(idx, 1);
            commit(next);
        });
        header.appendChild(removeBtn);
        card.appendChild(header);

        const updateServer = (patch) => {
            const next = servers.slice();
            next[idx] = normalizeMcpServer({ ...next[idx], ...patch }, idx);
            commit(next);
        };

        const row = (labelText, inputEl) => {
            const wrap = document.createElement("div");
            wrap.className = "mcp-server-row";
            const label = document.createElement("label");
            label.textContent = labelText;
            wrap.appendChild(label);
            wrap.appendChild(inputEl);
            return wrap;
        };

        const enabledToggle = document.createElement("input");
        enabledToggle.type = "checkbox";
        enabledToggle.checked = server.enabled !== false;
        enabledToggle.addEventListener("change", () => updateServer({ enabled: enabledToggle.checked }));
        card.appendChild(row(t("config.enabled", "Enabled"), enabledToggle));

        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.value = server.name || "";
        nameInput.placeholder = "server_name";
        nameInput.addEventListener("change", () => updateServer({ name: nameInput.value }));
        card.appendChild(row(t("config.mcpServerName", "Server Name"), nameInput));

        const transportSelect = document.createElement("select");
        const transportValues = Array.from(new Set([server.transport || "stdio", "stdio"]));
        for (const optValue of transportValues) {
            const opt = document.createElement("option");
            opt.value = optValue;
            opt.textContent = optValue;
            transportSelect.appendChild(opt);
        }
        transportSelect.value = server.transport || "stdio";
        transportSelect.addEventListener("change", () => updateServer({ transport: transportSelect.value }));
        card.appendChild(row(t("config.mcpTransport", "Transport"), transportSelect));

        const cmdInput = document.createElement("input");
        cmdInput.type = "text";
        cmdInput.value = server.command || "";
        cmdInput.placeholder = "python";
        cmdInput.addEventListener("change", () => updateServer({ command: cmdInput.value }));
        card.appendChild(row(t("config.mcpCommand", "Command"), cmdInput));

        const argsInput = document.createElement("input");
        argsInput.type = "text";
        argsInput.value = (server.args || []).join(", ");
        argsInput.placeholder = "-m, my_mcp_server";
        argsInput.addEventListener("change", () => updateServer({ args: parseCsvValue(argsInput.value) }));
        card.appendChild(row(t("config.mcpArgs", "Args (CSV)"), argsInput));

        const prefixInput = document.createElement("input");
        prefixInput.type = "text";
        prefixInput.value = server.tool_prefix || "";
        prefixInput.placeholder = "mcp_myserver_";
        prefixInput.addEventListener("change", () => updateServer({ tool_prefix: prefixInput.value }));
        card.appendChild(row(t("config.mcpPrefix", "Tool Prefix"), prefixInput));

        const riskSelect = document.createElement("select");
        for (const level of ["low", "medium", "high", "critical"]) {
            const opt = document.createElement("option");
            opt.value = level;
            opt.textContent = level;
            riskSelect.appendChild(opt);
        }
        riskSelect.value = server.risk_level || "high";
        riskSelect.addEventListener("change", () => updateServer({ risk_level: riskSelect.value }));
        card.appendChild(row(t("config.mcpRiskLevel", "Risk Level"), riskSelect));

        const startupInput = document.createElement("input");
        startupInput.type = "number";
        startupInput.min = "1";
        startupInput.step = "1";
        startupInput.value = String(server.startup_timeout ?? 15);
        startupInput.addEventListener("change", () => {
            const v = parseInt(startupInput.value, 10);
            updateServer({ startup_timeout: Number.isFinite(v) ? v : 15 });
        });
        card.appendChild(row(t("config.mcpStartupTimeout", "Startup Timeout (s)"), startupInput));

        const reqInput = document.createElement("input");
        reqInput.type = "number";
        reqInput.min = "1";
        reqInput.step = "1";
        reqInput.value = String(server.request_timeout ?? 30);
        reqInput.addEventListener("change", () => {
            const v = parseInt(reqInput.value, 10);
            updateServer({ request_timeout: Number.isFinite(v) ? v : 30 });
        });
        card.appendChild(row(t("config.mcpRequestTimeout", "Request Timeout (s)"), reqInput));

        const envInput = document.createElement("textarea");
        envInput.rows = 4;
        envInput.value = formatEnvMap(server.env);
        envInput.placeholder = "API_KEY=***\nBASE_URL=https://...";
        envInput.addEventListener("change", () => updateServer({ env: parseEnvMap(envInput.value) }));
        card.appendChild(row(t("config.mcpEnv", "Environment (KEY=VALUE per line)"), envInput));

        const discoverWrap = document.createElement("div");
        discoverWrap.className = "mcp-tools";

        const discoverActions = document.createElement("div");
        discoverActions.className = "mcp-tools-actions";
        const discoverBtn = document.createElement("button");
        discoverBtn.type = "button";
        discoverBtn.className = "btn btn-sm btn-secondary";
        discoverBtn.textContent = t("config.mcpDiscoverTools", "Discover Tools");
        const discoverStatus = document.createElement("span");
        discoverStatus.className = "mcp-discover-status";
        discoverActions.appendChild(discoverBtn);
        discoverActions.appendChild(discoverStatus);
        discoverWrap.appendChild(discoverActions);

        const discovery = getDiscovery(server, idx);
        const discoveredTools = Array.isArray(discovery && discovery.tools) ? discovery.tools : [];
        if (discovery && discovery.error) {
            const err = document.createElement("div");
            err.className = "mcp-tools-error";
            err.textContent = discovery.error;
            discoverWrap.appendChild(err);
        }

        const useAll = !Array.isArray(server.enabled_tools) || server.enabled_tools.length === 0;
        if (discoveredTools.length > 0) {
            const allWrap = document.createElement("label");
            allWrap.className = "mcp-use-all";
            const allToggle = document.createElement("input");
            allToggle.type = "checkbox";
            allToggle.checked = useAll;
            allToggle.addEventListener("change", () => {
                if (allToggle.checked) {
                    updateServer({ enabled_tools: [] });
                } else {
                    const allNames = discoveredTools
                        .map((tool) => String(tool && tool.name ? tool.name : "").trim())
                        .filter(Boolean);
                    updateServer({ enabled_tools: allNames });
                }
            });
            const allLabel = document.createElement("span");
            allLabel.textContent = t("config.mcpUseAllTools", "Use all discovered tools");
            allWrap.appendChild(allToggle);
            allWrap.appendChild(allLabel);
            discoverWrap.appendChild(allWrap);
        }

        if (discoveredTools.length > 0 && !useAll) {
            const list = document.createElement("div");
            list.className = "mcp-tool-list";
            for (const tool of discoveredTools) {
                const toolName = String(tool && tool.name ? tool.name : "").trim();
                if (!toolName) continue;
                const toolDesc = String(tool && tool.description ? tool.description : "");
                const item = document.createElement("label");
                item.className = "mcp-tool-item";
                const cb = document.createElement("input");
                cb.type = "checkbox";
                cb.checked = (server.enabled_tools || []).includes(toolName);
                cb.addEventListener("change", () => {
                    const nextSet = new Set(server.enabled_tools || []);
                    if (cb.checked) nextSet.add(toolName);
                    else nextSet.delete(toolName);
                    updateServer({ enabled_tools: Array.from(nextSet) });
                });
                const text = document.createElement("span");
                text.textContent = toolDesc ? `${toolName} - ${toolDesc}` : toolName;
                item.appendChild(cb);
                item.appendChild(text);
                list.appendChild(item);
            }
            discoverWrap.appendChild(list);
        } else if (discoveredTools.length > 0 && useAll) {
            const note = document.createElement("div");
            note.className = "mcp-tools-note";
            note.textContent = t("config.mcpAllToolsEnabled", "All discovered tools are currently enabled.");
            discoverWrap.appendChild(note);
        } else {
            const note = document.createElement("div");
            note.className = "mcp-tools-note";
            note.textContent = t("config.mcpNoToolsDiscovered", "No tools discovered yet.");
            discoverWrap.appendChild(note);
        }

        discoverBtn.addEventListener("click", async () => {
            discoverBtn.disabled = true;
            discoverStatus.textContent = t("common.loading", "Loading...");
            try {
                const resp = await fetch("/api/mcp/discover-tools", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        server: {
                            ...server,
                            args: server.args || [],
                            env: server.env || {},
                        },
                    }),
                });
                const data = await resp.json();
                if (!resp.ok || data.status !== "ok") {
                    throw new Error(data.message || t("config.loadFailed", "Failed to load"));
                }
                setDiscovery(server, idx, {
                    tools: Array.isArray(data.tools) ? data.tools : [],
                    error: "",
                });
                discoverStatus.textContent = t("config.mcpDiscoverSuccess", "Discovery complete");
            } catch (err) {
                setDiscovery(server, idx, {
                    tools: [],
                    error: String(err && err.message ? err.message : err),
                });
                discoverStatus.textContent = t("config.mcpDiscoverFailed", "Discovery failed");
            } finally {
                discoverBtn.disabled = false;
                render();
            }
        });

        card.appendChild(discoverWrap);
        return card;
    };

    const render = () => {
        root.innerHTML = "";

        const controls = document.createElement("div");
        controls.className = "mcp-editor-controls";
        const addBtn = document.createElement("button");
        addBtn.type = "button";
        addBtn.className = "btn btn-sm btn-secondary";
        addBtn.textContent = t("config.mcpAddServer", "Add Server");
        addBtn.addEventListener("click", () => {
            const next = servers.slice();
            next.push(normalizeMcpServer({}, next.length));
            commit(next);
        });
        controls.appendChild(addBtn);
        root.appendChild(controls);

        if (servers.length === 0) {
            const empty = document.createElement("div");
            empty.className = "mcp-empty";
            empty.textContent = t("config.mcpNoServers", "No MCP servers configured.");
            root.appendChild(empty);
            return;
        }

        for (let i = 0; i < servers.length; i++) {
            root.appendChild(renderServerCard(servers[i], i));
        }
    };

    render();
    return root;
}

function fieldTitle(field) {
    const label = field.label_i18n ? t(field.label_i18n, field.label || field.path) : (field.label || field.path);
    return label;
}

function fieldHelp(field) {
    if (field.help_i18n) {
        return t(field.help_i18n, field.help || "");
    }
    return field.help || "";
}

function sectionTitle(section) {
    return section.title_i18n ? t(section.title_i18n, section.title || section.id) : (section.title || section.id);
}

function sectionDescription(section) {
    if (section.description_i18n) {
        return t(section.description_i18n, section.description || "");
    }
    return section.description || "";
}

function categoryTitle(cat) {
    return cat.label_i18n ? t(cat.label_i18n, cat.label || cat.id) : (cat.label || cat.id);
}

function markDirty(dirty = true) {
    state.dirty = dirty;
    const btn = document.getElementById("save-btn");
    const status = document.getElementById("save-status");
    if (btn) btn.disabled = !dirty;
    if (status) status.textContent = dirty ? (t("config.unsaved", "Unsaved changes")) : "";
}

async function loadModelOptions() {
    const plainModels = [];
    const seenPlain = new Set();
    const oauthModelSet = new Set();
    const authCatalog = [];
    const seenAuthValue = new Set();

    const addPlain = (raw) => {
        const model = String(raw || "").trim();
        if (!model || seenPlain.has(model)) return;
        seenPlain.add(model);
        plainModels.push(model);
    };

    try {
        const res = await fetch("/api/models");
        if (!res.ok) {
            state.modelOptions = { plainModels, oauthModelSet, authCatalog };
            return;
        }
        const data = await res.json();
        const groups = data.groups || {};
        for (const arr of Object.values(groups)) {
            if (!Array.isArray(arr)) continue;
            for (const m of arr) addPlain(m);
        }
        const available = Array.isArray(data.available) ? data.available : [];
        for (const m of available) addPlain(m);

        const catalog = Array.isArray(data.catalog) ? data.catalog : [];
        for (const item of catalog) {
            const model = String(item && item.model ? item.model : "").trim();
            const value = String(item && item.value ? item.value : model).trim();
            if (model) addPlain(model);
            if (!value) continue;
            if (!seenAuthValue.has(value)) {
                seenAuthValue.add(value);
                authCatalog.push({
                    value,
                    label: String(item && item.label ? item.label : value),
                    group: String(item && item.group_label ? item.group_label : "Models"),
                });
            }
            if (item && item.auth === "oauth" && model) {
                oauthModelSet.add(model);
            }
        }
    } catch {
        // Keep defaults.
    }

    state.modelOptions = { plainModels, oauthModelSet, authCatalog };
}

async function refreshModelOptionsAndRender() {
    const reqId = ++state.modelOptionsRequestId;
    await loadModelOptions();
    if (reqId !== state.modelOptionsRequestId) return;
    renderAll();
}

function matchesSearch(section, query) {
    if (!query) return true;
    const q = query.toLowerCase();
    const sTitle = sectionTitle(section).toLowerCase();
    const sDesc = sectionDescription(section).toLowerCase();
    if (sTitle.includes(q) || sDesc.includes(q)) return true;
    const fields = Array.isArray(section.fields) ? section.fields : [];
    for (const field of fields) {
        const title = fieldTitle(field).toLowerCase();
        const help = fieldHelp(field).toLowerCase();
        if (title.includes(q) || help.includes(q) || String(field.path || "").toLowerCase().includes(q)) {
            return true;
        }
    }
    return false;
}

function visibleSections() {
    const schema = state.schema || {};
    const sections = Array.isArray(schema.sections) ? schema.sections : [];
    const q = String(state.search || "").trim();
    return sections.filter((s) => matchesSearch(s, q));
}

function renderSidebar() {
    const nav = document.getElementById("settings-nav");
    if (!nav) return;
    nav.innerHTML = "";

    const schema = state.schema || {};
    const categories = Array.isArray(schema.categories) ? schema.categories : [];
    const sections = visibleSections();
    const byCat = new Map();
    for (const sec of sections) {
        const cat = String(sec.category || "general");
        if (!byCat.has(cat)) byCat.set(cat, []);
        byCat.get(cat).push(sec);
    }

    if (sections.length === 0) {
        const empty = document.createElement("div");
        empty.className = "settings-empty";
        empty.textContent = t("config.noResults", "No matching settings.");
        nav.appendChild(empty);
        return;
    }

    for (const cat of categories) {
        const catId = String(cat.id || "").trim();
        if (!catId || !byCat.has(catId)) continue;
        const group = document.createElement("div");
        group.className = "settings-nav-group";
        const title = document.createElement("div");
        title.className = "settings-nav-title";
        title.textContent = categoryTitle(cat);
        group.appendChild(title);

        const secs = byCat.get(catId) || [];
        for (const sec of secs) {
            const a = document.createElement("a");
            a.href = `#settings-${sec.id}`;
            a.textContent = sectionTitle(sec);
            group.appendChild(a);
        }
        nav.appendChild(group);
    }

    for (const [catId, secs] of byCat.entries()) {
        if (categories.some((c) => String(c.id || "") === catId)) continue;
        const group = document.createElement("div");
        group.className = "settings-nav-group";
        const title = document.createElement("div");
        title.className = "settings-nav-title";
        title.textContent = catId;
        group.appendChild(title);
        for (const sec of secs) {
            const a = document.createElement("a");
            a.href = `#settings-${sec.id}`;
            a.textContent = sectionTitle(sec);
            group.appendChild(a);
        }
        nav.appendChild(group);
    }
}

function appendModelOptions(selectEl, field) {
    const mode = String(field.model_mode || "plain");
    const current = selectEl.value || "";
    const autoOpt = document.createElement("option");
    autoOpt.value = "";
    autoOpt.textContent = t("config.modelAuto", "Auto / Default");
    selectEl.appendChild(autoOpt);

    if (field.include_main) {
        const m = document.createElement("option");
        m.value = "main";
        m.textContent = "main";
        selectEl.appendChild(m);
    }

    if (mode === "auth") {
        const catalog = state.modelOptions.authCatalog || [];
        if (catalog.length > 0) {
            const grouped = new Map();
            for (const item of catalog) {
                if (!grouped.has(item.group)) grouped.set(item.group, []);
                grouped.get(item.group).push(item);
            }
            for (const [groupName, items] of grouped.entries()) {
                const optgroup = document.createElement("optgroup");
                optgroup.label = groupName || "Models";
                for (const item of items) {
                    const opt = document.createElement("option");
                    opt.value = item.value;
                    opt.textContent = item.label || item.value;
                    optgroup.appendChild(opt);
                }
                selectEl.appendChild(optgroup);
            }
        } else {
            const plainModels = state.modelOptions.plainModels || [];
            const oauthModels = state.modelOptions.oauthModelSet || new Set();
            for (const model of plainModels) {
                if (!model) continue;
                if (oauthModels.has(model)) {
                    const apiOpt = document.createElement("option");
                    apiOpt.value = model;
                    apiOpt.textContent = `${model} [API]`;
                    selectEl.appendChild(apiOpt);
                    const oauthOpt = document.createElement("option");
                    oauthOpt.value = `oauth:${model}`;
                    oauthOpt.textContent = `${model} [OAuth]`;
                    selectEl.appendChild(oauthOpt);
                } else {
                    const opt = document.createElement("option");
                    opt.value = model;
                    opt.textContent = model;
                    selectEl.appendChild(opt);
                }
            }
        }
    } else {
        const plainModels = state.modelOptions.plainModels || [];
        const oauthModels = state.modelOptions.oauthModelSet || new Set();
        for (const model of plainModels) {
            const opt = document.createElement("option");
            opt.value = model;
            opt.textContent = oauthModels.has(model) ? `${model} [OAuth]` : model;
            selectEl.appendChild(opt);
        }
    }

    const hasCurrent = Array.from(selectEl.options).some((o) => o.value === current);
    if (current && !hasCurrent) {
        const custom = document.createElement("option");
        custom.value = current;
        custom.textContent = `${current} (custom)`;
        selectEl.appendChild(custom);
    }
}

function createFieldInput(field, value, onChange) {
    const type = String(field.type || "text");
    if (type === "boolean") {
        const label = document.createElement("label");
        label.className = "toggle";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = value === true;
        const slider = document.createElement("span");
        slider.className = "slider";
        label.appendChild(input);
        label.appendChild(slider);
        input.addEventListener("change", () => onChange(parseFieldValue(field, input)));
        return label;
    }

    if (type === "select") {
        const select = document.createElement("select");
        const options = Array.isArray(field.options) ? field.options : [];
        for (const item of options) {
            const opt = document.createElement("option");
            const v = String(item && item.value !== undefined ? item.value : "").trim();
            opt.value = v;
            if (item && item.label_i18n) {
                opt.textContent = t(item.label_i18n, item.label || v);
            } else {
                opt.textContent = String(item && item.label ? item.label : v);
            }
            select.appendChild(opt);
        }
        select.value = value ?? "";
        select.addEventListener("change", () => onChange(parseFieldValue(field, select)));
        return select;
    }

    if (type === "model") {
        const select = document.createElement("select");
        select.value = value ?? "";
        appendModelOptions(select, field);
        select.value = value ?? "";
        select.addEventListener("change", () => onChange(parseFieldValue(field, select)));
        return select;
    }

    if (type === "mcp_servers") {
        return createMcpServersInput(field, value, onChange);
    }

    const input = document.createElement("input");
    if (type === "number") {
        input.type = "number";
        if (field.min !== undefined) input.min = String(field.min);
        if (field.max !== undefined) input.max = String(field.max);
        if (field.step !== undefined) input.step = String(field.step);
        input.value = value ?? "";
    } else {
        input.type = "text";
        input.value = type === "csv" ? normalizeCsvText(value) : (value ?? "");
    }
    if (field.placeholder) {
        input.placeholder = String(field.placeholder);
    }
    input.addEventListener("change", () => onChange(parseFieldValue(field, input)));
    return input;
}

function renderSections() {
    const root = document.getElementById("settings-sections");
    if (!root) return;
    root.innerHTML = "";

    const sections = visibleSections();
    if (sections.length === 0) {
        const empty = document.createElement("div");
        empty.className = "settings-empty";
        empty.textContent = t("config.noResults", "No matching settings.");
        root.appendChild(empty);
        return;
    }

    for (const section of sections) {
        const sec = document.createElement("div");
        sec.className = "section";
        sec.id = `settings-${section.id}`;

        const h2 = document.createElement("h2");
        const titleSpan = document.createElement("span");
        titleSpan.textContent = sectionTitle(section);
        h2.appendChild(titleSpan);

        const enabledField = (section.fields || []).find(
            (f) => String(f.path || "").endsWith(".enabled"),
        );
        if (enabledField) {
            const badge = document.createElement("span");
            const enabled = getByPath(state.draftValues, enabledField.path, false) === true;
            badge.className = `badge${enabled ? "" : " off"}`;
            badge.textContent = enabled ? (t("common.on", "ON")) : (t("common.off", "OFF"));
            h2.appendChild(badge);
        }

        sec.appendChild(h2);

        const desc = sectionDescription(section);
        if (desc) {
            const d = document.createElement("div");
            d.className = "settings-section-desc";
            d.textContent = desc;
            sec.appendChild(d);
        }

        for (const field of section.fields || []) {
            const row = document.createElement("div");
            row.className = "field";

            const labelWrap = document.createElement("div");
            labelWrap.className = "field-label";
            const title = document.createElement("span");
            title.textContent = fieldTitle(field);
            labelWrap.appendChild(title);
            const help = fieldHelp(field);
            if (help) {
                const small = document.createElement("small");
                small.textContent = help;
                labelWrap.appendChild(small);
            }

            const inputWrap = document.createElement("div");
            inputWrap.className = "field-input";
            const value = getByPath(state.draftValues, field.path, field.default ?? "");
            const input = createFieldInput(field, value, (nextVal) => {
                setByPath(state.draftValues, field.path, nextVal);
                markDirty(true);
            });
            inputWrap.appendChild(input);

            row.appendChild(labelWrap);
            row.appendChild(inputWrap);
            sec.appendChild(row);
        }

        root.appendChild(sec);
    }
}

function renderMeta() {
    const meta = document.getElementById("settings-meta");
    if (!meta) return;
    const version = getByPath(state.schema, "version", "1");
    meta.textContent = `Schema v${version}`;
}

function renderAll() {
    renderMeta();
    renderSidebar();
    renderSections();
    KuroPlugins.emit("onSettingsRender", {
        schema: state.schema,
        values: state.draftValues,
    });
}

function buildSchemaValuesPayload() {
    const payload = {};
    const schema = state.schema || {};
    const sections = Array.isArray(schema.sections) ? schema.sections : [];
    const seenPaths = new Set();
    for (const section of sections) {
        const fields = Array.isArray(section.fields) ? section.fields : [];
        for (const field of fields) {
            const path = String(field.path || "").trim();
            if (!path || seenPaths.has(path)) continue;
            const value = getByPath(state.draftValues, path, undefined);
            if (value === undefined) continue;
            seenPaths.add(path);
            setByPath(payload, path, value);
        }
    }
    return payload;
}

async function loadStats() {
    try {
        const resp = await fetch("/api/config/memory-stats");
        if (!resp.ok) return;
        const data = await resp.json();
        const setText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = String(value ?? "-");
        };
        setText("stat-facts", data.facts || 0);
        setText("stat-md-size", data.memory_md_size || 0);
        if (data.lifecycle) {
            setText("stat-pinned", data.lifecycle.pinned || 0);
            setText("stat-importance", data.lifecycle.avg_importance || "-");
            setText("stat-below", data.lifecycle.below_threshold || 0);
        }
    } catch {
        // best-effort
    }
}

async function loadLessons() {
    const listEl = document.getElementById("lessons-list");
    const statEl = document.getElementById("stat-lessons");
    if (!listEl || !statEl) return;

    try {
        const resp = await fetch("/api/config/lessons");
        if (!resp.ok) throw new Error("lessons request failed");
        const data = await resp.json();
        const lessons = Array.isArray(data.lessons) ? data.lessons : [];
        statEl.textContent = String(lessons.length);

        if (lessons.length === 0) {
            listEl.innerHTML = `<em style="color: var(--text-muted); font-size: 0.8rem;">${escapeHtml(t("config.noLessons", "No lessons yet."))}</em>`;
            return;
        }

        listEl.innerHTML = lessons.map((lesson) => {
            const category = escapeHtml(lesson && lesson.category ? lesson.category : "general");
            const text = escapeHtml(lesson && lesson.lesson ? lesson.lesson : "");
            const hits = Number.isFinite(lesson && lesson.hit_count)
                ? lesson.hit_count
                : 1;
            return `<div class="lesson-item"><span class="category">${category}</span><span class="text">${text}</span><span class="hits">${hits}x</span></div>`;
        }).join("");
    } catch {
        listEl.innerHTML = `<em style="color: var(--text-muted); font-size: 0.8rem;">${escapeHtml(t("config.loadFailed", "Failed to load"))}</em>`;
    }
}

async function runMaintenance(action) {
    try {
        const resp = await fetch("/api/config/run-maintenance", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action }),
        });
        const data = await resp.json();
        if (data.status === "ok") {
            showToast(`${action} completed`);
            loadStats();
            loadLessons();
        } else {
            showToast(data.message || t("config.saveFailed", "Failed"), "error");
        }
    } catch {
        showToast(t("config.networkError", "Network error"), "error");
    }
}

async function loadSettings() {
    const [schemaResp, valuesResp] = await Promise.all([
        fetch("/api/settings/schema"),
        fetch("/api/settings/values"),
    ]);
    if (!schemaResp.ok || !valuesResp.ok) {
        throw new Error("Failed to load settings schema/values");
    }
    const schemaData = await schemaResp.json();
    const valuesData = await valuesResp.json();
    state.schema = schemaData.schema || { version: 1, categories: [], sections: [] };
    state.originalValues = deepClone(valuesData.values || {});
    state.draftValues = deepClone(valuesData.values || {});
    state.mcpDiscovery = {};
    KuroPlugins.emit("onSettingsSchema", state.schema);
    markDirty(false);
    renderAll();
    void refreshModelOptionsAndRender();
    loadStats();
    loadLessons();
}

async function saveSettings() {
    try {
        const payload = buildSchemaValuesPayload();
        const resp = await fetch("/api/settings/values", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ values: payload }),
        });
        const data = await resp.json();
        if (!resp.ok || data.status !== "ok") {
            showToast(data.message || t("config.saveFailed", "Save failed"), "error");
            return;
        }
        state.originalValues = deepClone(state.draftValues);
        markDirty(false);
        const applied = Array.isArray(data.applied) ? data.applied : [];
        const suffix = applied.length > 0 ? ` (${applied.join(", ")})` : "";
        showToast(`${t("config.saved", "Saved")}${suffix}`);
        void refreshModelOptionsAndRender();
    } catch {
        showToast(t("config.networkError", "Network error"), "error");
    }
}

function bindEvents() {
    const saveBtn = document.getElementById("save-btn");
    const searchInput = document.getElementById("settings-search");
    const refreshBtn = document.getElementById("refresh-btn");

    saveBtn?.addEventListener("click", saveSettings);
    refreshBtn?.addEventListener("click", async () => {
        await loadSettings();
        showToast(t("config.reloaded", "Reloaded"));
    });

    searchInput?.addEventListener("input", () => {
        state.search = searchInput.value || "";
        renderAll();
    });

    document.querySelectorAll("[data-maintenance]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const action = btn.getAttribute("data-maintenance");
            if (action) runMaintenance(action);
        });
    });

    window.addEventListener("beforeunload", (e) => {
        if (!state.dirty) return;
        e.preventDefault();
        e.returnValue = "";
    });

    window.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
            e.preventDefault();
            searchInput?.focus();
            searchInput?.select();
        }
    });
}

async function init() {
    await initLayout({ activePath: "/config" });
    bindEvents();
    await loadSettings();

    onLocaleChange(() => {
        renderAll();
        loadLessons();
        if (state.dirty) {
            const status = document.getElementById("save-status");
            if (status) status.textContent = t("config.unsaved", "Unsaved changes");
        }
    });

    KuroPlugins.initAll();
    KuroPlugins.emit("onPageLoad", "config");
}

init().catch((e) => {
    console.error("settings init failed", e);
    showToast(t("config.loadFailed", "Failed to load settings"), "error");
});
