/**
 * Kuro - Shared panel quick navigation (schema-driven).
 */

import { t } from "./i18n.js";

const state = {
    pageId: "",
    schema: null,
};

function titleFor(item, fallback = "") {
    if (item && item.label_i18n) {
        return t(item.label_i18n, item.label || fallback);
    }
    return (item && item.label) || fallback;
}

function collectVisibleSections() {
    const schema = state.schema || {};
    const sections = Array.isArray(schema.sections) ? schema.sections : [];
    return sections.filter((section) => {
        const id = String(section.id || "").trim();
        if (!id) return false;
        return !!document.getElementById(id);
    });
}

function renderPanelNav() {
    const root = document.getElementById("panel-nav");
    if (!root) return;

    const schema = state.schema || {};
    const categories = Array.isArray(schema.categories) ? schema.categories : [];
    const sections = collectVisibleSections();

    root.innerHTML = "";
    if (sections.length === 0) {
        const empty = document.createElement("div");
        empty.className = "panel-nav-empty";
        empty.textContent = t("common.loading", "Loading...");
        root.appendChild(empty);
        return;
    }

    const navTitle = document.createElement("div");
    navTitle.className = "panel-nav-title";
    navTitle.textContent = t("panel.quickNav", "Quick Navigation");
    root.appendChild(navTitle);

    const byCategory = new Map();
    for (const section of sections) {
        const category = String(section.category || "general");
        if (!byCategory.has(category)) byCategory.set(category, []);
        byCategory.get(category).push(section);
    }

    for (const category of categories) {
        const catId = String(category.id || "").trim();
        if (!catId || !byCategory.has(catId)) continue;

        const group = document.createElement("div");
        group.className = "panel-nav-group";
        const groupTitle = document.createElement("div");
        groupTitle.className = "panel-nav-group-title";
        groupTitle.textContent = titleFor(category, catId);
        group.appendChild(groupTitle);

        const items = byCategory.get(catId) || [];
        for (const section of items) {
            const id = String(section.id || "").trim();
            if (!id) continue;
            const link = document.createElement("a");
            link.href = `#${id}`;
            link.textContent = titleFor(section, id);
            group.appendChild(link);
        }
        root.appendChild(group);
    }

    for (const [category, items] of byCategory.entries()) {
        if (categories.some((c) => String(c.id || "") === category)) continue;
        const group = document.createElement("div");
        group.className = "panel-nav-group";
        const groupTitle = document.createElement("div");
        groupTitle.className = "panel-nav-group-title";
        groupTitle.textContent = category;
        group.appendChild(groupTitle);
        for (const section of items) {
            const id = String(section.id || "").trim();
            if (!id) continue;
            const link = document.createElement("a");
            link.href = `#${id}`;
            link.textContent = titleFor(section, id);
            group.appendChild(link);
        }
        root.appendChild(group);
    }
}

export async function initPanelNav(pageId) {
    state.pageId = String(pageId || "").trim().toLowerCase();
    state.schema = null;
    if (!state.pageId) {
        renderPanelNav();
        return;
    }

    try {
        const resp = await fetch(`/api/ui/schema/${encodeURIComponent(state.pageId)}`);
        if (resp.ok) {
            const data = await resp.json();
            state.schema = data.schema || null;
        }
    } catch {
        // Best-effort fallback: nav stays empty.
    }
    renderPanelNav();
}

export function refreshPanelNav() {
    renderPanelNav();
}
