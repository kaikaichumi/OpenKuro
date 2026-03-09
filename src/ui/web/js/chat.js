/**
 * Kuro - Chat Page Module (Multi-Panel / Split-Screen)
 *
 * Supports 1-6 concurrent ChatPanel instances, each bound to the main
 * engine or an AgentInstance.  A single WebSocket carries all traffic;
 * every message includes an `agent_id` field so the server can route
 * to the correct engine and the client can dispatch to the right panel.
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml, renderMarkdown, scrollToBottom } from "./utils.js";
import KuroPlugins from "./plugins.js";

// ========================================================================
// Constants
// ========================================================================

const LAYOUT_MODES = {
    single:  { panels: 1, label: "chat.singlePanel"  },
    "split-2": { panels: 2, label: "chat.split2"     },
    "split-3": { panels: 3, label: "chat.split3"     },
    "grid-4":  { panels: 4, label: "chat.grid4"      },
    "grid-6":  { panels: 6, label: "chat.grid6"      },
};

const MAIN_AGENT_ID = "main";

// ========================================================================
// Module-level state
// ========================================================================

let ws = null;
let reconnectTimer = null;
let currentLayout = "single";
/** @type {Map<string, ChatPanel>} agentId -> ChatPanel */
const panels = new Map();
/** @type {Array<{id:string, name:string}>} Cached agent list */
let agentList = [];

function isKnownAgentId(agentId) {
    return agentId === MAIN_AGENT_ID || agentList.some(a => a.id === agentId);
}

function isRoutableAgentId(agentId) {
    return typeof agentId === "string" && isKnownAgentId(agentId);
}

// Shared DOM refs
let approvalModal, approvalTool, approvalRisk, approvalParams, approvalAgentLabel;
let btnApprove, btnDeny, btnTrust;
let currentApprovalId = null;
let settingsPanel, auditPanel, screenPanel;
let modelSelect, trustSelect;
let screenImage, screenAction, screenStep, screenPlaceholder;
let oauthStatusLabel, btnOauthLogin, btnOauthLogout;

// ========================================================================
// ChatPanel class
// ========================================================================

class ChatPanel {
    /**
     * @param {string} agentId  "main" or an instance ID
     * @param {HTMLElement} container  The root .chat-panel element
     */
    constructor(agentId, container) {
        this.agentId = agentId;
        this.el = container;

        // Per-panel DOM
        this.messagesEl = container.querySelector(".panel-messages");
        this.inputEl = container.querySelector(".panel-input");
        this.sendBtn = container.querySelector(".panel-send-btn");
        this.statusDot = container.querySelector(".panel-status-dot");
        this.modelBadge = container.querySelector(".panel-model-badge");
        this.agentSelect = container.querySelector(".panel-agent-select");
        this.panelLabel = container.querySelector(".panel-agent-label");

        // Per-panel streaming state
        this.isStreaming = false;
        this.streamBubble = null;
        this.streamText = "";
        this.sessionId = sessionStorage.getItem("kuro_session_" + agentId) || null;

        this._bindPanelEvents();
    }

    // --- DOM helpers ---

    addBubble(role, text) {
        const div = document.createElement("div");
        div.className = "message message-" + role;
        div.textContent = text;
        this.messagesEl.appendChild(div);
        this._scrollToBottom();
        return div;
    }

    addSystemMessage(text) {
        const div = document.createElement("div");
        div.className = "message message-assistant";
        div.style.opacity = "0.7";
        div.style.fontStyle = "italic";
        div.textContent = text;
        this.messagesEl.appendChild(div);
        this._scrollToBottom();
    }

    // --- Streaming ---

    startStream() {
        this.isStreaming = true;
        this.streamText = "";
        this.streamBubble = this.addBubble("assistant", "");
        this.streamBubble.classList.add("typing-indicator");
        this.sendBtn.disabled = true;
    }

    appendStream(text) {
        this.streamText += text;
        if (this.streamBubble) {
            this.streamBubble.classList.remove("typing-indicator");
            this.streamBubble.textContent = this.streamText;
            this._scrollToBottom();
        }
    }

