/**
 * Kuro - Collaboration Page Module
 */

import { initLayout } from "./layout.js";
import { t, onLocaleChange } from "./i18n.js";
import { escapeHtml, renderMarkdown, scrollToBottom } from "./utils.js";
import KuroPlugins from "./plugins.js";

// === State ===
let ws = null;
let myUserId = null;
let myDisplayName = null;
let sessionId = null;
let sessionName = null;
let inviteCode = null;
const participants = {};
const typingUsers = new Set();
let typingTimer = null;
let isTyping = false;
const colorPalette = ["#6c63ff", "#2ecc71", "#f39c12", "#bc8cff", "#f0883e", "#58a6ff", "#56d364"];
const userColors = {};

function userColor(uid) {
    if (!userColors[uid]) {
        userColors[uid] = colorPalette[Object.keys(userColors).length % colorPalette.length];
    }
    return userColors[uid];
}

function initials(name) {
    return (name || "?").split(" ").map(w => w[0]).join("").toUpperCase().slice(0, 2);
}

// === Setup ===

async function createSession() {
    const name = document.getElementById("create-name").value.trim();
    const sname = document.getElementById("session-name").value.trim() || "Collaboration";
    if (!name) { showError(t("collab.enterName")); return; }

    myDisplayName = name;
    myUserId = "user_" + Math.random().toString(36).slice(2, 8);

    try {
        const res = await fetch("/api/collab/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: sname, user_id: myUserId, display_name: name }),
        });
        if (!res.ok) { showError(t("collab.createFailed")); return; }
        const data = await res.json();
        enterSession(data.session_id, data.name, data.invite_code);
    } catch (e) {
        showError(t("collab.createFailed"));
    }
}

async function joinSession() {
    const name = document.getElementById("join-name").value.trim();
    const code = document.getElementById("invite-code").value.trim();
    if (!name) { showError(t("collab.enterName")); return; }
    if (!code) { showError(t("collab.enterCode")); return; }

    myDisplayName = name;
    myUserId = "user_" + Math.random().toString(36).slice(2, 8);

    try {
        const res = await fetch("/api/collab/join", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ invite_code: code, user_id: myUserId, display_name: name }),
        });
        const data = await res.json();
        if (data.error) { showError(data.error); return; }
        enterSession(data.session_id, data.name, null);
    } catch (e) {
        showError(t("collab.createFailed"));
    }
}

function enterSession(sid, sname, icode) {
    sessionId = sid;
    sessionName = sname;
    inviteCode = icode;

    document.getElementById("setup-panel").style.display = "none";
    document.getElementById("header-session").textContent = sname;

    if (inviteCode) {
        document.getElementById("invite-badge").textContent = inviteCode;
    } else {
        document.getElementById("invite-badge").style.display = "none";
    }

    connectWs();
}

function showError(msg) {
    const el = document.getElementById("setup-error");
    el.textContent = msg;
    el.style.display = "block";
}

function copyInvite() {
    if (!inviteCode) return;
    navigator.clipboard.writeText(inviteCode).catch(() => {});
    const hint = document.getElementById("copied-hint");
    hint.style.display = "inline";
    setTimeout(() => { hint.style.display = "none"; }, 2000);
}

// === WebSocket ===

