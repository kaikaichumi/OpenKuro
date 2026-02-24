"""Delegate tool: allows the main LLM to delegate tasks to sub-agents."""

from __future__ import annotations

from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class DelegateToAgentTool(BaseTool):
    """Delegate a task to a named sub-agent.

    The main LLM can use this tool to hand off work to specialized agents
    that may use different models or have different tool access.
    """

    name = "delegate_to_agent"
    description = (
        "Delegate a task to a named sub-agent. You MUST use this tool to "
        "actually run a sub-agent — do NOT pretend to delegate by just writing "
        "text. Sub-agents run on their own LLM model (local or cloud) and "
        "process the task independently. Use list_agents first to see available "
        "agents. Always return the agent's actual result to the user."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": (
                    "Name of the agent to delegate to (e.g., 'fast', 'coder')"
                ),
            },
            "task": {
                "type": "string",
                "description": "The task description to send to the agent",
            },
        },
        "required": ["agent_name", "task"],
    }
    risk_level = RiskLevel.MEDIUM  # Medium because it triggers a sub-agent loop

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Delegate a task to the named agent."""
        agent_name = params.get("agent_name", "")
        task = params.get("task", "")

        if not agent_name:
            return ToolResult.fail("agent_name is required")
        if not task:
            return ToolResult.fail("task is required")

        # Access agent manager from context
        agent_manager = getattr(context, "agent_manager", None)
        if agent_manager is None:
            return ToolResult.fail("Agent system is not available")

        try:
            # Pass parent session so the sub-agent's approval callback
            # can find the correct channel (Discord/Telegram/etc.)
            parent_session = getattr(context, "session", None)
            result = await agent_manager.delegate(
                agent_name, task, parent_session=parent_session
            )
            return ToolResult.ok(f"[Agent '{agent_name}' result]\n{result}")
        except Exception as e:
            return ToolResult.fail(f"Agent '{agent_name}' failed: {e}")


class ListAgentsTool(BaseTool):
    """List available sub-agents."""

    name = "list_agents"
    description = (
        "List all registered sub-agents with their names, models, and "
        "capabilities. Use this to discover which agents are available "
        "for delegation."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """List all registered agents."""
        agent_manager = getattr(context, "agent_manager", None)
        if agent_manager is None:
            return ToolResult.fail("Agent system is not available")

        definitions = agent_manager.list_definitions()
        if not definitions:
            return ToolResult.ok(
                "No agents registered. Use /agent create to create one."
            )

        lines = ["Available agents:"]
        for defn in definitions:
            tools_info = ""
            if defn.allowed_tools:
                tools_info = f", tools: {', '.join(defn.allowed_tools)}"
            elif defn.denied_tools:
                tools_info = f", denied: {', '.join(defn.denied_tools)}"
            lines.append(
                f"- {defn.name}: model={defn.model}, "
                f"rounds={defn.max_tool_rounds}{tools_info}"
            )

        return ToolResult.ok("\n".join(lines))
