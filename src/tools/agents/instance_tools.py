"""Tools for managing Primary Agent instances at runtime.

These tools allow the main agent (or other authorized agents) to
create, delete, and list Primary Agent instances dynamically.
"""

from __future__ import annotations

import json
from typing import Any

from src.config import save_config
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class CreateAgentInstanceTool(BaseTool):
    """Create a new Primary Agent instance with its own memory and personality."""

    name = "create_agent_instance"
    description = (
        "Create a new Primary Agent instance. Each instance is a full AI persona "
        "with its own memory, personality, sub-agent pool, and optional bot binding. "
        "Provide at minimum an id, name, and model."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Unique identifier (e.g., 'research-bot', 'cs-agent')",
            },
            "name": {
                "type": "string",
                "description": "Display name (e.g., 'Research Assistant')",
            },
            "model": {
                "type": "string",
                "description": "LLM model to use (e.g., 'gemini/gemini-3-flash'). Empty = inherit main.",
            },
            "system_prompt": {
                "type": "string",
                "description": "Custom system prompt for this agent. Empty = inherit main.",
            },
            "memory_mode": {
                "type": "string",
                "enum": ["independent", "shared", "linked"],
                "description": "Memory mode: independent (own memory), shared (main's memory), linked (share with another agent)",
            },
            "linked_agents": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Agent IDs to link memory with (only for 'linked' mode)",
            },
            "personality_mode": {
                "type": "string",
                "enum": ["independent", "shared"],
                "description": "Personality mode: independent (own personality.md) or shared (main's)",
            },
            "persist": {
                "type": "boolean",
                "description": "Persist the new instance into config.yaml so it survives restart (default: false)",
            },
        },
        "required": ["id", "name"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.instance_manager
        if manager is None:
            return ToolResult.error(
                "Agent instance system is not enabled. "
                "Set agents.instances in config to enable."
            )

        from src.config import (
            AgentInstanceConfig,
            MemoryModeConfig,
        )

        instance_id = params["id"]
        name = params["name"]
        persist = bool(params.get("persist", False))

        root_cfg = getattr(manager, "_config", None)
        if root_cfg is None and context.config is not None and hasattr(context.config, "agents"):
            root_cfg = context.config

        # Check if already exists
        if manager.get(instance_id):
            return ToolResult.error(f"Agent instance '{instance_id}' already exists.")
        if root_cfg is not None:
            if any(cfg.id == instance_id for cfg in root_cfg.agents.instances):
                return ToolResult.error(
                    f"Agent instance '{instance_id}' already exists in configuration."
                )

        # Build config
        memory_cfg = MemoryModeConfig(
            mode=params.get("memory_mode", "independent"),
            linked_agents=params.get("linked_agents", []),
        )
        cfg = AgentInstanceConfig(
            id=instance_id,
            name=name,
            model=params.get("model") or None,
            system_prompt=params.get("system_prompt") or None,
            memory=memory_cfg,
            personality_mode=params.get("personality_mode", "independent"),
        )

        try:
            inst = await manager.create_instance(cfg)
            if persist:
                if root_cfg is None:
                    await manager.delete_instance(instance_id)
                    return ToolResult.error(
                        "Configuration is unavailable; cannot persist instance."
                    )
                root_cfg.agents.instances.append(cfg.model_copy(deep=True))
                save_config(root_cfg)

            return ToolResult.ok(
                f"Created Primary Agent instance '{inst.name}' (id={inst.id})\n"
                f"  Model: {cfg.model or 'inherited from main'}\n"
                f"  Memory: {cfg.memory.mode}\n"
                f"  Personality: {cfg.personality_mode}\n"
                f"  Persisted: {'Yes' if persist else 'No (runtime-only)'}"
            )
        except Exception as e:
            if persist:
                try:
                    await manager.delete_instance(instance_id)
                except Exception:
                    pass
            return ToolResult.error(f"Failed to create instance: {e}")


class DeleteAgentInstanceTool(BaseTool):
    """Delete a Primary Agent instance."""

    name = "delete_agent_instance"
    description = (
        "Delete a Primary Agent instance by ID. This removes the runtime instance "
        "but does NOT delete the agent's data directory (~/.kuro/agents/<id>/)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "ID of the agent instance to delete",
            },
        },
        "required": ["id"],
    }
    risk_level = RiskLevel.HIGH

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.instance_manager
        if manager is None:
            return ToolResult.error("Agent instance system is not enabled.")

        instance_id = params["id"]
        deleted = await manager.delete_instance(instance_id)
        if deleted:
            return ToolResult.ok(f"Deleted agent instance '{instance_id}'.")
        return ToolResult.error(f"Agent instance '{instance_id}' not found.")


class ListAgentInstancesTool(BaseTool):
    """List all Primary Agent instances and their sub-agents."""

    name = "list_agent_instances"
    description = (
        "List all Primary Agent instances with their configuration, memory mode, "
        "bot binding, and sub-agent pools."
    )
    parameters = {
        "type": "object",
        "properties": {},
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.instance_manager
        if manager is None:
            return ToolResult.ok("Agent instance system is not enabled. No instances.")

        instances = manager.list_all()
        if not instances:
            return ToolResult.ok("No Primary Agent instances configured.")

        lines = [f"Primary Agent Instances ({len(instances)}):"]
        for inst in instances:
            info = inst.get_info()
            lines.append(f"\n  [{info['id']}] {info['name']}")
            lines.append(f"    Model: {info['model'] or 'inherited'}")
            lines.append(f"    Memory: {info['memory_mode']}")
            if info['memory_linked_agents']:
                lines.append(f"    Linked to: {', '.join(info['memory_linked_agents'])}")
            lines.append(f"    Personality: {info['personality_mode']}")
            if info['bot_binding']:
                lines.append(
                    f"    Bot: {info['bot_binding']['adapter_type']} "
                    f"({info['bot_binding']['bot_token_env']})"
                )
            if info['sub_agents']:
                lines.append(f"    Sub-agents: {', '.join(info['sub_agents'])}")
            lines.append(f"    Active sessions: {info['active_sessions']}")

        return ToolResult.ok("\n".join(lines))