function connectWs() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${location.host}/ws/collab/${sessionId}`);

    ws.onopen = () => {
        ws.send(JSON.stringify({ user_id: myUserId }));
    };

    ws.onmessage = (ev) => {
        const data = JSON.parse(ev.data);
        handleMessage(data);
    };

    ws.onclose = () => {
        appendSys(t("collab.disconnected"));
        setTimeout(connectWs, 3000);
    };

    ws.onerror = (e) => {
        console.error("WS error", e);
    };
}

function handleMessage(data) {
    KuroPlugins.emit("onMessage", data);
    switch (data.type) {
        case "collab_joined":
            appendSys(t("collab.joined") + ": " + data.name);
            data.participants.forEach(p => { participants[p.user_id] = p; });
            renderParticipants();
            enableInput();
            break;

        case "collab_presence":
            if (!participants[data.user_id]) {
                participants[data.user_id] = { user_id: data.user_id, display_name: data.display_name, is_online: data.online, is_typing: false, permissions: [] };
            } else {
                participants[data.user_id].is_online = data.online;
                participants[data.user_id].is_typing = false;
            }
            renderParticipants();
            appendSys(`${data.display_name} ${data.online ? "joined" : "left"}`);
            break;

        case "collab_stream_start":
            if (data.author_user_id !== myUserId) {
                appendUserMessage(data.author_user_id, data.author_name, data.text);
            }
            appendSys(t("collab.aiThinking"));
            break;

        case "collab_response":
            removeLastSys(t("collab.aiThinking"));
            appendAssistantMessage(data.response, data.author_name);
            break;

        case "collab_typing":
            if (data.is_typing) { typingUsers.add(data.display_name); }
            else { typingUsers.delete(data.display_name); }
            renderTyping();
            if (participants[data.user_id]) {
                participants[data.user_id].is_typing = data.is_typing;
                renderParticipants();
            }
            break;

        case "collab_approval_request":
            appendVotePanel(data);
            break;

        case "collab_vote_update":
            updateVotePanel(data);
            break;

        case "collab_approval_expired":
            removeVotePanel(data.approval_id);
            appendSys(t("collab.voteExpired") + ": " + data.tool_name);
            break;

        case "error":
            appendSys(data.message, true);
            break;
    }
}

// === Rendering ===

function renderParticipants() {
    const list = document.getElementById("participant-list");
    list.innerHTML = "";
    Object.values(participants).forEach(p => {
        const el = document.createElement("div");
        el.className = "participant" + (p.is_typing ? " typing" : "");
        const color = userColor(p.user_id);
        el.innerHTML = `
            <div class="avatar" style="background:${color}20;color:${color}">${initials(p.display_name)}</div>
            <div class="pname" title="${escapeHtml(p.display_name)}">${escapeHtml(p.display_name)}${p.user_id === myUserId ? " (you)" : ""}</div>
            <div class="typing-dot"></div>
            <div class="${p.is_online ? "online" : "offline"}"></div>
        `;
        list.appendChild(el);
    });
}

function renderTyping() {
    const el = document.getElementById("typing-indicator");
    const names = [...typingUsers];
    if (names.length === 0) {
        el.textContent = "";
    } else if (names.length === 1) {
        el.textContent = `${names[0]} is typing...`;
    } else {
        el.textContent = `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]} are typing...`;
    }
}

const msgContainer = document.getElementById("messages");

function appendUserMessage(userId, name, text) {
    const isSelf = userId === myUserId;
    const color = userColor(userId);
    const div = document.createElement("div");
    div.className = "msg" + (isSelf ? " self" : "");
    div.innerHTML = `
        <div class="msg-avatar" style="background:${color}20;color:${color}">${initials(name)}</div>
        <div>
            <div class="meta">${escapeHtml(name)}</div>
            <div class="bubble">${escapeHtml(text)}</div>
        </div>
    `;
    msgContainer.appendChild(div);
    scrollToBottom(msgContainer);
}

function appendAssistantMessage(text, triggeredBy) {
    const div = document.createElement("div");
    div.className = "msg assistant";
    div.innerHTML = `
        <div class="msg-avatar" style="background:#6c63ff20;color:#6c63ff">AI</div>
        <div>
            <div class="meta" style="color:var(--text-muted)">Kuro${triggeredBy ? " · via " + escapeHtml(triggeredBy) : ""}</div>
            <div class="bubble">${renderMarkdown(text)}</div>
        </div>
    `;
    msgContainer.appendChild(div);
    scrollToBottom(msgContainer);
}

function appendSys(text, isError = false) {
    const div = document.createElement("div");
    div.className = "sys-msg";
    div.style.color = isError ? "var(--danger)" : "var(--text-muted)";
    div.dataset.text = text;
    div.textContent = text;
    msgContainer.appendChild(div);
    scrollToBottom(msgContainer);
}

function removeLastSys(text) {
    const els = msgContainer.querySelectorAll(".sys-msg");
    for (let i = els.length - 1; i >= 0; i--) {
        if (els[i].dataset.text === text) { els[i].remove(); return; }
    }
}

function appendVotePanel(data) {
    const div = document.createElement("div");
    div.className = "vote-panel";
    div.id = "vote-" + data.approval_id;
    div.innerHTML = `
        <div class="vote-title">${t("collab.voteNeeded")}: ${escapeHtml(data.tool_name)}</div>
        <div class="vote-params">${escapeHtml(JSON.stringify(data.params, null, 2))}</div>
        <div class="vote-buttons">
            <button class="btn btn-secondary" onclick="window._castVote('${data.approval_id}', true)">${t("collab.allow")}</button>
            <button class="btn btn-danger" onclick="window._castVote('${data.approval_id}', false)">${t("approval.deny")}</button>
        </div>
        <div class="vote-progress" id="vote-prog-${data.approval_id}">
            ${t("collab.waitingVotes")} (${data.required_votes}/${data.total_approvers} required)
        </div>
        <div class="vote-bar"><div class="vote-bar-fill" id="vote-bar-${data.approval_id}" style="width:0%"></div></div>
    `;
    msgContainer.appendChild(div);
    scrollToBottom(msgContainer);
}

function updateVotePanel(data) {
    const prog = document.getElementById("vote-prog-" + data.approval_id);
    const bar = document.getElementById("vote-bar-" + data.approval_id);
    if (!prog) return;
    const pct = data.required ? Math.round((data.approve / data.required) * 100) : 0;
    if (bar) bar.style.width = Math.min(pct, 100) + "%";
    if (data.status === "approved") {
        prog.textContent = t("collab.voteApproved");
        removeVotePanel(data.approval_id, 2000);
    } else if (data.status === "denied") {
        prog.textContent = t("collab.voteDenied");
        removeVotePanel(data.approval_id, 2000);
    } else {
        prog.textContent = `${data.voter_name} voted · ${data.approve} approve / ${data.deny} deny · need ${data.required}`;
    }
}

function removeVotePanel(approvalId, delay = 0) {
    setTimeout(() => {
        const el = document.getElementById("vote-" + approvalId);
        if (el) el.remove();
    }, delay);
}

function castVote(approvalId, approve) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "vote", approval_id: approvalId, approve }));
}

// Expose for inline onclick
window._castVote = castVote;

function enableInput() {
    document.getElementById("msg-input").disabled = false;
    document.getElementById("send-btn-collab").disabled = false;
}

// === Send ===

function sendMessage() {
    const input = document.getElementById("msg-input");
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    appendUserMessage(myUserId, myDisplayName, text);
    ws.send(JSON.stringify({ type: "message", text }));
    input.value = "";
    input.style.height = "auto";
    sendTyping(false);
}

function sendTyping(typing) {
    if (ws && ws.readyState === WebSocket.OPEN && typing !== isTyping) {
        isTyping = typing;
        ws.send(JSON.stringify({ type: "typing", is_typing: typing }));
    }
}

// === Bind Events ===

function bindEvents() {
    document.getElementById("btn-create").addEventListener("click", createSession);
    document.getElementById("btn-join").addEventListener("click", joinSession);
    document.getElementById("invite-badge").addEventListener("click", copyInvite);
    document.getElementById("send-btn-collab").addEventListener("click", sendMessage);

    const msgInput = document.getElementById("msg-input");
    msgInput.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    msgInput.addEventListener("input", () => {
        msgInput.style.height = "auto";
        msgInput.style.height = Math.min(msgInput.scrollHeight, 140) + "px";
        sendTyping(true);
        clearTimeout(typingTimer);
        typingTimer = setTimeout(() => sendTyping(false), 2000);
    });
}

// === Init ===

async function init() {
    await initLayout({ activePath: "/collab" });

    bindEvents();

    onLocaleChange(() => {
        // Static text is handled by data-i18n
    });

    KuroPlugins.initAll();
    KuroPlugins.emit("onPageLoad", "collab");
}

init();
