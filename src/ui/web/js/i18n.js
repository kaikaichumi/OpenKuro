/**
 * Kuro - Internationalization (i18n) Engine
 *
 * Usage:
 *   HTML: <span data-i18n="nav.chat">Chat</span>
 *   JS:   import { t } from './i18n.js';  t('nav.chat')
 *   Attr: <input data-i18n-placeholder="chat.placeholder">
 */

const STORAGE_KEY = "kuro-locale";
const DEFAULT_LOCALE = "en";
const SUPPORTED_LOCALES = ["en", "zh-TW"];

let currentLocale = DEFAULT_LOCALE;
let translations = {};
let loadedLocales = {};
const listeners = [];

/**
 * Initialize i18n: load saved locale and apply translations.
 */
export async function initI18n() {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && SUPPORTED_LOCALES.includes(saved)) {
        currentLocale = saved;
    }
    await loadLocale(currentLocale);
    applyTranslations();
    initSwitcher();
}

/**
 * Load a locale JSON file.
 */
async function loadLocale(locale) {
    if (loadedLocales[locale]) {
        translations = loadedLocales[locale];
        return;
    }
    try {
        const resp = await fetch(`/static/locales/${locale}.json`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        loadedLocales[locale] = data;
        translations = data;
    } catch (e) {
        console.warn(`[i18n] Failed to load locale "${locale}":`, e);
        // Fallback to default
        if (locale !== DEFAULT_LOCALE) {
            await loadLocale(DEFAULT_LOCALE);
        }
    }
}

/**
 * Get translation for a dot-separated key.
 * Returns the key itself if not found.
 */
export function t(key, fallback) {
    const parts = key.split(".");
    let val = translations;
    for (const part of parts) {
        if (val && typeof val === "object" && part in val) {
            val = val[part];
        } else {
            return fallback !== undefined ? fallback : key;
        }
    }
    return typeof val === "string" ? val : (fallback !== undefined ? fallback : key);
}

/**
 * Switch to a different locale.
 */
export async function setLocale(locale) {
    if (!SUPPORTED_LOCALES.includes(locale)) return;
    currentLocale = locale;
    localStorage.setItem(STORAGE_KEY, locale);
    await loadLocale(locale);
    applyTranslations();
    // Update switcher UI
    const switcher = document.getElementById("lang-switcher");
    if (switcher) switcher.value = locale;
    // Notify listeners
    for (const fn of listeners) {
        try { fn(locale); } catch (e) { console.error("[i18n] listener error:", e); }
    }
}

/**
 * Register a callback for locale changes.
 * Used by page modules to re-render dynamic content.
 */
export function onLocaleChange(fn) {
    listeners.push(fn);
}

/**
 * Get the current locale.
 */
export function getLocale() {
    return currentLocale;
}

/**
 * Scan DOM and apply translations to elements with data-i18n attributes.
 */
export function applyTranslations() {
    // Text content
    document.querySelectorAll("[data-i18n]").forEach(el => {
        const key = el.getAttribute("data-i18n");
        const val = t(key);
        if (val !== key) {
            el.textContent = val;
        }
    });

    // Placeholder attribute
    document.querySelectorAll("[data-i18n-placeholder]").forEach(el => {
        const key = el.getAttribute("data-i18n-placeholder");
        const val = t(key);
        if (val !== key) {
            el.placeholder = val;
        }
    });

    // Title attribute
    document.querySelectorAll("[data-i18n-title]").forEach(el => {
        const key = el.getAttribute("data-i18n-title");
        const val = t(key);
        if (val !== key) {
            el.title = val;
        }
    });
}

/**
 * Initialize the language switcher dropdown.
 */
function initSwitcher() {
    const switcher = document.getElementById("lang-switcher");
    if (!switcher) return;
    switcher.value = currentLocale;
    switcher.addEventListener("change", () => {
        setLocale(switcher.value);
    });
}

export { SUPPORTED_LOCALES, DEFAULT_LOCALE };
