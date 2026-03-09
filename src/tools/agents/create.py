"""Dynamic agent creation/deletion tools (Phase 1).

Allows the LLM to create and destroy sub-agents at runtime, enabling
dynamic specialisation without requiring pre-configuration.
"""

from __future__ import annotations

from typing import Any

from src.config import AgentDefinitionConfig, save_config
from src.core.types import AgentDefinition
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

_ALLOWED_TIERS = {"trivial", "simple", "moderate", "complex", "expert"}


def _normalize_tier(value: Any) -> str:
    tier = str(value or "moderate").strip().lower()
    return tier if tier in _ALLOWED_TIERS else "moderate"


def _get_root_config(context: ToolContext):
    """Resolve the root KuroConfig used for persistence."""
    manager = getattr(context, "instance_manager", None)
    cfg = getattr(manager, "_config", None)
    if cfg is not None and hasattr(cfg, "agents"):
        return cfg
    cfg = getattr(context, "config", None)
    if cfg is not None and hasattr(cfg, "agents"):
        return cfg
    return None


def _resolve_persist_target(
    context: ToolContext,
    persist_scope: str,
    instance_id: str | None,
):
    """Resolve a persistence target list and label from scope selection."""
    root_cfg = _get_root_config(context)
    if root_cfg is None:
        raise RuntimeError("Configuration is unavailable; cannot persist agent")

    scope = (persist_scope or "current").strip().lower()
    if scope not in {"current", "main", "instance"}:
        raise RuntimeError("persist_scope must be one of: current, main, instance")

    if scope == "current":
        scope = "instance" if context.agent_instance_id else "main"

    if scope == "main":
        return root_cfg, root_cfg.agents.sub_agents, "main"

    target_instance_id = (instance_id or context.agent_instance_id or "").strip()
    if not target_instance_id:
        raise RuntimeError("instance_id is required when persist_scope=instance")

    target_cfg = next(
        (cfg for cfg in root_cfg.agents.instances if cfg.id == target_instance_id),
        None,
    )
    if target_cfg is None:
        raise RuntimeError(f"Instance '{target_instance_id}' not found in config")
    return root_cfg, target_cfg.sub_agents, f"instance:{target_instance_id}"


def _to_agent_config(defn: AgentDefinition) -> AgentDefinitionConfig:
    """Convert runtime AgentDefinition to persisted AgentDefinitionConfig."""
    return AgentDefinitionConfig(
        name=defn.name,
        model=defn.model,
        system_prompt=defn.system_prompt,
        allowed_tools=list(defn.allowed_tools),
        denied_tools=list(defn.denied_tools),
        max_tool_rounds=defn.max_tool_rounds,
        temperature=defn.temperature,
        max_tokens=defn.max_tokens,
        complexity_tier=defn.complexity_tier,
        max_depth=defn.max_depth,
        inherit_context=defn.inherit_context,
        output_schema=defn.output_schema,
    )


def _persist_agent_definition(
    defn: AgentDefinition,
    context: ToolContext,
    *,
    persist_scope: str = "current",
    instance_id: str | None = None,
    overwrite: bool = False,
) -> str:
    """Persist a sub-agent definition into config.yaml."""
    root_cfg, target_list, target_label = _resolve_persist_target(
        context=context,
        persist_scope=persist_scope,
        instance_id=instance_id,
    )

    existing_idx = next(
        (idx for idx, sa in enumerate(target_list) if sa.name == defn.name),
        None,
    )
    new_cfg = _to_agent_config(defn)
    if existing_idx is None:
        target_list.append(new_cfg)
    else:
        if not overwrite:
            raise RuntimeError(
                f"Persist target already has sub-agent '{defn.name}'. "
                "Set overwrite_persisted=true to replace it."
            )
        target_list[existing_idx] = new_cfg

    save_config(root_cfg)
    return target_label


def _remove_persisted_agent(
    name: str,
    context: ToolContext,
    *,
    persist_scope: str = "current",
    instance_id: str | None = None,
) -> str:
    """Remove a persisted sub-agent definition from config.yaml."""
    root_cfg, target_list, target_label = _resolve_persist_target(
        context=context,
        persist_scope=persist_scope,
        instance_id=instance_id,
    )
    before = len(target_list)
    target_list[:] = [sa for sa in target_list if sa.name != name]
    if len(target_list) == before:
        raise RuntimeError(
            f"Sub-agent '{name}' is not persisted under {target_label}"
        )
    save_config(root_cfg)
    return target_label


