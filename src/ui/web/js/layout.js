/**
 * Kuro - Shared Layout (Navigation Bar)
 *
 * Injects the navigation bar into each page's <nav id="main-nav"> placeholder.
 * Highlights the active page and includes the language switcher.
 */

import { initI18n, applyTranslations } from "./i18n.js";

const NAV_LINKS = [
    { href: "/", key: "nav.chat", label: "Chat" },
    { href: "/scheduler", key: "nav.scheduler", label: "Scheduler" },
    { href: "/security", key: "nav.security", label: "Security" },
    { href: "/analytics", key: "nav.analytics", label: "Analytics" },
    { href: "/collab", key: "nav.collab", label: "Collaboration" },
    { href: "/config", key: "nav.settings", label: "Settings" },
];

/**
 * Initialize the shared layout: inject nav, then init i18n.
 * Call this at the start of every page module.
 *
 * @param {Object} options
 * @param {string} options.activePath - Current page path (e.g. "/", "/config")
 * @param {string[]} [options.rightButtons] - Extra HTML for nav-right buttons
 */
export async function initLayout(options = {}) {
    const { activePath = window.location.pathname, rightButtons = [] } = options;
    injectNav(activePath, rightButtons);
    await initI18n();
}

function injectNav(activePath, rightButtons) {
    const nav = document.getElementById("main-nav");
    if (!nav) return;

    // Build nav links
    let linksHtml = NAV_LINKS.map(link => {
        const isActive = link.href === activePath ||
            (link.href !== "/" && activePath.startsWith(link.href));
        return `<a href="${link.href}" class="nav-link${isActive ? " active" : ""}" data-i18n="${link.key}">${link.label}</a>`;
    }).join("");

    // Build right section
    let rightHtml = rightButtons.join("");
    rightHtml += `
        <select id="lang-switcher">
            <option value="en">EN</option>
            <option value="zh-TW">繁中</option>
        </select>
    `;

    nav.innerHTML = `
        <div class="nav-inner">
            <div class="nav-left">
                <a href="/" class="logo">Kuro <span class="logo-kanji">暗</span></a>
            </div>
            <div class="nav-center">${linksHtml}</div>
            <div class="nav-right">${rightHtml}</div>
        </div>
    `;
}

/**
 * Re-apply translations after dynamic content changes.
 */
export function refreshTranslations() {
    applyTranslations();
}
