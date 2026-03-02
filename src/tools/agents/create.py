"""Dynamic agent creation/deletion tools (Phase 1).

Allows the LLM to create and destroy sub-agents at runtime, enabling
dynamic specialisation without requiring pre-configuration.
"""

from __future__ import annotations

from typing import Any

from src.core.types import AgentDefinition
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class CreateAgentTool(BaseTool):
    """Dynamically create a new sub-agent at runtime.

    Use when you need a specialist agent that doesn't exist yet.
    The created agent persists for the current session only.
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
            "inherit_context": {
                "type": "boolean",
                "description": "Whether the agent should see the parent conversation context",
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
            created_by="runtime",
            inherit_context=params.get("inherit_context", False),
        )
        agent_manager.register(defn)

        return ToolResult.ok(
            f"Agent '{name}' created successfully.\n"
            f"  Model: {model}\n"
            f"  System prompt: {defn.system_prompt[:80] or '(default)'}\n"
            f"  Inherit context: {defn.inherit_context}\n"
            f"You can now use delegate_to_agent to send tasks to '{name}'."
        )


class DeleteAgentTool(BaseTool):
    """Delete a runtime-created sub-agent."""

    name = "delete_agent"
    description = (
        "Delete a sub-agent that was created at runtime. "
        "Cannot delete predefined agents from configuration."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the agent to delete",
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

        defn = agent_manager.get_definition(name)
        if defn is None:
            return ToolResult.fail(f"Agent '{name}' not found")

        if defn.created_by == "config":
            return ToolResult.fail(
                f"Agent '{name}' is predefined in configuration and cannot be "
                f"deleted at runtime. Edit config.yaml to remove it."
            )

        if agent_manager.unregister(name):
            return ToolResult.ok(f"Agent '{name}' deleted successfully.")
        return ToolResult.fail(f"Failed to delete agent '{name}'")