class CreateAgentTool(BaseTool):
    """Dynamically create a new sub-agent at runtime.

    Use when you need a specialist agent that doesn't exist yet.
    By default this is session-only, but it can also persist to config.
    """

    name = "create_agent"
    description = (
        "Create a new sub-agent at runtime with a specific model and "
        "configuration. Use when you need a specialist agent that doesn't "
        "exist yet. The agent will be available for delegation immediately."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Unique name for the new agent (e.g., 'translator', 'data-analyst')",
            },
            "model": {
                "type": "string",
                "description": (
                    "Model ID to use (e.g., 'gemini/gemini-2.5-flash', "
                    "'ollama/qwen3:32b', 'anthropic/claude-sonnet-4.5')"
                ),
            },
            "system_prompt": {
                "type": "string",
                "description": "Custom system prompt defining the agent's role and behavior",
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tool names this agent can use (empty = all tools)",
            },
            "denied_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tool names to deny for this agent",
            },
            "max_tool_rounds": {
                "type": "integer",
                "description": "Maximum number of tool-use rounds (default: 5)",
            },
            "complexity_tier": {
                "type": "string",
                "enum": ["trivial", "simple", "moderate", "complex", "expert"],
                "description": "Capability tier for complexity-based delegation routing (default: moderate)",
            },
            "inherit_context": {
                "type": "boolean",
                "description": "Whether the agent should see the parent conversation context",
            },
            "persist": {
                "type": "boolean",
                "description": "Persist this sub-agent into config.yaml so it survives restart (default: false)",
            },
            "persist_scope": {
                "type": "string",
                "enum": ["current", "main", "instance"],
                "description": "Where to persist: current context, main agent, or a specific instance",
            },
            "instance_id": {
                "type": "string",
                "description": "Target instance ID when persist_scope=instance",
            },
            "overwrite_persisted": {
                "type": "boolean",
                "description": "Replace existing persisted definition with same name (default: false)",
            },
        },
        "required": ["name", "model"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Create a new agent dynamically."""
        agent_manager = getattr(context, "agent_manager", None)
        if agent_manager is None:
            return ToolResult.fail("Agent system is not available")

        # Check if dynamic creation is allowed
        config = getattr(agent_manager, "config", None)
        if config and not config.agents.allow_dynamic_creation:
            return ToolResult.fail(
                "Dynamic agent creation is disabled in configuration"
            )

        name = params["name"]
        model = params["model"]
        persist = bool(params.get("persist", False))
        persist_scope = str(params.get("persist_scope", "current") or "current")
        instance_id = params.get("instance_id")
        overwrite_persisted = bool(params.get("overwrite_persisted", False))

        if agent_manager.has_agent(name):
            return ToolResult.fail(
                f"Agent '{name}' already exists. Use a different name or "
                f"delete it first with delete_agent."
            )

        defn = AgentDefinition(
            name=name,
            model=model,
            system_prompt=params.get("system_prompt", ""),
            allowed_tools=params.get("allowed_tools", []),
            denied_tools=params.get("denied_tools", []),
            max_tool_rounds=params.get("max_tool_rounds", 5),
            complexity_tier=_normalize_tier(params.get("complexity_tier")),
            created_by="runtime",
            inherit_context=params.get("inherit_context", False),
        )
        agent_manager.register(defn)

        persisted_note = "No (session-only)"
        if persist:
            try:
                persisted_to = _persist_agent_definition(
                    defn,
                    context,
                    persist_scope=persist_scope,
                    instance_id=instance_id,
                    overwrite=overwrite_persisted,
                )
                persisted_note = f"Yes ({persisted_to})"
            except Exception as e:
                agent_manager.unregister(name)
                return ToolResult.fail(f"Failed to persist agent '{name}': {e}")

        return ToolResult.ok(
            f"Agent '{name}' created successfully.\n"
            f"  Model: {model}\n"
            f"  Complexity tier: {defn.complexity_tier}\n"
            f"  System prompt: {defn.system_prompt[:80] or '(default)'}\n"
            f"  Inherit context: {defn.inherit_context}\n"
            f"  Persisted: {persisted_note}\n"
            f"You can now use delegate_to_agent to send tasks to '{name}'."
        )


class DeleteAgentTool(BaseTool):
    """Delete a runtime-created sub-agent."""

    name = "delete_agent"
    description = (
        "Delete a sub-agent runtime registration. "
        "Optionally also remove it from persisted config.yaml."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the agent to delete",
            },
            "remove_persisted": {
                "type": "boolean",
                "description": "Also remove this sub-agent from config.yaml (default: false)",
            },
            "persist_scope": {
                "type": "string",
                "enum": ["current", "main", "instance"],
                "description": "Where to remove persisted definition from",
            },
            "instance_id": {
                "type": "string",
                "description": "Target instance ID when persist_scope=instance",
            },
        },
        "required": ["name"],
    }
    risk_level = RiskLevel.LOW

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Delete a runtime-created agent."""
        agent_manager = getattr(context, "agent_manager", None)
        if agent_manager is None:
            return ToolResult.fail("Agent system is not available")

        name = params.get("name", "")
        if not name:
            return ToolResult.fail("name is required")

        remove_persisted = bool(params.get("remove_persisted", False))
        persist_scope = str(params.get("persist_scope", "current") or "current")
        instance_id = params.get("instance_id")

        defn = agent_manager.get_definition(name)
        if defn is None and not remove_persisted:
            return ToolResult.fail(f"Agent '{name}' not found")

        runtime_removed = False
        if defn is not None:
            if defn.created_by == "config" and not remove_persisted:
                return ToolResult.fail(
                    f"Agent '{name}' is predefined in configuration. "
                    "Set remove_persisted=true to remove it from config too."
                )
            runtime_removed = agent_manager.unregister(name)

        persisted_msg = "No"
        if remove_persisted:
            try:
                removed_from = _remove_persisted_agent(
                    name,
                    context,
                    persist_scope=persist_scope,
                    instance_id=instance_id,
                )
                persisted_msg = f"Yes ({removed_from})"
            except Exception as e:
                if runtime_removed and defn is not None:
                    agent_manager.register(defn)
                return ToolResult.fail(str(e))

        if runtime_removed or remove_persisted:
            return ToolResult.ok(
                f"Agent '{name}' deleted.\n"
                f"  Runtime removed: {'Yes' if runtime_removed else 'No (not registered)'}\n"
                f"  Persisted removed: {persisted_msg}"
            )
        return ToolResult.fail(f"Failed to delete agent '{name}'")
