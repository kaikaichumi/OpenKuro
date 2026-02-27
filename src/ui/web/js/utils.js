/**
 * Kuro - Shared Utility Functions
 */

export function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

export function renderMarkdown(text) {
    let html = text
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

export function formatTokens(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "K";
    return String(n);
}

export function showToast(message, type = "success") {
    let toast = document.getElementById("toast");
    if (!toast) {
        toast = document.createElement("div");
        toast.id = "toast";
        toast.className = "toast";
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.className = "toast " + type + " show";
    setTimeout(() => { toast.className = "toast"; }, 3000);
}

export function scrollToBottom(container) {
    if (typeof container === "string") {
        container = document.getElementById(container);
    }
    if (container) {
        container.scrollTop = container.scrollHeight;
    }
}
