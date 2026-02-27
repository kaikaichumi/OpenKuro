/**
 * Kuro - Plugin System
 *
 * Provides a simple event-based plugin registration system.
 * Plugins can hook into lifecycle events and extend the UI.
 *
 * Usage:
 *   KuroPlugins.register("my-plugin", {
 *     init() { ... },
 *     destroy() { ... },
 *     hooks: {
 *       onPageLoad(pageName) { ... },
 *       onMessage(data) { ... },
 *       onLocaleChange(locale) { ... },
 *     }
 *   });
 */

const KuroPlugins = {
    _plugins: {},
    _initialized: false,

    /**
     * Register a plugin.
     * @param {string} name - Unique plugin name
     * @param {Object} plugin - Plugin object with init, destroy, hooks
     */
    register(name, plugin) {
        if (this._plugins[name]) {
            console.warn(`[KuroPlugins] Plugin "${name}" already registered. Replacing.`);
            this.unregister(name);
        }
        this._plugins[name] = plugin;
        if (this._initialized && typeof plugin.init === "function") {
            try { plugin.init(); } catch (e) {
                console.error(`[KuroPlugins] init error for "${name}":`, e);
            }
        }
    },

    /**
     * Unregister a plugin.
     */
    unregister(name) {
        const plugin = this._plugins[name];
        if (plugin && typeof plugin.destroy === "function") {
            try { plugin.destroy(); } catch (e) {
                console.error(`[KuroPlugins] destroy error for "${name}":`, e);
            }
        }
        delete this._plugins[name];
    },

    /**
     * Emit an event to all plugin hooks.
     * @param {string} event - Hook name (e.g. "onPageLoad", "onMessage")
     * @param  {...any} args - Arguments to pass to the hook
     */
    emit(event, ...args) {
        for (const [name, plugin] of Object.entries(this._plugins)) {
            if (plugin.hooks && typeof plugin.hooks[event] === "function") {
                try {
                    plugin.hooks[event](...args);
                } catch (e) {
                    console.error(`[KuroPlugins] hook "${event}" error in "${name}":`, e);
                }
            }
        }
    },

    /**
     * Initialize all registered plugins.
     * Called once after the page is ready.
     */
    initAll() {
        this._initialized = true;
        for (const [name, plugin] of Object.entries(this._plugins)) {
            if (typeof plugin.init === "function") {
                try { plugin.init(); } catch (e) {
                    console.error(`[KuroPlugins] init error for "${name}":`, e);
                }
            }
        }
    },

    /**
     * Get all registered plugin names.
     */
    list() {
        return Object.keys(this._plugins);
    },

    /**
     * Get a specific plugin by name.
     */
    get(name) {
        return this._plugins[name] || null;
    },
};

// Expose globally for external plugins loaded via <script>
window.KuroPlugins = KuroPlugins;

export default KuroPlugins;
