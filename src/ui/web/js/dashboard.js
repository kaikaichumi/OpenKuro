/**
 * Kuro - Real-time Agent Dashboard
 *
 * Displays live agent states, event timeline, and aggregated stats.
 * Connects via WebSocket to receive real-time agent_event pushes.
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml } from "./utils.js";
import { initPanelNav, refreshPanelNav } from "./panel_nav.js";

let ws = null;
let reconnectTimer = null;
let stats = null;
let events = [];
let selectedAgent = "all";

let statsGrid, agentsSection, timelineSection, loadingEl;
let statTotalEvents, statActiveAgents, statToolCalls, statErrors;
let agentStateCards, eventTimeline, liveDot;
let agentFilterWrap, agentFilterSelect;

function normalizeStats(raw) {
    const normalized = raw && typeof raw === "object" ? raw : {};
    if (!normalized.agents || typeof normalized.agents !== "object") normalized.agents = {};
    if (!normalized.agent_states || typeof normalized.agent_states !== "object") normalized.agent_states = {};
    if (!Number.isFinite(normalized.total_events)) normalized.total_events = 0;

    const available = Array.isArray(normalized.available_agents)
        ? normalized.available_agents.filter(Boolean).map(String)
        : [];
    if (!available.includes("main")) available.unshift("main");
    normalized.available_agents = Array.from(new Set(available));
    return normalized;
}

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

    agentFilterWrap = document.getElementById("agent-filter-wrap");
    agentFilterSelect = document.getElementById("agent-filter");
}

async function fetchInitialData() {
    try {
        const [statsRes, eventsRes] = await Promise.all([
            fetch("/api/dashboard/stats"),
            fetch("/api/dashboard/events?limit=200"),
        ]);

        if (statsRes.ok) {
            stats = normalizeStats(await statsRes.json());
        } else {
            stats = normalizeStats({ total_events: 0, agents: {}, agent_states: {} });
        }

        if (eventsRes.ok) {
            const data = await eventsRes.json();
            events = data.events || [];
        } else {
            events = [];
        }

        loadingEl.style.display = "none";
        if (agentFilterWrap) agentFilterWrap.style.display = "";
        statsGrid.style.display = "";
        agentsSection.style.display = "";
        timelineSection.style.display = "";

        renderAgentFilter();
        renderStats();
        renderAgentStates();
        renderTimeline();
        refreshPanelNav();
    } catch (_e) {
        loadingEl.textContent = t("dashboard.loadFailed") || "Failed to load dashboard";
    }
}

function collectAgentIds() {
    const ids = new Set();
    if (stats) {
        const states = stats.agent_states || {};
        const agents = stats.agents || {};
        const availableAgents = Array.isArray(stats.available_agents) ? stats.available_agents : [];
        for (const id of Object.keys(states)) ids.add(id);
        for (const id of Object.keys(agents)) ids.add(id);
        for (const id of availableAgents) ids.add(id);
    }
    for (const evt of events) {
        if (evt && evt.source_agent) ids.add(evt.source_agent);
        if (evt && evt.target_agent) ids.add(evt.target_agent);
    }
    return Array.from(ids).filter(Boolean).sort();
}

function renderAgentFilter() {
    if (!agentFilterSelect) return;
    const current = selectedAgent || "all";
    const ids = collectAgentIds();

    agentFilterSelect.innerHTML = "";
    const allOpt = document.createElement("option");
    allOpt.value = "all";
    allOpt.textContent = t("dashboard.allAgents") || "All agents";
    agentFilterSelect.appendChild(allOpt);

    for (const id of ids) {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = id;
        agentFilterSelect.appendChild(opt);
    }

    const hasCurrent = Array.from(agentFilterSelect.options).some((o) => o.value === current);
    selectedAgent = hasCurrent ? current : "all";
    agentFilterSelect.value = selectedAgent;
}

function getAgentTotals(agentStats) {
    return agentStats.total_events
        || (agentStats.messages || 0)
            + (agentStats.tool_calls || 0)
            + (agentStats.delegations || 0)
            + (agentStats.errors || 0)
            + (agentStats.responses || 0);
}

function renderStats() {
    if (!stats) return;

    const states = stats.agent_states || {};
    const agents = stats.agents || {};

    if (selectedAgent === "all") {
        statTotalEvents.textContent = stats.total_events || 0;
        statActiveAgents.textContent = Object.values(states).filter((s) => s === "busy").length;

        let totalTools = 0;
        let totalErrors = 0;
        for (const a of Object.values(agents)) {
            totalTools += a.tool_calls || 0;
            totalErrors += a.errors || 0;
        }
        statToolCalls.textContent = totalTools;
        statErrors.textContent = totalErrors;
        return;
    }

    const a = agents[selectedAgent] || {};
    statTotalEvents.textContent = getAgentTotals(a);
    statActiveAgents.textContent = states[selectedAgent] === "busy" ? 1 : 0;
    statToolCalls.textContent = a.tool_calls || 0;
    statErrors.textContent = a.errors || 0;
}

function renderAgentStates() {
    if (!stats) return;
    const states = stats.agent_states || {};
    const agentStats = stats.agents || {};
    agentStateCards.innerHTML = "";

    const agentIds = selectedAgent === "all" ? collectAgentIds() : [selectedAgent];
    if (agentIds.length === 0) {
        agentStateCards.innerHTML =
            '<div class="empty-hint">' + (t("dashboard.noAgents") || "No agent activity yet") + "</div>";
        return;
    }

    for (const agentId of agentIds) {
        if (!agentId) continue;
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

function filterEventsForSelected() {
    if (selectedAgent === "all") return events;
    return events.filter((evt) => evt.source_agent === selectedAgent || evt.target_agent === selectedAgent);
}

function renderTimeline() {
    eventTimeline.innerHTML = "";
    const filtered = filterEventsForSelected();

    if (filtered.length === 0) {
        eventTimeline.innerHTML =
            '<div class="empty-hint">' + (t("dashboard.noEvents") || "No events yet") + "</div>";
        return;
    }

    const display = filtered.slice(-100).reverse();
    for (const evt of display) {
        appendTimelineEntry(evt, false);
    }
}

function appendTimelineEntry(evt, prepend = true) {
    const div = document.createElement("div");
    div.className = "timeline-entry timeline-" + (evt.event_type || "unknown");

    const ts = evt.timestamp ? new Date(evt.timestamp * 1000).toLocaleTimeString() : "";
    const icon = EVENT_ICONS[evt.event_type] || "*";
    const agent = evt.source_agent || "main";
    const target = evt.target_agent ? " -> " + escapeHtml(evt.target_agent) : "";

    const shellCommand = getShellCommandForEvent(evt);
    const detailId = shellCommand ? `timeline-cmd-${Math.random().toString(36).slice(2, 10)}` : "";
    if (shellCommand) {
        div.classList.add("has-detail");
    }

    div.innerHTML = `
        <span class="timeline-icon">${icon}</span>
        <span class="timeline-time">${ts}</span>
        <span class="timeline-agent">${escapeHtml(agent)}${target}</span>
        <span class="timeline-type badge">${evt.event_type || "?"}</span>
        <span class="timeline-content">${escapeHtml(evt.content || "")}</span>
        ${shellCommand ? `
        <button
            type="button"
            class="timeline-detail-toggle"
            data-toggle="shell-command"
            aria-expanded="false"
            aria-controls="${detailId}"
        >${escapeHtml(t("dashboard.viewCommand") || "查看指令")}</button>
        <pre id="${detailId}" class="timeline-detail" hidden>${escapeHtml(shellCommand)}</pre>
        ` : ""}
    `;

    if (shellCommand) {
        const toggleBtn = div.querySelector('[data-toggle="shell-command"]');
        const detailEl = div.querySelector(".timeline-detail");
        if (toggleBtn && detailEl) {
            toggleBtn.addEventListener("click", () => {
                const isHidden = detailEl.hasAttribute("hidden");
                if (isHidden) {
                    detailEl.removeAttribute("hidden");
                    toggleBtn.setAttribute("aria-expanded", "true");
                    toggleBtn.textContent = t("dashboard.hideCommand") || "隱藏指令";
                } else {
                    detailEl.setAttribute("hidden", "");
                    toggleBtn.setAttribute("aria-expanded", "false");
                    toggleBtn.textContent = t("dashboard.viewCommand") || "查看指令";
                }
            });
        }
    }

    if (prepend && eventTimeline.firstChild) {
        eventTimeline.insertBefore(div, eventTimeline.firstChild);
        while (eventTimeline.children.length > 100) {
            eventTimeline.removeChild(eventTimeline.lastChild);
        }
    } else {
        eventTimeline.appendChild(div);
    }
}

function getShellCommandForEvent(evt) {
    if (!evt || evt.event_type !== "tool_call") return "";
    const meta = evt.metadata && typeof evt.metadata === "object" ? evt.metadata : {};
    if (meta.tool_name !== "shell_execute") return "";
    return typeof meta.command === "string" ? meta.command : "";
}

const EVENT_ICONS = {
    message_received: "📨",
    tool_call: "🔧",
    tool_result: "✅",
    delegation: "🔀",
    stream_start: "▶",
    stream_end: "⏹",
    response: "💬",
    error: "⚠️",
    status_change: "🔄",
};

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
        try {
            data = JSON.parse(event.data);
        } catch (_e) {
            return;
        }
        if (data.type === "agent_event") {
            const evt = data.event;
            events.push(evt);
            if (
                selectedAgent === "all"
                || evt.source_agent === selectedAgent
                || evt.target_agent === selectedAgent
            ) {
                appendTimelineEntry(evt, true);
            }
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
    if (!stats.agents || typeof stats.agents !== "object") stats.agents = {};
    if (!stats.agent_states || typeof stats.agent_states !== "object") stats.agent_states = {};
    if (!Array.isArray(stats.available_agents)) stats.available_agents = ["main"];

    const agent = evt.source_agent || "main";
    if (!stats.agents[agent]) {
        stats.agents[agent] = {
            messages: 0,
            tool_calls: 0,
            delegations: 0,
            errors: 0,
            responses: 0,
            total_events: 0,
        };
    }
    const a = stats.agents[agent];
    a.total_events = (a.total_events || 0) + 1;
    if (evt.event_type === "message_received") a.messages++;
    if (evt.event_type === "tool_call") a.tool_calls++;
    if (evt.event_type === "delegation") a.delegations++;
    if (evt.event_type === "error") a.errors++;
    if (evt.event_type === "response") a.responses++;

    if (!stats.agent_states) stats.agent_states = {};
    if (evt.event_type === "stream_start") stats.agent_states[agent] = "busy";
    if (evt.event_type === "stream_end" || evt.event_type === "response" || evt.event_type === "error") {
        stats.agent_states[agent] = "idle";
    }
    if (!stats.available_agents.includes(agent)) stats.available_agents.push(agent);

    renderAgentFilter();
    renderStats();
    renderAgentStates();
}

async function init() {
    await initLayout({
        activePath: "/dashboard",
    });
    await initPanelNav("dashboard");
    resolveDom();

    if (agentFilterSelect) {
        agentFilterSelect.addEventListener("change", () => {
            selectedAgent = agentFilterSelect.value || "all";
            renderStats();
            renderAgentStates();
            renderTimeline();
        });
    }

    await fetchInitialData();
    connectWs();

    onLocaleChange(() => {
        renderAgentFilter();
        renderStats();
        renderAgentStates();
        renderTimeline();
        refreshPanelNav();
    });
}

init();