    endStream() {
        this.isStreaming = false;
        if (this.streamBubble) {
            this.streamBubble.classList.remove("typing-indicator");
            this.streamBubble.innerHTML = renderMarkdown(this.streamText);
            this.streamBubble = null;
        }
        this.streamText = "";
        this.sendBtn.disabled = false;
        this._scrollToBottom();
    }

    // --- History ---

    restoreHistory(messages) {
        this.messagesEl.innerHTML = "";
        for (const msg of messages) {
            const div = document.createElement("div");
            div.className = "message message-" + msg.role;
            if (msg.role === "assistant") {
                div.innerHTML = renderMarkdown(msg.content);
            } else {
                div.textContent = msg.content;
            }
            this.messagesEl.appendChild(div);
        }
        this._scrollToBottom();
    }

    // --- Status ---

    setStatus(state) {
        this.statusDot.className = "dot panel-status-dot dot-" + state;
    }

    updateStatus(data) {
        if (data.model && this.modelBadge) {
            const short = data.model.split("/").pop();
            const mode = data.model_auth_mode === "oauth" ? " (OAuth)" : "";
            this.modelBadge.textContent = short + mode;
            this.modelBadge.title = data.model;
        }
        if (data.session_id) {
            this.sessionId = data.session_id;
            sessionStorage.setItem("kuro_session_" + this.agentId, data.session_id);
        }
    }

    // --- Send ---

    sendMessage() {
        const text = this.inputEl.value.trim();
        if (!text || this.isStreaming) return;
        if (!isRoutableAgentId(this.agentId)) {
            this.addSystemMessage("Select a valid agent before sending.");
            return;
        }
        this.addBubble("user", text);
        send({ type: "message", text: text, agent_id: this.agentId });
        this.inputEl.value = "";
        this._autoResize();
    }

    // --- Clear ---

    clearMessages() {
        if (isRoutableAgentId(this.agentId)) {
            send({ type: "command", command: "clear", agent_id: this.agentId });
        }
        this.messagesEl.innerHTML = "";
        this.sessionId = null;
        sessionStorage.removeItem("kuro_session_" + this.agentId);
    }

    // --- Change agent binding ---

    switchAgent(newAgentId) {
        if (newAgentId === this.agentId) return;
        const oldId = this.agentId;
        if (panels.has(newAgentId)) {
            this.addSystemMessage("This agent is already assigned to another panel.");
            if (this.agentSelect) this.agentSelect.value = oldId;
            return;
        }
        panels.delete(oldId);
        this.agentId = newAgentId;
        this.el.dataset.agentId = newAgentId;
        this.sessionId = sessionStorage.getItem("kuro_session_" + newAgentId) || null;
        this.messagesEl.innerHTML = "";
        panels.set(newAgentId, this);
        // Request handshake/restore for the new agent if routable.
        if (isRoutableAgentId(newAgentId)) {
            send({ type: "restore", session_id: this.sessionId, agent_id: newAgentId });
        }
    }

    // --- Internal ---

    _scrollToBottom() {
        const container = this.messagesEl.parentElement; // .panel-messages-container
        if (container) {
            container.scrollTop = container.scrollHeight;
        }
    }

    _autoResize() {
        this.inputEl.style.height = "auto";
        this.inputEl.style.height = Math.min(this.inputEl.scrollHeight, 120) + "px";
    }

    _bindPanelEvents() {
        this.sendBtn.addEventListener("click", () => this.sendMessage());
        this.inputEl.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
        });
        this.inputEl.addEventListener("input", () => this._autoResize());

        if (this.agentSelect) {
            this.agentSelect.addEventListener("change", () => {
                this.switchAgent(this.agentSelect.value);
            });
        }
    }

    /** Populate the agent <select> with current options. */
    populateAgentSelect() {
        if (!this.agentSelect) return;
        const current = this.agentId;
        this.agentSelect.innerHTML = "";

        if (!isKnownAgentId(current)) {
            const unassigned = document.createElement("option");
            unassigned.value = current;
            unassigned.textContent = "Unassigned";
            unassigned.selected = true;
            this.agentSelect.appendChild(unassigned);
        }

        const mainOpt = document.createElement("option");
        mainOpt.value = MAIN_AGENT_ID;
        mainOpt.textContent = "Main";
        if (current === MAIN_AGENT_ID) mainOpt.selected = true;
        this.agentSelect.appendChild(mainOpt);

        for (const agent of agentList) {
            const opt = document.createElement("option");
            opt.value = agent.id;
            opt.textContent = agent.name || agent.id;
            if (current === agent.id) opt.selected = true;
            this.agentSelect.appendChild(opt);
        }
    }
}

