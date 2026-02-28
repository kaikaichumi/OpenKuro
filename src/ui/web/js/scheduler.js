/**
 * Kuro - Scheduler Management Module
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml, showToast } from "./utils.js";

let tasks = [];

async function loadTasks() {
    try {
        const resp = await fetch("/api/scheduler");
        const data = await resp.json();
        tasks = data.tasks || [];
        renderTasks();
    } catch (e) {
        showToast(t("scheduler.loadFailed") || "Failed to load tasks", "error");
    }
}

function renderTasks() {
    const dashboard = document.getElementById("dashboard");
    const loading = document.getElementById("loading");
    if (loading) loading.style.display = "none";

    let html = "";

    // Summary stats
    const enabled = tasks.filter(t => t.enabled).length;
    const withNotify = tasks.filter(t => t.notify_adapter).length;
    html += '<div class="stats-grid">';
    html += '<div class="stat-card"><div class="stat-value blue">' + tasks.length +
            '</div><div class="stat-label">' + (t("scheduler.totalTasks") || "Total Tasks") + '</div></div>';
    html += '<div class="stat-card"><div class="stat-value green">' + enabled +
            '</div><div class="stat-label">' + (t("scheduler.enabled") || "Enabled") + '</div></div>';
    html += '<div class="stat-card"><div class="stat-value yellow">' + withNotify +
            '</div><div class="stat-label">' + (t("scheduler.withNotify") || "With Notification") + '</div></div>';
    html += '</div>';

    // Task table
    html += '<div class="chart-section"><h3>' + (t("scheduler.taskList") || "Scheduled Tasks") + '</h3>';

    if (tasks.length === 0) {
        html += '<div style="color:var(--text-muted);font-size:0.85rem;padding:1rem 0">' +
                (t("scheduler.noTasks") || "No scheduled tasks. Create one by chatting with Kuro.") + '</div>';
    } else {
        html += '<div class="table-scroll"><table class="data-table"><thead><tr>';
        html += '<th>' + (t("scheduler.colStatus") || "Status") + '</th>';
        html += '<th>' + (t("scheduler.colName") || "Name") + '</th>';
        html += '<th>' + (t("scheduler.colType") || "Type") + '</th>';
        html += '<th>' + (t("scheduler.colSchedule") || "Schedule") + '</th>';
        html += '<th>' + (t("scheduler.colNotify") || "Notify") + '</th>';
        html += '<th>' + (t("scheduler.colNextRun") || "Next Run") + '</th>';
        html += '<th>' + (t("scheduler.colRuns") || "Runs") + '</th>';
        html += '<th></th>';
        html += '</tr></thead><tbody>';

        for (const task of tasks) {
            const statusBadge = task.enabled
                ? '<span class="badge">ON</span>'
                : '<span class="badge off">OFF</span>';
            const typeBadge = task.task_type === "agent"
                ? '<span class="badge" style="background:var(--info)">Agent</span>'
                : '<span class="badge off">Tool</span>';
            const notifyText = task.notify_adapter
                ? escapeHtml(task.notify_adapter) + ' <span style="color:var(--text-muted);font-size:0.75rem">' + escapeHtml((task.notify_user_id || "").slice(-6)) + '</span>'
                : '<span style="color:var(--text-muted)">-</span>';
            const schedText = formatSchedule(task);
            const nextRun = task.next_run ? formatTime(task.next_run) : "-";

            html += '<tr>';
            html += '<td>' + statusBadge + '</td>';
            html += '<td><strong>' + escapeHtml(task.name) + '</strong><br><span style="color:var(--text-muted);font-size:0.75rem">' + escapeHtml(task.id) + '</span></td>';
            html += '<td>' + typeBadge + ' <span style="font-size:0.8rem">' + escapeHtml(task.tool_name) + '</span></td>';
            html += '<td style="font-size:0.85rem">' + escapeHtml(schedText) + '</td>';
            html += '<td>' + notifyText + '</td>';
            html += '<td style="font-size:0.85rem">' + escapeHtml(nextRun) + '</td>';
            html += '<td>' + (task.run_count || 0) + '</td>';
            html += '<td><button class="btn btn-sm" data-edit="' + escapeHtml(task.id) + '">' + (t("common.edit") || "Edit") + '</button></td>';
            html += '</tr>';
        }

        html += '</tbody></table></div>';
    }
    html += '</div>';

    dashboard.innerHTML = html;

    // Bind edit buttons
    document.querySelectorAll("[data-edit]").forEach(btn => {
        btn.addEventListener("click", () => openEditModal(btn.getAttribute("data-edit")));
    });
}

function formatSchedule(task) {
    const type = task.schedule_type;
    if (type === "daily") return "Daily " + (task.schedule_time || "");
    if (type === "weekly") {
        const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
        const dayNames = (task.schedule_days || []).map(d => days[d] || d);
        return dayNames.join(",") + " " + (task.schedule_time || "");
    }
    if (type === "hourly") return "Every hour";
    if (type === "interval") return "Every " + (task.interval_minutes || "?") + " min";
    if (type === "once") return "Once " + (task.schedule_time || "");
    return type || "?";
}

function formatTime(isoStr) {
    try {
        const d = new Date(isoStr);
        const month = String(d.getMonth() + 1).padStart(2, "0");
        const day = String(d.getDate()).padStart(2, "0");
        const hour = String(d.getHours()).padStart(2, "0");
        const min = String(d.getMinutes()).padStart(2, "0");
        return month + "/" + day + " " + hour + ":" + min;
    } catch {
        return isoStr;
    }
}

// === Edit Modal ===

function openEditModal(taskId) {
    const task = tasks.find(t => t.id === taskId);
    if (!task) return;

    document.getElementById("edit-task-id").value = task.id;
    document.getElementById("modal-title").textContent =
        (t("scheduler.editTask") || "Edit Task") + ": " + task.name;

    document.getElementById("edit-notify-adapter").value = task.notify_adapter || "";
    document.getElementById("edit-notify-channel").value = task.notify_user_id || "";
    document.getElementById("edit-schedule-time").value = task.schedule_time || "";

    const agentGroup = document.getElementById("agent-task-group");
    if (task.task_type === "agent") {
        agentGroup.style.display = "block";
        document.getElementById("edit-agent-task").value = task.agent_task || "";
    } else {
        agentGroup.style.display = "none";
    }

    document.getElementById("edit-modal").style.display = "flex";
}

function closeEditModal() {
    document.getElementById("edit-modal").style.display = "none";
}

async function saveEdit() {
    const taskId = document.getElementById("edit-task-id").value;
    const updates = {};

    const adapter = document.getElementById("edit-notify-adapter").value;
    const channel = document.getElementById("edit-notify-channel").value.trim();
    const schedTime = document.getElementById("edit-schedule-time").value.trim();
    const agentTask = document.getElementById("edit-agent-task").value.trim();

    updates.notify_adapter = adapter;
    updates.notify_user_id = channel;
    if (schedTime) updates.schedule_time = schedTime;

    const task = tasks.find(t => t.id === taskId);
    if (task && task.task_type === "agent" && agentTask) {
        updates.agent_task = agentTask;
    }

    try {
        const resp = await fetch("/api/scheduler/" + encodeURIComponent(taskId), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updates),
        });
        const data = await resp.json();
        if (data.status === "ok") {
            showToast((t("scheduler.saved") || "Task updated") + " (" + data.message + ")");
            closeEditModal();
            loadTasks();
        } else {
            showToast(data.message || "Save failed", "error");
        }
    } catch (e) {
        showToast("Network error", "error");
    }
}

// === Init ===

function bindEvents() {
    document.getElementById("modal-cancel").addEventListener("click", closeEditModal);
    document.getElementById("modal-save").addEventListener("click", saveEdit);
    document.getElementById("edit-modal").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeEditModal();
    });
}

async function init() {
    await initLayout({ activePath: "/scheduler" });
    bindEvents();
    loadTasks();

    onLocaleChange(() => {
        renderTasks();
    });
}

init();
