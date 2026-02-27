/**
 * Kuro - Chat Page Module
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml, renderMarkdown, scrollToBottom } from "./utils.js";
import KuroPlugins from "./plugins.js";

// === State ===
let ws = null;
let reconnectTimer = null;
let currentApprovalId = null;
let isStreaming = false;
let streamBubble = null;
let streamText = "";

// === DOM Elements (resolved after init) ===
let messagesEl, inputEl, sendBtn, modelBadge, trustBadge, statusDot, statusText;
let approvalModal, approvalTool, approvalRisk, approvalParams;
let btnApprove, btnDeny, btnTrust;
let settingsPanel, auditPanel, screenPanel;
let modelSelect, trustSelect;
let screenImage, screenAction, screenStep, screenPlaceholder;

function resolveDom() {
    messagesEl = document.getElementById("messages");
    inputEl = document.getElementById("user-input");
    sendBtn = document.getElementById("btn-send");
    modelBadge = document.getElementById("model-badge");
    trustBadge = document.getElementById("trust-badge");
    statusDot = document.getElementById("status-dot");
    statusText = document.getElementById("status-text");

    approvalModal = document.getElementById("approval-modal");
    approvalTool = document.getElementById("approval-tool");
    approvalRisk = document.getElementById("approval-risk");
    approvalParams = document.getElementById("approval-params");
    btnApprove = document.getElementById("btn-approve");
    btnDeny = document.getElementById("btn-deny");
    btnTrust = document.getElementById("btn-trust");

    settingsPanel = document.getElementById("settings-panel");
    auditPanel = document.getElementById("audit-panel");
    screenPanel = document.getElementById("screen-panel");
    modelSelect = document.getElementById("model-select");
    trustSelect = document.getElementById("trust-select");

    screenImage = document.getElementById("screen-image");
    screenAction = document.getElementById("screen-action");
    screenStep = document.getElementById("screen-step");
    screenPlaceholder = document.getElementById("screen-placeholder");
}

// === WebSocket ===

function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws`;
    setStatus("connecting");
    ws = new WebSocket(url);

    ws.onopen = function () {
        setStatus("connected");
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
    };

    ws.onmessage = function (event) {
        let data;
        try { data = JSON.parse(event.data); } catch (e) { return; }
        handleMessage(data);
    };

    ws.onclose = function () {
        setStatus("disconnected");
        scheduleReconnect();
    };

    ws.onerror = function () {
        setStatus("disconnected");
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

function setStatus(state) {
    statusDot.className = "dot dot-" + state;
    const labels = {
        connected: t("chat.connected"),
        disconnected: t("chat.disconnected"),
        connecting: t("chat.connecting"),
    };
    statusText.textContent = labels[state] || state;
}

// === Message Handling ===

function handleMessage(data) {
    KuroPlugins.emit("onMessage", data);
    switch (data.type) {
        case "status": updateStatus(data); break;
        case "stream_start": startStream(); break;
        case "stream_chunk": appendStream(data.text); break;
        case "stream_end": endStream(); break;
        case "approval_request": showApproval(data); break;
        case "approval_result": break;
        case "screen_update": showScreenUpdate(data); break;
        case "screen_action": showScreenAction(data); break;
        case "skills_list": renderSkillsList(data.skills || []); break;
        case "error": addSystemMessage(t("common.error") + ": " + data.message); break;
    }
}

function updateStatus(data) {
    if (data.model) {
        const short = data.model.split("/").pop();
        modelBadge.textContent = short;
        modelBadge.title = data.model;
    }
    if (data.trust_level) {
        trustBadge.textContent = data.trust_level.toUpperCase();
        trustSelect.value = data.trust_level;
    }
}

// === Streaming ===

function startStream() {
    isStreaming = true;
    streamText = "";
    streamBubble = addBubble("assistant", "");
    streamBubble.classList.add("typing-indicator");
    sendBtn.disabled = true;
}

function appendStream(text) {
    streamText += text;
    if (streamBubble) {
        streamBubble.classList.remove("typing-indicator");
        streamBubble.textContent = streamText;
        scrollToBottom("chat-container");
    }
}

function endStream() {
    isStreaming = false;
    if (streamBubble) {
        streamBubble.classList.remove("typing-indicator");
        streamBubble.innerHTML = renderMarkdown(streamText);
        streamBubble = null;
    }
    streamText = "";
    sendBtn.disabled = false;
    scrollToBottom("chat-container");
}

// === Approval Modal ===

function showApproval(data) {
    currentApprovalId = data.approval_id;
    approvalTool.textContent = t("approval.tool") + ": " + data.tool_name;
    approvalRisk.textContent = t("approval.risk") + ": " + data.risk_level.toUpperCase();
    approvalRisk.style.color = riskColor(data.risk_level);
    approvalParams.textContent = JSON.stringify(data.params, null, 2);
    approvalModal.classList.remove("hidden");
}

function respondApproval(action) {
    if (!currentApprovalId) return;
    send({ type: "approval_response", approval_id: currentApprovalId, action: action });
    addSystemMessage(action + ": " + currentApprovalId.split(":").pop());
    currentApprovalId = null;
    approvalModal.classList.add("hidden");
}

function riskColor(level) {
    const colors = { low: "#2ecc71", medium: "#f39c12", high: "#e74c3c", critical: "#ff0000" };
    return colors[level] || "#e0e0e0";
}

// === UI Helpers ===

function addBubble(role, text) {
    const div = document.createElement("div");
    div.className = "message message-" + role;
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollToBottom("chat-container");
    return div;
}

function addSystemMessage(text) {
    const div = document.createElement("div");
    div.className = "message message-assistant";
    div.style.opacity = "0.7";
    div.style.fontStyle = "italic";
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollToBottom("chat-container");
}

// === Auto-resize textarea ===

function autoResize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 150) + "px";
}

// === Send Message ===

function sendMessage() {
    const text = inputEl.value.trim();
    if (!text || isStreaming) return;
    addBubble("user", text);
    send({ type: "message", text: text });
    inputEl.value = "";
    autoResize();
}

// === Settings Panel ===

function loadModels() {
    fetch("/api/models")
        .then(r => r.json())
        .then(data => {
            modelSelect.innerHTML = "";
            const opt = document.createElement("option");
            opt.value = "";
            opt.textContent = t("common.default") + " (" + (data.default || "").split("/").pop() + ")";
            modelSelect.appendChild(opt);
            const groups = data.groups || {};
            if (Object.keys(groups).length > 0) {
                Object.keys(groups).forEach(provider => {
                    const optgroup = document.createElement("optgroup");
                    optgroup.label = provider.charAt(0).toUpperCase() + provider.slice(1);
                    groups[provider].forEach(m => {
                        const o = document.createElement("option");
                        o.value = m;
                        o.textContent = m.split("/").pop();
                        optgroup.appendChild(o);
                    });
                    modelSelect.appendChild(optgroup);
                });
            } else {
                (data.available || []).forEach(m => {
                    const o = document.createElement("option");
                    o.value = m;
                    o.textContent = m;
                    modelSelect.appendChild(o);
                });
            }
        })
        .catch(() => {});
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

// === Screen Preview ===

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

// === Skills Panel ===

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
            send({ type: "command", command: "skill", args: s.name });
        });
        container.appendChild(div);
    });
}

// === Bind Events ===

function bindEvents() {
    sendBtn.addEventListener("click", sendMessage);
    inputEl.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    inputEl.addEventListener("input", autoResize);

    btnApprove.addEventListener("click", () => respondApproval("approve"));
    btnDeny.addEventListener("click", () => respondApproval("deny"));
    btnTrust.addEventListener("click", () => respondApproval("trust"));

    document.getElementById("btn-screen").addEventListener("click", () => {
        screenPanel.classList.toggle("hidden");
        settingsPanel.classList.add("hidden");
        auditPanel.classList.add("hidden");
    });

    document.getElementById("btn-settings").addEventListener("click", () => {
        settingsPanel.classList.toggle("hidden");
        auditPanel.classList.add("hidden");
        screenPanel.classList.add("hidden");
        if (!settingsPanel.classList.contains("hidden")) {
            loadModels();
            loadSkills();
        }
    });

    document.getElementById("btn-audit").addEventListener("click", () => {
        auditPanel.classList.toggle("hidden");
        settingsPanel.classList.add("hidden");
        screenPanel.classList.add("hidden");
        if (!auditPanel.classList.contains("hidden")) loadAudit();
    });

    document.querySelectorAll(".panel-close").forEach(btn => {
        btn.addEventListener("click", () => {
            const panelId = btn.getAttribute("data-panel");
            document.getElementById(panelId).classList.add("hidden");
        });
    });

    modelSelect.addEventListener("change", () => {
        send({ type: "command", command: "model", args: modelSelect.value });
    });

    trustSelect.addEventListener("change", () => {
        send({ type: "command", command: "trust", args: trustSelect.value });
    });

    document.getElementById("btn-clear").addEventListener("click", () => {
        send({ type: "command", command: "clear" });
        messagesEl.innerHTML = "";
        settingsPanel.classList.add("hidden");
    });

    document.getElementById("btn-refresh-audit").addEventListener("click", loadAudit);
}

// === Init ===

async function init() {
    await initLayout({
        activePath: "/",
        rightButtons: [
            '<a href="/security" class="icon-btn" data-i18n-title="nav.security">&#128737;</a>',
            '<a href="/analytics" class="icon-btn" data-i18n-title="nav.analytics">&#128200;</a>',
            '<a href="/config" class="icon-btn" data-i18n-title="nav.settings">&#128736;</a>',
            '<button id="btn-screen" class="icon-btn" data-i18n-title="chat.screenPreview">&#128424;</button>',
            '<button id="btn-settings" class="icon-btn" data-i18n-title="settings.title">&#9881;</button>',
            '<button id="btn-audit" class="icon-btn" data-i18n-title="chat.auditLog">&#128220;</button>',
        ],
    });

    resolveDom();
    bindEvents();
    connect();

    onLocaleChange(() => {
        // Re-apply status text
        if (ws && ws.readyState === WebSocket.OPEN) {
            setStatus("connected");
        }
    });

    KuroPlugins.initAll();
    KuroPlugins.emit("onPageLoad", "chat");
}

init();