// ========================================================================
// Panel creation
// ========================================================================

function createPanelElement(agentId) {
    const el = document.createElement("div");
    el.className = "chat-panel";
    el.dataset.agentId = agentId;
    el.innerHTML = `
        <div class="chat-panel-header">
            <select class="panel-agent-select"></select>
            <span class="dot panel-status-dot dot-disconnected"></span>
            <span class="badge badge-model panel-model-badge" title="">&#8212;</span>
            <span class="panel-agent-label"></span>
        </div>
        <div class="panel-messages-container">
            <div class="panel-messages"></div>
        </div>
        <div class="panel-input-area">
            <div class="panel-input-wrapper">
                <textarea class="panel-input" data-i18n-placeholder="chat.placeholder" placeholder="${t("chat.placeholder") || "Type a message..."}" rows="1"></textarea>
                <button class="panel-send-btn" title="${t("chat.send") || "Send"}">&#10148;</button>
            </div>
        </div>
    `;
    return el;
}

function ensurePanels(count) {
    const grid = document.getElementById("panels-grid");
    const existing = panels.size;

    // Need more panels
    if (count > existing) {
        // Collect agent ids already used
        const usedIds = new Set(panels.keys());
        // Available agents for new panels
        const available = agentList.filter(a => !usedIds.has(a.id));
        let availIdx = 0;

        for (let i = existing; i < count; i++) {
            let agentId;
            if (i === 0 && !usedIds.has(MAIN_AGENT_ID)) {
                agentId = MAIN_AGENT_ID;
            } else if (availIdx < available.length) {
                agentId = available[availIdx++].id;
            } else {
                // Extra panel remains unassigned until user picks an agent.
                agentId = "__unassigned_" + i;
            }
            const el = createPanelElement(agentId);
            grid.appendChild(el);
            const panel = new ChatPanel(agentId, el);
            panel.populateAgentSelect();
            panels.set(agentId, panel);
            if (ws && ws.readyState === WebSocket.OPEN) {
                panel.setStatus("connected");
            }
            // Request session from server
            if (ws && ws.readyState === WebSocket.OPEN && isRoutableAgentId(agentId)) {
                send({ type: "restore", session_id: panel.sessionId, agent_id: agentId });
            }
        }
    }

    // Need fewer panels — remove from the end
    if (count < existing) {
        const allKeys = [...panels.keys()];
        for (let i = allKeys.length - 1; i >= count; i--) {
            const key = allKeys[i];
            const panel = panels.get(key);
            if (panel && panel.el.parentElement) {
                panel.el.parentElement.removeChild(panel.el);
            }
            panels.delete(key);
        }
    }
}

// ========================================================================
// Layout management
// ========================================================================

function setLayout(mode) {
    const grid = document.getElementById("panels-grid");
    const info = LAYOUT_MODES[mode] || LAYOUT_MODES.single;
    currentLayout = mode;

    // Update grid CSS class
    grid.className = "panels-grid layout-" + mode;

    // Update toolbar buttons
    document.querySelectorAll(".layout-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.layout === mode);
    });

    // Update label
    const label = document.getElementById("layout-label");
    if (label) label.textContent = t(info.label) || mode;

    // Ensure correct number of panels
    ensurePanels(info.panels);

    // Hide panel headers in single mode for cleaner look
    panels.forEach(p => {
        const header = p.el.querySelector(".chat-panel-header");
        if (header) {
            header.style.display = mode === "single" ? "none" : "";
        }
    });
}

// ========================================================================
// WebSocket (single shared connection)
// ========================================================================

