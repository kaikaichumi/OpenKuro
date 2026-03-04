/**
 * Kuro - Real-time Agent Dashboard
 *
 * Displays live agent states, event timeline, and aggregated stats.
 * Connects via WebSocket to receive real-time agent_event pushes.
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml } from "./utils.js";

// ── State ──

let ws = null;
let reconnectTimer = null;
let stats = null;
let events = [];

// ── DOM refs ──

let statsGrid, agentsSection, timelineSection, loadingEl;
let statTotalEvents, statActiveAgents, statToolCalls, statErrors;
let agentStateCards, eventTimeline, liveDot;

function resolveDom() {
    statsGrid = document.getElementById("stats-grid");
    agentsSection = document.getElementById("agents-section");
    timelineSection = document.getElementById("timeline-section");
    loadingEl = document.getElementById("loading");

    statTotalEvents = document.getElementById("stat-total-events");
    statActiveAgents = document.getElementById("stat-active-agents");
    statToolCalls = document.getElementById("stat-tool-calls");
    statErrors = document.getElementById("stat-errors");

    agentStateCards = document.getElementById("agent-state-cards");
    eventTimeline = document.getElementById("event-timeline");
    liveDot = document.getElementById("live-dot");
}

// ── Data fetching ──

async function fetchInitialData() {
    try {
        const [statsRes, eventsRes] = await Promise.all([
            fetch("/api/dashboard/stats"),
            fetch("/api/dashboard/events?limit=100"),
        ]);

        if (statsRes.ok) {
            stats = await statsRes.json();
        }
        if (eventsRes.ok) {
            const data = await eventsRes.json();
            events = data.events || [];
        }

        loadingEl.style.display = "none";
        statsGrid.style.display = "";
        agentsSection.style.display = "";
        timelineSection.style.display = "";

        renderStats();
        renderAgentStates();
        renderTimeline();
    } catch (e) {
        loadingEl.textContent = t("dashboard.loadFailed") || "Failed to load dashboard";
    }
}

// ── Rendering ──

function renderStats() {
    if (!stats) return;
    statTotalEvents.textContent = stats.total_events || 0;

    // Count active agents
    const states = stats.agent_states || {};
    const active = Object.values(states).filter(s => s === "busy").length;
    statActiveAgents.textContent = active;

    // Sum tool calls and errors across agents
    const agents = stats.agents || {};
    let totalTools = 0;
    let totalErrors = 0;
    for (const a of Object.values(agents)) {
        totalTools += a.tool_calls || 0;
        totalErrors += a.errors || 0;
    }
    statToolCalls.textContent = totalTools;
    statErrors.textContent = totalErrors;
}

function renderAgentStates() {
    if (!stats) return;
    const states = stats.agent_states || {};
    const agentStats = stats.agents || {};
    agentStateCards.innerHTML = "";

    const agentIds = Object.keys(states);
    if (agentIds.length === 0) {
        agentStateCards.innerHTML = '<div class="empty-hint">' + (t("dashboard.noAgents") || "No agent activity yet") + "</div>";
        return;
    }

    for (const agentId of agentIds) {
        const state = states[agentId] || "idle";
        const as = agentStats[agentId] || {};
        const card = document.createElement("div");
        card.className = "agent-state-card";
        const dotClass = state === "busy" ? "status-on" : "status-off";
        card.innerHTML = `
            <div class="agent-state-header">
                <span class="status-dot ${dotClass}"></span>
                <strong>${escapeHtml(agentId)}</strong>
                <span class="badge">${state}</span>
            </div>
            <div class="agent-state-meta">
                <span>${t("dashboard.messages") || "Messages"}: ${as.messages || 0}</span>
                <span>${t("dashboard.toolCalls") || "Tools"}: ${as.tool_calls || 0}</span>
                <span>${t("dashboard.delegations") || "Delegations"}: ${as.delegations || 0}</span>
                <span>${t("dashboard.errors") || "Errors"}: ${as.errors || 0}</span>
            </div>
        `;
        agentStateCards.appendChild(card);
    }
}

function renderTimeline() {
    eventTimeline.innerHTML = "";
    if (events.length === 0) {
        eventTimeline.innerHTML = '<div class="empty-hint">' + (t("dashboard.noEvents") || "No events yet") + "</div>";
        return;
    }

    // Show most recent first, limited to 100
    const display = events.slice(-100).reverse();
    for (const evt of display) {
        appendTimelineEntry(evt, false);
    }
}

function appendTimelineEntry(evt, prepend = true) {
    const div = document.createElement("div");
    div.className = "timeline-entry timeline-" + (evt.event_type || "unknown");

    const ts = evt.timestamp ? new Date(evt.timestamp * 1000).toLocaleTimeString() : "";
    const icon = EVENT_ICONS[evt.event_type] || "\u25cf";
    const agent = evt.source_agent || "main";
    const target = evt.target_agent ? " \u2192 " + escapeHtml(evt.target_agent) : "";

    div.innerHTML = `
        <span class="timeline-icon">${icon}</span>
        <span class="timeline-time">${ts}</span>
        <span class="timeline-agent">${escapeHtml(agent)}${target}</span>
        <span class="timeline-type badge">${evt.event_type || "?"}</span>
        <span class="timeline-content">${escapeHtml(evt.content || "")}</span>
    `;

    if (prepend && eventTimeline.firstChild) {
        eventTimeline.insertBefore(div, eventTimeline.firstChild);
        // Keep timeline bounded
        while (eventTimeline.children.length > 100) {
            eventTimeline.removeChild(eventTimeline.lastChild);
        }
    } else {
        eventTimeline.appendChild(div);
    }
}

const EVENT_ICONS = {
    message_received: "\ud83d\udce8",  // 📨
    tool_call:        "\ud83d\udd27",  // 🔧
    tool_result:      "\u2705",         // ✅
    delegation:       "\ud83d\udd00",  // 🔀
    stream_start:     "\u25b6",         // ▶
    stream_end:       "\u23f9",         // ⏹
    response:         "\ud83d\udcac",  // 💬
    error:            "\u26a0\ufe0f",   // ⚠️
    status_change:    "\ud83d\udd04",  // 🔄
};

// ── WebSocket (live events) ──

function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/dashboard`;
    ws = new WebSocket(url);

    ws.onopen = function () {
        if (liveDot) liveDot.style.opacity = "1";
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
    };

    ws.onmessage = function (event) {
        let data;
        try { data = JSON.parse(event.data); } catch (e) { return; }
        if (data.type === "agent_event") {
            const evt = data.event;
            events.push(evt);
            appendTimelineEntry(evt, true);
            // Refresh stats periodically from new events
            updateStatsFromEvent(evt);
        }
    };

    ws.onclose = function () {
        if (liveDot) liveDot.style.opacity = "0.3";
        scheduleReconnect();
    };

    ws.onerror = function () {
        if (liveDot) liveDot.style.opacity = "0.3";
    };
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        connectWs();
    }, 5000);
}

function updateStatsFromEvent(evt) {
    if (!stats) return;
    stats.total_events = (stats.total_events || 0) + 1;
    statTotalEvents.textContent = stats.total_events;

    const agent = evt.source_agent || "main";
    if (!stats.agents[agent]) {
        stats.agents[agent] = { messages: 0, tool_calls: 0, delegations: 0, errors: 0, responses: 0 };
    }
    const a = stats.agents[agent];
    if (evt.event_type === "message_received") a.messages++;
    if (evt.event_type === "tool_call") a.tool_calls++;
    if (evt.event_type === "delegation") a.delegations++;
    if (evt.event_type === "error") a.errors++;
    if (evt.event_type === "response") a.responses++;

    // Update state
    if (!stats.agent_states) stats.agent_states = {};
    if (evt.event_type === "stream_start") stats.agent_states[agent] = "busy";
    if (evt.event_type === "stream_end" || evt.event_type === "response" || evt.event_type === "error") {
        stats.agent_states[agent] = "idle";
    }

    // Re-render summary counters
    renderStats();
    renderAgentStates();
}

// ── Init ──

async function init() {
    await initLayout({
        activePath: "/dashboard",
    });
    resolveDom();
    await fetchInitialData();
    connectWs();

    onLocaleChange(() => {
        renderStats();
        renderAgentStates();
    });
}

init();
