"""Team management tools: run, create, and list agent teams.

These tools are available to the main LLM for orchestrating teams.
"""

from __future__ import annotations

import json
from typing import Any

from src.core.teams.types import TeamDefinition, TeamRole
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class RunTeamTool(BaseTool):
    """Execute a team of agents working together on a task."""

    name = "run_team"
    description = (
        "Execute a registered agent team to collaboratively work on a task. "
        "Teams consist of multiple agents with different roles that share a "
        "workspace and communicate via messages. Use list_teams to see "
        "available teams. The team coordinator handles task distribution "
        "and result synthesis automatically."
    )
    parameters = {
        "type": "object",
        "properties": {
            "team_name": {
                "type": "string",
                "description": "Name of the team to run (e.g., 'research-team')",
            },
            "task": {
                "type": "string",
                "description": "The task description for the team to work on",
            },
        },
        "required": ["team_name", "task"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Run a team."""
        team_manager = getattr(context, "team_manager", None)
        if team_manager is None:
            return ToolResult.fail("Team system is not available")

        team_name = params.get("team_name", "")
        task = params.get("task", "")

        if not team_name:
            return ToolResult.fail("team_name is required")
        if not task:
            return ToolResult.fail("task is required")

        try:
            parent_session = getattr(context, "session", None)
            result = await team_manager.run_team(
                team_name, task, parent_session=parent_session
            )

            if result.success:
                return ToolResult.ok(
                    f"[Team '{team_name}' result]\n"
                    f"Rounds: {result.rounds_used}, "
                    f"Messages: {result.messages_exchanged}, "
                    f"Duration: {result.duration_ms}ms\n\n"
                    f"{result.final_output}"
                )
            else:
                return ToolResult.fail(
                    f"Team '{team_name}' failed: {result.error or result.final_output}"
                )
        except Exception as e:
            return ToolResult.fail(f"Team '{team_name}' execution error: {e}")


class CreateTeamTool(BaseTool):
    """Create a new agent team at runtime."""

    name = "create_team"
    description = (
        "Create a new agent team with specified roles. Each role maps to "
        "a registered agent. The team can then be run with run_team."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Unique name for the team",
            },
            "description": {
                "type": "string",
                "description": "What this team does",
            },
            "roles": {
                "type": "array",
                "description": "List of team roles",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Role name (e.g., 'researcher', 'writer')",
                        },
                        "agent_name": {
                            "type": "string",
                            "description": "Registered agent to use for this role",
                        },
                        "responsibility": {
                            "type": "string",
                            "description": "What this role is responsible for",
                        },
                    },
                    "required": ["name", "agent_name"],
                },
            },
            "coordinator_model": {
                "type": "string",
                "description": "Model for the team coordinator (optional)",
            },
            "max_rounds": {
                "type": "integer",
                "description": "Maximum coordination rounds (default: 5)",
            },
        },
        "required": ["name", "roles"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Create a new team."""
        team_manager = getattr(context, "team_manager", None)
        if team_manager is None:
            return ToolResult.fail("Team system is not available")

        name = params["name"]
        if team_manager.has_team(name):
            return ToolResult.fail(f"Team '{name}' already exists")

        # Build roles
        roles = []
        agent_manager = getattr(context, "agent_manager", None)
        for role_data in params.get("roles", []):
            role_name = role_data.get("name", "")
            agent_name = role_data.get("agent_name", "")

            # Validate agent exists
            if agent_manager and not agent_manager.has_agent(agent_name):
                return ToolResult.fail(
                    f"Agent '{agent_name}' for role '{role_name}' not found. "
                    f"Create it first with create_agent."
                )

            roles.append(TeamRole(
                name=role_name,
                agent_name=agent_name,
                responsibility=role_data.get("responsibility", ""),
            ))

        if not roles:
            return ToolResult.fail("At least one role is required")

        defn = TeamDefinition(
            name=name,
            description=params.get("description", ""),
            roles=roles,
            coordinator_model=params.get("coordinator_model"),
            max_rounds=params.get("max_rounds", 5),
            created_by="runtime",
        )
        team_manager.register(defn)

        role_names = ", ".join(r.name for r in roles)
        return ToolResult.ok(
            f"Team '{name}' created with roles: {role_names}\n"
            f"Use run_team to execute a task with this team."
        )


class ListTeamsTool(BaseTool):
    """List all registered agent teams."""

    name = "list_teams"
    description = (
        "List all registered agent teams with their roles and configuration. "
        "Use this to discover which teams are available for execution."
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
        """List all teams."""
        team_manager = getattr(context, "team_manager", None)
        if team_manager is None:
            return ToolResult.fail("Team system is not available")

        definitions = team_manager.list_definitions()
        if not definitions:
            return ToolResult.ok(
                "No teams registered. Use create_team to create one."
            )

        lines = ["Available teams:"]
        for defn in definitions:
            roles_str = ", ".join(
                f"{r.name}({r.agent_name})" for r in defn.roles
            )
            lines.append(
                f"- {defn.name}: {defn.description or '(no description)'}\n"
                f"  Roles: {roles_str}\n"
                f"  Max rounds: {defn.max_rounds}"
            )

        return ToolResult.ok("\n".join(lines))