function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws`;
    panels.forEach(p => p.setStatus("connecting"));
    ws = new WebSocket(url);

    ws.onopen = function () {
        panels.forEach(p => p.setStatus("connected"));
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
        // Send main panel handshake first, then other routable panels.
        const mainPanel = panels.get(MAIN_AGENT_ID);
        if (mainPanel && isRoutableAgentId(mainPanel.agentId)) {
            ws.send(JSON.stringify({
                type: "restore",
                session_id: mainPanel.sessionId,
                agent_id: mainPanel.agentId,
            }));
        }
        panels.forEach(p => {
            if (p === mainPanel || !isRoutableAgentId(p.agentId)) return;
            ws.send(JSON.stringify({
                type: "restore",
                session_id: p.sessionId,
                agent_id: p.agentId,
            }));
        });
    };

    ws.onmessage = function (event) {
        let data;
        try { data = JSON.parse(event.data); } catch (e) { return; }
        routeMessage(data);
    };

    ws.onclose = function () {
        panels.forEach(p => p.setStatus("disconnected"));
        scheduleReconnect();
    };

    ws.onerror = function () {
        panels.forEach(p => p.setStatus("disconnected"));
    };
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        connect();
    }, 3000);
}

function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(data));
    }
}

// ========================================================================
// Message routing
// ========================================================================

function routeMessage(data) {
    KuroPlugins.emit("onMessage", data);
    const agentId = data.agent_id || MAIN_AGENT_ID;
    const panel = panels.get(agentId);

    switch (data.type) {
        case "status":
            if (panel) {
                panel.setStatus("connected");
                panel.updateStatus(data);
            }
            updateGlobalStatus(data);
            break;
        case "history":
            if (panel) panel.restoreHistory(data.messages || []);
            break;
        case "stream_start":
            if (panel) panel.startStream();
            break;
        case "stream_chunk":
            if (panel) panel.appendStream(data.text);
            break;
        case "stream_end":
            if (panel) panel.endStream();
            break;
        case "approval_request":
            showApproval(data);
            break;
        case "approval_result":
            break;
        case "screen_update":
            showScreenUpdate(data);
            break;
        case "screen_action":
            showScreenAction(data);
            break;
        case "skills_list":
            renderSkillsList(data.skills || []);
            break;
        case "error":
            if (panel) panel.addSystemMessage(t("common.error") + ": " + data.message);
            break;
    }
}

/** Update global badges (trust etc.) from main agent status. */
function updateGlobalStatus(data) {
    if ((data.agent_id || MAIN_AGENT_ID) !== MAIN_AGENT_ID) return;
    if (data.trust_level) {
        const tb = document.getElementById("trust-badge-global");
        if (tb) tb.textContent = data.trust_level.toUpperCase();
        if (trustSelect) trustSelect.value = data.trust_level;
    }
    if (modelSelect && data.model) {
        if (!data.model_override) {
            modelSelect.value = "";
            return;
        }
        let expected = data.model;
        if (data.model_auth_mode === "oauth") {
            expected = "oauth:" + data.model;
        } else if (data.model.startsWith("openai/")) {
            expected = "api:" + data.model;
        }
        if ([...modelSelect.options].some(o => o.value === expected)) {
            modelSelect.value = expected;
        } else if ([...modelSelect.options].some(o => o.value === data.model)) {
            modelSelect.value = data.model;
        }
    }
}

// ========================================================================
// Approval Modal (shared)
// ========================================================================

function showApproval(data) {
    currentApprovalId = data.approval_id;
    approvalTool.textContent = t("approval.tool") + ": " + data.tool_name;
    approvalRisk.textContent = t("approval.risk") + ": " + data.risk_level.toUpperCase();
    approvalRisk.style.color = riskColor(data.risk_level);
    approvalParams.textContent = JSON.stringify(data.params, null, 2);
    // Show which agent requested approval
    const aid = data.agent_id || MAIN_AGENT_ID;
    if (approvalAgentLabel) {
        approvalAgentLabel.textContent = aid !== MAIN_AGENT_ID
            ? "Agent: " + aid
            : "";
    }
    approvalModal.classList.remove("hidden");
}

function respondApproval(action) {
    if (!currentApprovalId) return;
    send({ type: "approval_response", approval_id: currentApprovalId, action: action });
    currentApprovalId = null;
    approvalModal.classList.add("hidden");
}

function riskColor(level) {
    const colors = { low: "#2ecc71", medium: "#f39c12", high: "#e74c3c", critical: "#ff0000" };
    return colors[level] || "#e0e0e0";
}

// ========================================================================
// Settings / Audit / Screen  (shared, largely unchanged)
// ========================================================================

function loadModels() {
    fetch("/api/models")
        .then(r => r.json())
        .then(data => {
            const previous = modelSelect.value;
            modelSelect.innerHTML = "";
            const opt = document.createElement("option");
            opt.value = "";
            opt.textContent = t("common.default") + " (" + (data.default || "").split("/").pop() + ")";
            modelSelect.appendChild(opt);
            const catalog = Array.isArray(data.catalog) ? data.catalog : [];
            if (catalog.length > 0) {
                const grouped = new Map();
                for (const item of catalog) {
                    const key = item.group_label || item.provider || "Models";
                    if (!grouped.has(key)) grouped.set(key, []);
                    grouped.get(key).push(item);
                }
                for (const [groupLabel, items] of grouped.entries()) {
                    const optgroup = document.createElement("optgroup");
                    optgroup.label = groupLabel;
                    for (const item of items) {
                        const o = document.createElement("option");
                        o.value = item.value;
                        o.textContent = item.label || item.model || item.value;
                        optgroup.appendChild(o);
                    }
                    modelSelect.appendChild(optgroup);
                }
            } else {
                (data.available || []).forEach((m) => {
                    const o = document.createElement("option");
                    o.value = m;
                    o.textContent = m;
                    modelSelect.appendChild(o);
                });
            }
            if (previous && [...modelSelect.options].some(o => o.value === previous)) {
                modelSelect.value = previous;
            }
        })
        .catch(() => {});
}

function loadOpenAIOAuthStatus() {
    fetch("/api/oauth/openai/status")
        .then(r => r.json())
        .then(data => {
            if (!oauthStatusLabel) return;
            if (!data.configured) {
                oauthStatusLabel.textContent = "OpenAI OAuth Subscription: Unavailable";
                if (btnOauthLogin) {
                    btnOauthLogin.classList.remove("hidden");
                    btnOauthLogin.disabled = false;
                }
                if (btnOauthLogout) {
                    btnOauthLogout.classList.add("hidden");
                    btnOauthLogout.disabled = true;
                }
                return;
            }
            if (data.logged_in) {
                const email = data.email ? " (" + data.email + ")" : "";
                oauthStatusLabel.textContent = "OpenAI OAuth Subscription: Connected" + email;
                if (btnOauthLogin) {
                    btnOauthLogin.classList.add("hidden");
                    btnOauthLogin.disabled = false;
                }
                if (btnOauthLogout) {
                    btnOauthLogout.classList.remove("hidden");
                    btnOauthLogout.disabled = false;
                }
            } else {
                oauthStatusLabel.textContent = "OpenAI OAuth Subscription: Not signed in";
                if (btnOauthLogin) {
                    btnOauthLogin.classList.remove("hidden");
                    btnOauthLogin.disabled = false;
                }
                if (btnOauthLogout) {
                    btnOauthLogout.classList.add("hidden");
                    btnOauthLogout.disabled = false;
                }
            }
        })
        .catch(() => {
            if (oauthStatusLabel) {
                oauthStatusLabel.textContent = "OpenAI OAuth Subscription: Status unavailable";
            }
        });
}

function loadAudit() {
    fetch("/api/audit?limit=50")
        .then(r => r.json())
        .then(data => {
            const container = document.getElementById("audit-entries");
            container.innerHTML = "";
            (data.entries || []).forEach(entry => {
                const div = document.createElement("div");
                div.className = "audit-entry";
                const time = entry.timestamp || entry.ts || "";
                const tool = entry.tool_name || entry.event_type || "event";
                const detail = entry.result_summary || entry.details || "";
                div.innerHTML =
                    '<span class="audit-time">' + escapeHtml(time) + "</span> " +
                    '<span class="audit-tool">' + escapeHtml(tool) + "</span> " +
                    escapeHtml(detail);
                container.appendChild(div);
            });
            if (!data.entries || data.entries.length === 0) {
                container.innerHTML = '<div class="audit-entry">' + t("chat.noAudit") + '</div>';
            }
        })
        .catch(() => {});
}

function showScreenUpdate(data) {
    if (screenPanel.classList.contains("hidden")) {
        screenPanel.classList.remove("hidden");
    }
    screenPlaceholder.classList.add("hidden");
    screenImage.classList.remove("hidden");
    screenImage.src = data.image;
    screenStep.textContent = t("chat.screenStep") + " " + (data.step || 0);
    if (data.action) {
        screenAction.textContent = data.action;
        screenAction.classList.remove("hidden");
    }
}

function showScreenAction(data) {
    if (screenPanel.classList.contains("hidden")) {
        screenPanel.classList.remove("hidden");
    }
    screenStep.textContent = t("chat.screenStep") + " " + (data.step || 0);
    screenAction.textContent = data.action || "";
    screenAction.classList.remove("hidden");
}

function loadSkills() {
    fetch("/api/skills")
        .then(r => r.json())
        .then(data => { renderSkillsList(data.skills || []); })
        .catch(() => {});
}

function renderSkillsList(skills) {
    const container = document.getElementById("skills-list");
    if (!container) return;
    container.innerHTML = "";
    if (skills.length === 0) {
        container.innerHTML = '<div class="skill-item" style="opacity:0.5">' + t("settings.noSkills") + '</div>';
        return;
    }
    skills.forEach(s => {
        const div = document.createElement("div");
        div.className = "skill-item";
        const dot = s.active ? "\u25cf" : "\u25cb";
        const cls = s.active ? "skill-active" : "skill-inactive";
        div.innerHTML = '<span class="' + cls + '">' + dot + "</span> " +
            '<span class="skill-name">' + escapeHtml(s.name) + "</span>" +
            '<span class="skill-desc"> \u2014 ' + escapeHtml(s.description) + "</span>";
        div.style.cursor = "pointer";
        div.addEventListener("click", () => {
            send({ type: "command", command: "skill", args: s.name, agent_id: MAIN_AGENT_ID });
        });
        container.appendChild(div);
    });
}

// ========================================================================
// Fetch agent instances for panel selector
// ========================================================================

async function fetchAgentList() {
    try {
        const res = await fetch("/api/agents/instances");
        if (!res.ok) return;
        const data = await res.json();
        agentList = (data.instances || [])
            .filter(a => a.enabled && a.running !== false)
            .map(a => ({ id: a.id, name: a.name }));
        // Refresh selects in all panels
        panels.forEach(p => p.populateAgentSelect());
    } catch (e) {
        // Agents endpoint may not exist yet — that's fine
        agentList = [];
    }
}

// ========================================================================
// Global event binding
// ========================================================================

function bindGlobalEvents() {
    // Approval modal
    approvalModal = document.getElementById("approval-modal");
    approvalTool = document.getElementById("approval-tool");
    approvalRisk = document.getElementById("approval-risk");
    approvalParams = document.getElementById("approval-params");
    approvalAgentLabel = document.getElementById("approval-agent-label");
    btnApprove = document.getElementById("btn-approve");
    btnDeny = document.getElementById("btn-deny");
    btnTrust = document.getElementById("btn-trust");

    btnApprove?.addEventListener("click", () => respondApproval("approve"));
    btnDeny?.addEventListener("click", () => respondApproval("deny"));
    btnTrust?.addEventListener("click", () => respondApproval("trust"));

    // Side panels
    settingsPanel = document.getElementById("settings-panel");
    auditPanel = document.getElementById("audit-panel");
    screenPanel = document.getElementById("screen-panel");
    modelSelect = document.getElementById("model-select");
    trustSelect = document.getElementById("trust-select");
    screenImage = document.getElementById("screen-image");
    screenAction = document.getElementById("screen-action");
    screenStep = document.getElementById("screen-step");
    screenPlaceholder = document.getElementById("screen-placeholder");
    oauthStatusLabel = document.getElementById("openai-oauth-status");
    btnOauthLogin = document.getElementById("btn-openai-oauth-login");
    btnOauthLogout = document.getElementById("btn-openai-oauth-logout");

    document.getElementById("btn-screen")?.addEventListener("click", () => {
        screenPanel?.classList.toggle("hidden");
        settingsPanel?.classList.add("hidden");
        auditPanel?.classList.add("hidden");
    });

    document.getElementById("btn-settings")?.addEventListener("click", () => {
        settingsPanel?.classList.toggle("hidden");
        auditPanel?.classList.add("hidden");
        screenPanel?.classList.add("hidden");
        if (settingsPanel && !settingsPanel.classList.contains("hidden")) {
            loadModels();
            loadSkills();
            loadOpenAIOAuthStatus();
        }
    });

    document.getElementById("btn-audit")?.addEventListener("click", () => {
        auditPanel?.classList.toggle("hidden");
        settingsPanel?.classList.add("hidden");
        screenPanel?.classList.add("hidden");
        if (auditPanel && !auditPanel.classList.contains("hidden")) loadAudit();
    });

    document.querySelectorAll(".panel-close").forEach(btn => {
        btn.addEventListener("click", () => {
            const panelId = btn.getAttribute("data-panel");
            const panel = document.getElementById(panelId);
            if (panel) panel.classList.add("hidden");
        });
    });

    modelSelect?.addEventListener("change", () => {
        send({ type: "command", command: "model", args: modelSelect.value, agent_id: MAIN_AGENT_ID });
    });

    btnOauthLogin?.addEventListener("click", async () => {
        try {
            const r = await fetch("/api/oauth/openai/status");
            const s = await r.json();
            if (!s.configured) {
                alert("OpenAI OAuth subscription sign-in is not available right now.");
                return;
            }
        } catch (e) {
            // ignore and still try login redirect
        }
        window.location.href = "/api/oauth/openai/login";
    });

    btnOauthLogout?.addEventListener("click", async () => {
        try {
            await fetch("/api/oauth/openai/logout", { method: "POST" });
        } catch (e) {
            // ignore
        }
        loadOpenAIOAuthStatus();
        loadModels();
    });

    trustSelect?.addEventListener("change", () => {
        send({ type: "command", command: "trust", args: trustSelect.value, agent_id: MAIN_AGENT_ID });
    });

    document.getElementById("btn-clear")?.addEventListener("click", () => {
        // Clear the focused/main panel
        const main = panels.get(MAIN_AGENT_ID);
        if (main) main.clearMessages();
        settingsPanel?.classList.add("hidden");
    });

    document.getElementById("btn-refresh-audit")?.addEventListener("click", loadAudit);

    // Layout mode buttons
    document.querySelectorAll(".layout-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const mode = btn.dataset.layout;
            if (mode && LAYOUT_MODES[mode]) {
                setLayout(mode);
            }
        });
    });
}

// ========================================================================
// Init
// ========================================================================

async function init() {
    await initLayout({
        activePath: "/",
        rightButtons: [
            '<button id="btn-screen" class="icon-btn" data-i18n-title="chat.screenPreview">&#128424;</button>',
            '<button id="btn-settings" class="icon-btn" data-i18n-title="settings.title">&#9881;</button>',
            '<button id="btn-audit" class="icon-btn" data-i18n-title="chat.auditLog">&#128220;</button>',
        ],
    });

    // Fetch agent list before creating panels
    await fetchAgentList();

    // Create the initial main panel
    const grid = document.getElementById("panels-grid");
    const mainEl = createPanelElement(MAIN_AGENT_ID);
    grid.appendChild(mainEl);
    const mainPanel = new ChatPanel(MAIN_AGENT_ID, mainEl);
    mainPanel.populateAgentSelect();
    panels.set(MAIN_AGENT_ID, mainPanel);

    // Apply initial layout (single)
    setLayout("single");

    // Bind shared UI events
    bindGlobalEvents();

    // Connect WebSocket
    connect();
    loadOpenAIOAuthStatus();
    loadModels();

    onLocaleChange(() => {
        // Re-apply status text
        if (ws && ws.readyState === WebSocket.OPEN) {
            panels.forEach(p => p.setStatus("connected"));
        }
        // Update layout label
        const info = LAYOUT_MODES[currentLayout];
        const label = document.getElementById("layout-label");
        if (label && info) label.textContent = t(info.label) || currentLayout;
    });

    KuroPlugins.initAll();
    KuroPlugins.emit("onPageLoad", "chat");
}

init();
