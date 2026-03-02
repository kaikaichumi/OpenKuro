"""A2A tools: remote agent delegation and discovery.

These tools are auto-discovered and available to the main LLM
when A2A is enabled in configuration.
"""

from __future__ import annotations

from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class RemoteDelegateTool(BaseTool):
    """Delegate a task to an agent on a remote Kuro instance."""

    name = "remote_delegate"
    description = (
        "Delegate a task to a sub-agent running on a REMOTE Kuro instance. "
        "Use discover_remote_agents first to see what's available on the "
        "network. Remote agents may have different models or hardware "
        "(e.g., GPU servers for local models)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Name of the remote agent",
            },
            "task": {
                "type": "string",
                "description": "Task description for the remote agent",
            },
            "endpoint": {
                "type": "string",
                "description": "Remote instance URL (optional, auto-discovers if omitted)",
            },
        },
        "required": ["agent_name", "task"],
    }
    risk_level = RiskLevel.HIGH  # Cross-instance requires higher scrutiny

    # Only auto-discover this tool if A2A is configured
    _auto_discover = True

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Delegate to a remote agent."""
        # Get discovery service from engine (set during initialization)
        discovery = getattr(context, "_a2a_discovery", None)
        if discovery is None:
            # Try to get from a global or engine reference
            return ToolResult.fail(
                "A2A (Agent-to-Agent) is not enabled. "
                "Enable it in config: a2a.enabled = true"
            )

        agent_name = params.get("agent_name", "")
        task = params.get("task", "")
        endpoint = params.get("endpoint")

        if not agent_name:
            return ToolResult.fail("agent_name is required")
        if not task:
            return ToolResult.fail("task is required")

        try:
            response = await discovery.delegate_to_remote(
                agent_name=agent_name,
                task=task,
                endpoint=endpoint,
            )

            if response.success:
                result_str = str(response.result) if response.result else "(no output)"
                return ToolResult.ok(
                    f"[Remote Agent '{agent_name}' @ {response.instance_id}]\n"
                    f"Model: {response.model_used}, Duration: {response.duration_ms}ms\n\n"
                    f"{result_str}"
                )
            else:
                return ToolResult.fail(
                    f"Remote agent '{agent_name}' failed: {response.error}"
                )

        except Exception as e:
            return ToolResult.fail(f"Remote delegation error: {e}")


class DiscoverRemoteAgentsTool(BaseTool):
    """Discover agents available on remote Kuro instances."""

    name = "discover_remote_agents"
    description = (
        "Discover what agents are available on remote Kuro instances "
        "in the network. Refreshes the capability cache from all known peers."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    risk_level = RiskLevel.LOW

    _auto_discover = True

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Discover remote agents."""
        discovery = getattr(context, "_a2a_discovery", None)
        if discovery is None:
            return ToolResult.fail(
                "A2A (Agent-to-Agent) is not enabled. "
                "Enable it in config: a2a.enabled = true"
            )

        try:
            # Refresh capabilities from all peers
            results = await discovery.refresh_capabilities()

            if not results:
                peers = discovery.known_peers
                if not peers:
                    return ToolResult.ok(
                        "No remote peers configured. Add peers in config: "
                        "a2a.known_peers = ['http://host:port']"
                    )
                return ToolResult.ok(
                    f"No remote agents found. {len(peers)} peer(s) checked but "
                    "none responded or had registered agents."
                )

            lines = ["Remote agents discovered:"]
            for endpoint, caps in results.items():
                lines.append(f"\n  Instance: {endpoint}")
                for cap in caps:
                    specs = f", specialties: {', '.join(cap.specialties)}" if cap.specialties else ""
                    lines.append(
                        f"    - {cap.agent_name}: model={cap.model}{specs}"
                    )

            total = sum(len(caps) for caps in results.values())
            lines.append(f"\nTotal: {total} agents across {len(results)} instances")

            return ToolResult.ok("\n".join(lines))

        except Exception as e:
            return ToolResult.fail(f"Discovery error: {e}")
