"""Page schema registry for schema-driven Web UI navigation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

PageSchemaProvider = Callable[[], dict[str, Any]]


@dataclass
class _Provider:
    name: str
    order: int
    fn: PageSchemaProvider


class UIPageSchemaRegistry:
    """Collect page schema fragments for a specific page id."""

    def __init__(self) -> None:
        self._providers: dict[str, list[_Provider]] = {}

    def register(
        self,
        page_id: str,
        name: str,
        fn: PageSchemaProvider,
        *,
        order: int = 100,
    ) -> None:
        page = page_id.strip().lower()
        if not page:
            return
        providers = self._providers.setdefault(page, [])
        providers[:] = [p for p in providers if p.name != name]
        providers.append(_Provider(name=name, order=order, fn=fn))

    def list_pages(self) -> list[str]:
        return sorted(self._providers.keys())

    def build_page_schema(self, page_id: str) -> dict[str, Any] | None:
        page = page_id.strip().lower()
        if not page or page not in self._providers:
            return None
        categories: list[dict[str, Any]] = []
        sections: list[dict[str, Any]] = []
        forms_by_id: dict[str, dict[str, Any]] = {}
        seen_category_ids: set[str] = set()

        for provider in sorted(self._providers[page], key=lambda p: (p.order, p.name)):
            fragment = provider.fn() or {}
            fragment_categories = fragment.get("categories") or []
            if isinstance(fragment_categories, list):
                for cat in fragment_categories:
                    if not isinstance(cat, dict):
                        continue
                    cat_id = str(cat.get("id", "")).strip()
                    if not cat_id or cat_id in seen_category_ids:
                        continue
                    seen_category_ids.add(cat_id)
                    categories.append(cat)

            fragment_sections = fragment.get("sections") or []
            if isinstance(fragment_sections, list):
                for sec in fragment_sections:
                    if isinstance(sec, dict):
                        sections.append(sec)

            fragment_forms = fragment.get("forms") or []
            if isinstance(fragment_forms, list):
                for form in fragment_forms:
                    if not isinstance(form, dict):
                        continue
                    form_id = str(form.get("id", "")).strip()
                    if not form_id:
                        continue
                    existing = forms_by_id.get(form_id)
                    if existing is None:
                        forms_by_id[form_id] = copy.deepcopy(form)
                        continue
                    # Allow extensions to append extra sections/fields to an existing form id.
                    existing_sections = existing.setdefault("sections", [])
                    next_sections = form.get("sections") or []
                    if isinstance(existing_sections, list) and isinstance(next_sections, list):
                        existing_sections.extend(
                            copy.deepcopy([s for s in next_sections if isinstance(s, dict)])
                        )
                    if "order" in form and "order" not in existing:
                        existing["order"] = form["order"]

        categories.sort(key=lambda c: int(c.get("order", 999)))
        sections.sort(key=lambda s: int(s.get("order", 999)))
        forms = list(forms_by_id.values())
        forms.sort(key=lambda f: int(f.get("order", 999)))
        return {
            "version": 1,
            "page": page,
            "categories": categories,
            "sections": sections,
            "forms": forms,
        }


def build_agents_page_schema() -> dict[str, Any]:
    return {
        "categories": [
            {
                "id": "overview",
                "label": "Overview",
                "label_i18n": "panel.overview",
                "order": 10,
            },
            {
                "id": "management",
                "label": "Management",
                "label_i18n": "panel.management",
                "order": 20,
            },
        ],
        "sections": [
            {
                "id": "agents-overview",
                "category": "overview",
                "label": "Instance Overview",
                "label_i18n": "panel.agents.overview",
                "order": 10,
            },
            {
                "id": "main-subagents",
                "category": "management",
                "label": "Main Sub-Agents",
                "label_i18n": "panel.agents.mainSubAgents",
                "order": 20,
            },
            {
                "id": "instance-management",
                "category": "management",
                "label": "Instance Management",
                "label_i18n": "panel.agents.instances",
                "order": 30,
            },
        ],
        "forms": [
            {
                "id": "instance-editor",
                "label": "Instance Editor",
                "label_i18n": "agents.editInstance",
                "order": 10,
                "sections": [
                    {
                        "id": "instance-basic",
                        "title": "Basic",
                        "title_i18n": "panel.overview",
                        "order": 10,
                        "fields": [
                            {
                                "path": "id",
                                "type": "text",
                                "label_i18n": "agents.instanceId",
                                "placeholder": "e.g. customer-service",
                                "required": True,
                            },
                            {
                                "path": "name",
                                "type": "text",
                                "label_i18n": "agents.instanceName",
                                "placeholder": "e.g. Customer Service Bot",
                                "required": True,
                            },
                            {
                                "path": "enabled",
                                "type": "boolean",
                                "label_i18n": "agents.enabled",
                                "default": True,
                            },
                            {
                                "path": "model",
                                "type": "model",
                                "label_i18n": "agents.model",
                                "allow_empty": True,
                                "empty_label": "(inherit from main)",
                            },
                            {
                                "path": "temperature",
                                "type": "number",
                                "label_i18n": "agents.temperature",
                                "min": 0,
                                "max": 2,
                                "step": 0.1,
                                "nullable": True,
                            },
                        ],
                    },
                    {
                        "id": "instance-behavior",
                        "title": "Behavior",
                        "title_i18n": "panel.management",
                        "order": 20,
                        "fields": [
                            {
                                "path": "personality_mode",
                                "type": "select",
                                "label_i18n": "agents.personalityMode",
                                "default": "independent",
                                "options": [
                                    {"value": "independent", "label": "Independent"},
                                    {"value": "shared", "label": "Shared (use main)"},
                                ],
                            },
                            {
                                "path": "memory.mode",
                                "type": "select",
                                "label_i18n": "agents.memoryMode",
                                "default": "independent",
                                "options": [
                                    {"value": "independent", "label": "Independent"},
                                    {"value": "shared", "label": "Shared (use main)"},
                                    {"value": "linked", "label": "Linked (share longterm)"},
                                ],
                            },
                            {
                                "path": "memory.linked_agents",
                                "type": "csv",
                                "label_i18n": "agents.linkedAgents",
                                "visible_if": {"path": "memory.mode", "equals": "linked"},
                            },
                        ],
                    },
                    {
                        "id": "instance-bot",
                        "title": "Bot",
                        "title_i18n": "agents.botAdapter",
                        "order": 30,
                        "fields": [
                            {
                                "path": "bot_binding.adapter_type",
                                "type": "select",
                                "label_i18n": "agents.botAdapter",
                                "default": "",
                                "options": [
                                    {"value": "", "label": "None"},
                                    {"value": "discord", "label": "Discord"},
                                    {"value": "telegram", "label": "Telegram"},
                                    {"value": "slack", "label": "Slack"},
                                    {"value": "line", "label": "LINE"},
                                    {"value": "email", "label": "Email"},
                                ],
                            },
                            {
                                "path": "bot_binding.bot_token_env",
                                "type": "text",
                                "label_i18n": "agents.botTokenEnv",
                                "placeholder": "e.g. KURO_DISCORD_TOKEN_CS",
                                "required": True,
                                "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$",
                                "visible_if": {
                                    "path": "bot_binding.adapter_type",
                                    "not_equals": "",
                                },
                            },
                        ],
                    },
                    {
                        "id": "instance-prompt",
                        "title": "Prompt",
                        "title_i18n": "agents.systemPrompt",
                        "order": 40,
                        "fields": [
                            {
                                "path": "system_prompt",
                                "type": "textarea",
                                "label_i18n": "agents.systemPrompt",
                                "rows": 3,
                                "nullable": True,
                            },
                        ],
                    },
                    {
                        "id": "instance-security",
                        "title": "Security",
                        "title_i18n": "agents.securitySettings",
                        "order": 50,
                        "collapsible": True,
                        "fields": [
                            {
                                "path": "security.max_risk_level",
                                "type": "select",
                                "label": "Max Risk Level",
                                "default": "inherit",
                                "options": [
                                    {"value": "inherit", "label": "(inherit from main)"},
                                    {"value": "low", "label": "low"},
                                    {"value": "medium", "label": "medium"},
                                    {"value": "high", "label": "high"},
                                    {"value": "critical", "label": "critical"},
                                ],
                            },
                            {
                                "path": "security.auto_approve_levels",
                                "type": "csv",
                                "label_i18n": "agents.autoApproveLevels",
                            },
                            {
                                "path": "security.allowed_directories",
                                "type": "csv",
                                "label_i18n": "agents.allowedDirs",
                            },
                            {
                                "path": "security.blocked_commands",
                                "type": "csv",
                                "label_i18n": "agents.blockedCommands",
                            },
                            {
                                "path": "allowed_tools",
                                "type": "csv",
                                "label_i18n": "agents.allowedTools",
                            },
                            {
                                "path": "denied_tools",
                                "type": "csv",
                                "label_i18n": "agents.deniedTools",
                            },
                        ],
                    },
                    {
                        "id": "instance-features",
                        "title": "Feature Overrides",
                        "label_i18n": "agents.featureOverrides",
                        "order": 60,
                        "collapsible": True,
                        "fields": [
                            {
                                "path": "feature_overrides.context_compression_enabled_mode",
                                "type": "select",
                                "label": "Context Compression",
                                "default": "inherit",
                                "options": [
                                    {"value": "inherit", "label": "Inherit from main"},
                                    {"value": "enabled", "label": "Enabled"},
                                    {"value": "disabled", "label": "Disabled"},
                                ],
                            },
                            {
                                "path": "feature_overrides.context_compression_summarize_model",
                                "type": "model",
                                "label": "Compression Summarize Model",
                                "allow_empty": True,
                                "empty_label": "(inherit from main)",
                            },
                            {
                                "path": "feature_overrides.context_compression_trigger_threshold",
                                "type": "number",
                                "label": "Compression Trigger Threshold",
                                "min": 0,
                                "max": 1,
                                "step": 0.01,
                                "nullable": True,
                            },
                            {
                                "path": "feature_overrides.memory_lifecycle_enabled_mode",
                                "type": "select",
                                "label": "Memory Lifecycle",
                                "default": "inherit",
                                "options": [
                                    {"value": "inherit", "label": "Inherit from main"},
                                    {"value": "enabled", "label": "Enabled"},
                                    {"value": "disabled", "label": "Disabled"},
                                ],
                            },
                            {
                                "path": "feature_overrides.learning_enabled_mode",
                                "type": "select",
                                "label": "Learning",
                                "default": "inherit",
                                "options": [
                                    {"value": "inherit", "label": "Inherit from main"},
                                    {"value": "enabled", "label": "Enabled"},
                                    {"value": "disabled", "label": "Disabled"},
                                ],
                            },
                            {
                                "path": "feature_overrides.code_feedback_enabled_mode",
                                "type": "select",
                                "label": "Code Feedback",
                                "default": "inherit",
                                "options": [
                                    {"value": "inherit", "label": "Inherit from main"},
                                    {"value": "enabled", "label": "Enabled"},
                                    {"value": "disabled", "label": "Disabled"},
                                ],
                            },
                            {
                                "path": "feature_overrides.task_complexity_enabled_mode",
                                "type": "select",
                                "label": "Task Complexity",
                                "default": "inherit",
                                "options": [
                                    {"value": "inherit", "label": "Inherit from main"},
                                    {"value": "enabled", "label": "Enabled"},
                                    {"value": "disabled", "label": "Disabled"},
                                ],
                            },
                            {
                                "path": "feature_overrides.vision_image_analysis_mode_mode",
                                "type": "select",
                                "label": "Vision Analysis Mode",
                                "default": "inherit",
                                "options": [
                                    {"value": "inherit", "label": "Inherit from main"},
                                    {"value": "auto", "label": "Auto"},
                                    {"value": "always", "label": "Always"},
                                    {"value": "disabled", "label": "Disabled"},
                                ],
                            },
                        ],
                    },
                ],
            },
            {
                "id": "subagent-editor",
                "label": "Sub-Agent Editor",
                "label_i18n": "agents.editSubAgent",
                "order": 20,
                "sections": [
                    {
                        "id": "subagent-basic",
                        "title": "Basic",
                        "title_i18n": "panel.overview",
                        "order": 10,
                        "fields": [
                            {
                                "path": "name",
                                "type": "text",
                                "label_i18n": "agents.subAgentName",
                                "placeholder": "e.g. researcher",
                                "required": True,
                            },
                            {
                                "path": "model",
                                "type": "model",
                                "label_i18n": "agents.model",
                                "allow_empty": True,
                                "empty_label": "(inherit from main)",
                                "placeholder": "e.g. ollama/qwen3:32b",
                            },
                            {
                                "path": "system_prompt",
                                "type": "textarea",
                                "label_i18n": "agents.systemPrompt",
                                "rows": 3,
                                "nullable": True,
                            },
                            {
                                "path": "max_tool_rounds",
                                "type": "number",
                                "label_i18n": "agents.maxToolRounds",
                                "default": 5,
                                "min": 1,
                                "max": 50,
                                "step": 1,
                            },
                            {
                                "path": "complexity_tier",
                                "type": "select",
                                "label_i18n": "agents.complexityTier",
                                "default": "moderate",
                                "options": [
                                    {"value": "trivial", "label": "trivial"},
                                    {"value": "simple", "label": "simple"},
                                    {"value": "moderate", "label": "moderate"},
                                    {"value": "complex", "label": "complex"},
                                    {"value": "expert", "label": "expert"},
                                ],
                            },
                        ],
                    }
                ],
            },
        ],
    }


def build_dashboard_page_schema() -> dict[str, Any]:
    return {
        "categories": [
            {
                "id": "monitoring",
                "label": "Monitoring",
                "label_i18n": "panel.monitoring",
                "order": 10,
            },
        ],
        "sections": [
            {
                "id": "agent-filter-wrap",
                "category": "monitoring",
                "label": "Agent Filter",
                "label_i18n": "panel.dashboard.filter",
                "order": 10,
            },
            {
                "id": "stats-grid",
                "category": "monitoring",
                "label": "Stats",
                "label_i18n": "panel.dashboard.stats",
                "order": 20,
            },
            {
                "id": "agents-section",
                "category": "monitoring",
                "label": "Agent States",
                "label_i18n": "panel.dashboard.agentStates",
                "order": 30,
            },
            {
                "id": "timeline-section",
                "category": "monitoring",
                "label": "Timeline",
                "label_i18n": "panel.dashboard.timeline",
                "order": 40,
            },
        ],
    }


def build_security_page_schema() -> dict[str, Any]:
    return {
        "categories": [
            {
                "id": "overview",
                "label": "Overview",
                "label_i18n": "panel.overview",
                "order": 10,
            },
            {
                "id": "analysis",
                "label": "Analysis",
                "label_i18n": "panel.analysis",
                "order": 20,
            },
        ],
        "sections": [
            {
                "id": "security-score",
                "category": "overview",
                "label": "Security Score",
                "label_i18n": "panel.security.score",
                "order": 10,
            },
            {
                "id": "security-overview",
                "category": "overview",
                "label": "Overview Stats",
                "label_i18n": "panel.security.stats",
                "order": 20,
            },
            {
                "id": "security-risk",
                "category": "analysis",
                "label": "Risk Distribution",
                "label_i18n": "panel.security.risk",
                "order": 30,
            },
            {
                "id": "security-tools",
                "category": "analysis",
                "label": "Top Tools",
                "label_i18n": "panel.security.tools",
                "order": 40,
            },
            {
                "id": "security-hourly",
                "category": "analysis",
                "label": "Hourly Activity",
                "label_i18n": "panel.security.hourly",
                "order": 50,
            },
            {
                "id": "security-history",
                "category": "analysis",
                "label": "Approved vs Denied",
                "label_i18n": "panel.security.history",
                "order": 60,
            },
        ],
    }
