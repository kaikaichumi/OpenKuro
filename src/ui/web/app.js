/**
 * Kuro 暗 - Web GUI Client
 * Vanilla JavaScript WebSocket client with streaming, approval, and settings.
 */
(function () {
    "use strict";

    // === State ===
    let ws = null;
    let reconnectTimer = null;
    let currentApprovalId = null;
    let isStreaming = false;
    let streamBubble = null;
    let streamText = "";

    // === DOM Elements ===
    const messagesEl = document.getElementById("messages");
    const inputEl = document.getElementById("user-input");
    const sendBtn = document.getElementById("btn-send");
    const modelBadge = document.getElementById("model-badge");
    const trustBadge = document.getElementById("trust-badge");
    const statusDot = document.getElementById("status-dot");
    const statusText = document.getElementById("status-text");

    // Modal
    const approvalModal = document.getElementById("approval-modal");
    const approvalTool = document.getElementById("approval-tool");
    const approvalRisk = document.getElementById("approval-risk");
    const approvalParams = document.getElementById("approval-params");
    const btnApprove = document.getElementById("btn-approve");
    const btnDeny = document.getElementById("btn-deny");
    const btnTrust = document.getElementById("btn-trust");

    // Panels
    const settingsPanel = document.getElementById("settings-panel");
    const auditPanel = document.getElementById("audit-panel");
    const screenPanel = document.getElementById("screen-panel");
    const modelSelect = document.getElementById("model-select");
    const trustSelect = document.getElementById("trust-select");

    // Screen preview
    const screenImage = document.getElementById("screen-image");
    const screenAction = document.getElementById("screen-action");
    const screenStep = document.getElementById("screen-step");
    const screenPlaceholder = document.getElementById("screen-placeholder");

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
            try {
                data = JSON.parse(event.data);
            } catch (e) {
                return;
            }
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
        var labels = { connected: "Connected", disconnected: "Disconnected", connecting: "Connecting..." };
        statusText.textContent = labels[state] || state;
    }

    // === Message Handling ===

    function handleMessage(data) {
        switch (data.type) {
            case "status":
                updateStatus(data);
                break;
            case "stream_start":
                startStream();
                break;
            case "stream_chunk":
                appendStream(data.text);
                break;
            case "stream_end":
                endStream();
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
                addSystemMessage("Error: " + data.message);
                break;
        }
    }

    function updateStatus(data) {
        if (data.model) {
            var short = data.model.split("/").pop();
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
            scrollToBottom();
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
        scrollToBottom();
    }

    // === Approval Modal ===

    function showApproval(data) {
        currentApprovalId = data.approval_id;
        approvalTool.textContent = "Tool: " + data.tool_name;
        approvalRisk.textContent = "Risk: " + data.risk_level.toUpperCase();
        approvalRisk.style.color = riskColor(data.risk_level);
        approvalParams.textContent = JSON.stringify(data.params, null, 2);
        approvalModal.classList.remove("hidden");
    }

    function respondApproval(action) {
        if (!currentApprovalId) return;
        send({ type: "approval_response", approval_id: currentApprovalId, action: action });
        addSystemMessage("Approval " + action + ": " + currentApprovalId.split(":").pop());
        currentApprovalId = null;
        approvalModal.classList.add("hidden");
    }

    function riskColor(level) {
        var colors = { low: "#2ecc71", medium: "#f39c12", high: "#e74c3c", critical: "#ff0000" };
        return colors[level] || "#e0e0e0";
    }

    // === UI Helpers ===

    function addBubble(role, text) {
        var div = document.createElement("div");
        div.className = "message message-" + role;
        div.textContent = text;
        messagesEl.appendChild(div);
        scrollToBottom();
        return div;
    }

    function addSystemMessage(text) {
        var div = document.createElement("div");
        div.className = "message message-assistant";
        div.style.opacity = "0.7";
        div.style.fontStyle = "italic";
        div.textContent = text;
        messagesEl.appendChild(div);
        scrollToBottom();
    }

    function scrollToBottom() {
        var container = document.getElementById("chat-container");
        container.scrollTop = container.scrollHeight;
    }

    // === Simple Markdown Renderer ===

    function renderMarkdown(text) {
        // Escape HTML first
        var html = text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");

        // Code blocks: ```lang\n...\n```
        html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function (_, lang, code) {
            return '<pre><code class="lang-' + lang + '">' + code + "</code></pre>";
        });

        // Inline code: `...`
        html = html.replace(/`([^`]+)`/g, "<code>$1</code>");

        // Bold: **...**
        html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

        // Italic: *...*
        html = html.replace(/(?<!\*)\*(?!\*)([^*]+)\*(?!\*)/g, "<em>$1</em>");

        // Links: [text](url)
        html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

        // Line breaks
        html = html.replace(/\n/g, "<br>");

        // Fix: undo <br> inside <pre>
        html = html.replace(/<pre><code([^>]*)>([\s\S]*?)<\/code><\/pre>/g, function (_, attrs, code) {
            return "<pre><code" + attrs + ">" + code.replace(/<br>/g, "\n") + "</code></pre>";
        });

        return html;
    }

    // === Auto-resize textarea ===

    function autoResize() {
        inputEl.style.height = "auto";
        inputEl.style.height = Math.min(inputEl.scrollHeight, 150) + "px";
    }

    // === Send Message ===

    function sendMessage() {
        var text = inputEl.value.trim();
        if (!text || isStreaming) return;

        addBubble("user", text);
        send({ type: "message", text: text });
        inputEl.value = "";
        autoResize();
    }

    // === Settings Panel ===

    function loadModels() {
        fetch("/api/models")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                modelSelect.innerHTML = "";
                // Add default option
                var opt = document.createElement("option");
                opt.value = "";
                opt.textContent = "Default (" + (data.default || "").split("/").pop() + ")";
                modelSelect.appendChild(opt);
                // Grouped by provider (preferred)
                var groups = data.groups || {};
                var hasGroups = Object.keys(groups).length > 0;
                if (hasGroups) {
                    Object.keys(groups).forEach(function (provider) {
                        var optgroup = document.createElement("optgroup");
                        optgroup.label = provider.charAt(0).toUpperCase() + provider.slice(1);
                        groups[provider].forEach(function (m) {
                            var o = document.createElement("option");
                            o.value = m;
                            o.textContent = m.split("/").pop();
                            optgroup.appendChild(o);
                        });
                        modelSelect.appendChild(optgroup);
                    });
                } else {
                    // Fallback: flat list
                    (data.available || []).forEach(function (m) {
                        var o = document.createElement("option");
                        o.value = m;
                        o.textContent = m;
                        modelSelect.appendChild(o);
                    });
                }
            })
            .catch(function () {});
    }

    function loadAudit() {
        fetch("/api/audit?limit=50")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var container = document.getElementById("audit-entries");
                container.innerHTML = "";
                (data.entries || []).forEach(function (entry) {
                    var div = document.createElement("div");
                    div.className = "audit-entry";
                    var time = entry.timestamp || entry.ts || "";
                    var tool = entry.tool_name || entry.event_type || "event";
                    var detail = entry.result_summary || entry.details || "";
                    div.innerHTML =
                        '<span class="audit-time">' + escapeHtml(time) + "</span> " +
                        '<span class="audit-tool">' + escapeHtml(tool) + "</span> " +
                        escapeHtml(detail);
                    container.appendChild(div);
                });
                if (!data.entries || data.entries.length === 0) {
                    container.innerHTML = '<div class="audit-entry">No audit entries yet.</div>';
                }
            })
            .catch(function () {});
    }

    function escapeHtml(str) {
        if (!str) return "";
        return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // === Screen Preview ===

    function showScreenUpdate(data) {
        // Auto-open panel on first screenshot
        if (screenPanel.classList.contains("hidden")) {
            screenPanel.classList.remove("hidden");
        }
        screenPlaceholder.classList.add("hidden");
        screenImage.classList.remove("hidden");
        screenImage.src = data.image;
        screenStep.textContent = "Step " + (data.step || 0);
        if (data.action) {
            screenAction.textContent = data.action;
            screenAction.classList.remove("hidden");
        }
    }

    function showScreenAction(data) {
        if (screenPanel.classList.contains("hidden")) {
            screenPanel.classList.remove("hidden");
        }
        screenStep.textContent = "Step " + (data.step || 0);
        screenAction.textContent = data.action || "";
        screenAction.classList.remove("hidden");
    }

    // === Skills Panel ===

    function loadSkills() {
        fetch("/api/skills")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                renderSkillsList(data.skills || []);
            })
            .catch(function () {});
    }

    function renderSkillsList(skills) {
        var container = document.getElementById("skills-list");
        if (!container) return;
        container.innerHTML = "";
        if (skills.length === 0) {
            container.innerHTML = '<div class="skill-item" style="opacity:0.5">No skills found. Place SKILL.md in ~/.kuro/skills/&lt;name&gt;/</div>';
            return;
        }
        skills.forEach(function (s) {
            var div = document.createElement("div");
            div.className = "skill-item";
            var dot = s.active ? "\u25cf" : "\u25cb";
            var cls = s.active ? "skill-active" : "skill-inactive";
            div.innerHTML = '<span class="' + cls + '">' + dot + "</span> " +
                '<span class="skill-name">' + escapeHtml(s.name) + "</span>" +
                '<span class="skill-desc"> \u2014 ' + escapeHtml(s.description) + "</span>";
            div.style.cursor = "pointer";
            div.addEventListener("click", function () {
                send({ type: "command", command: "skill", args: s.name });
            });
            container.appendChild(div);
        });
    }

    // === Event Listeners ===

    // Send
    sendBtn.addEventListener("click", sendMessage);
    inputEl.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    inputEl.addEventListener("input", autoResize);

    // Approval
    btnApprove.addEventListener("click", function () { respondApproval("approve"); });
    btnDeny.addEventListener("click", function () { respondApproval("deny"); });
    btnTrust.addEventListener("click", function () { respondApproval("trust"); });

    // Screen preview panel
    document.getElementById("btn-screen").addEventListener("click", function () {
        screenPanel.classList.toggle("hidden");
        settingsPanel.classList.add("hidden");
        auditPanel.classList.add("hidden");
    });

    // Settings panel
    document.getElementById("btn-settings").addEventListener("click", function () {
        settingsPanel.classList.toggle("hidden");
        auditPanel.classList.add("hidden");
        screenPanel.classList.add("hidden");
        if (!settingsPanel.classList.contains("hidden")) {
            loadModels();
            loadSkills();
        }
    });

    // Audit panel
    document.getElementById("btn-audit").addEventListener("click", function () {
        auditPanel.classList.toggle("hidden");
        settingsPanel.classList.add("hidden");
        screenPanel.classList.add("hidden");
        if (!auditPanel.classList.contains("hidden")) loadAudit();
    });

    // Close panels
    document.querySelectorAll(".panel-close").forEach(function (btn) {
        btn.addEventListener("click", function () {
            var panelId = btn.getAttribute("data-panel");
            document.getElementById(panelId).classList.add("hidden");
        });
    });

    // Model change
    modelSelect.addEventListener("change", function () {
        send({ type: "command", command: "model", args: modelSelect.value });
    });

    // Trust change
    trustSelect.addEventListener("change", function () {
        send({ type: "command", command: "trust", args: trustSelect.value });
    });

    // Clear conversation
    document.getElementById("btn-clear").addEventListener("click", function () {
        send({ type: "command", command: "clear" });
        messagesEl.innerHTML = "";
        settingsPanel.classList.add("hidden");
    });

    // Refresh audit
    document.getElementById("btn-refresh-audit").addEventListener("click", loadAudit);

    // === Init ===
    connect();
})();
