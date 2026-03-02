"""TeamRunner & TeamManager: execution engine and lifecycle for agent teams.

TeamRunner orchestrates the execution of an Agent Team:
1. Sets up workspace and message bus
2. Coordinator assigns initial tasks
3. Iterates: run roles in parallel → coordinator evaluates → next round
4. Coordinator synthesizes final result

TeamManager is the registry and lifecycle manager (analogous to AgentManager).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from src.core.teams.coordinator import TeamCoordinator
from src.core.teams.message_bus import MessageBus
from src.core.teams.types import TeamDefinition, TeamResult, TeamRole
from src.core.teams.workspace import SharedWorkspace

if TYPE_CHECKING:
    from src.config import KuroConfig
    from src.core.agents import AgentManager
    from src.core.model_router import ModelRouter
    from src.core.types import Session

logger = structlog.get_logger()


class TeamRunner:
    """Executes an Agent Team task.

    Manages the full lifecycle of a team execution:
    - Initializes shared workspace and message bus
    - Coordinates multiple agent roles via TeamCoordinator
    - Runs roles in parallel using AgentManager
    - Collects and synthesizes results
    """

    def __init__(
        self,
        definition: TeamDefinition,
        agent_manager: AgentManager,
        model_router: ModelRouter,
        config: KuroConfig,
    ) -> None:
        self.definition = definition
        self.agents = agent_manager
        self.model = model_router
        self.config = config

        # Team infrastructure
        self.workspace = SharedWorkspace()
        self.message_bus = MessageBus()
        self.coordinator = TeamCoordinator(
            model_router=model_router,
            config=config,
            coordinator_model=definition.coordinator_model,
        )

    async def run(
        self,
        task: str,
        parent_session: Session | None = None,
    ) -> TeamResult:
        """Execute the team task.

        Main execution loop:
        1. Register all roles on the message bus
        2. Coordinator plans initial assignments
        3. For each round: run roles in parallel, evaluate progress
        4. Synthesize final result

        Args:
            task: The user task to work on.
            parent_session: Caller's session for approval callbacks.

        Returns:
            TeamResult with final output and metadata.
        """
        start = time.monotonic()
        role_outputs: dict[str, str] = {}

        # 1. Register all roles on the message bus
        for role in self.definition.roles:
            self.message_bus.register_role(role.name)

        # Validate that all role agents exist
        for role in self.definition.roles:
            if not self.agents.has_agent(role.agent_name):
                return TeamResult(
                    team_name=self.definition.name,
                    task=task,
                    final_output=f"Error: Agent '{role.agent_name}' for role "
                    f"'{role.name}' not found in agent registry.",
                    success=False,
                    error=f"Agent '{role.agent_name}' not found",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

        # 2. Coordinator plans initial assignments
        assignments = await self.coordinator.plan_initial_assignments(
            task, self.definition.roles
        )

        logger.info(
            "team_execution_started",
            team=self.definition.name,
            roles=[r.name for r in self.definition.roles],
            task_preview=task[:80],
        )

        rounds_used = 0

        # 3. Iterative execution loop
        for round_num in range(self.definition.max_rounds):
            rounds_used = round_num + 1

            # Check timeout
            elapsed = time.monotonic() - start
            if elapsed > self.definition.timeout_seconds:
                logger.warning(
                    "team_timeout",
                    team=self.definition.name,
                    elapsed=elapsed,
                    timeout=self.definition.timeout_seconds,
                )
                break

            # Run all roles that have assignments in parallel
            round_outputs = await self._run_round(
                assignments, parent_session
            )

            # Merge into cumulative outputs (latest overwrites)
            for role_name, output in round_outputs.items():
                role_outputs[role_name] = output

            # Coordinator evaluates progress
            next_assignments = await self.coordinator.evaluate_round(
                task=task,
                workspace=self.workspace,
                messages=self.message_bus.all_messages,
                role_outputs=role_outputs,
                round_num=round_num,
                max_rounds=self.definition.max_rounds,
            )

            if next_assignments is None:
                # Task is complete
                logger.info(
                    "team_task_complete",
                    team=self.definition.name,
                    rounds=rounds_used,
                )
                break

            if not next_assignments:
                # No new assignments → effectively complete
                break

            assignments = next_assignments

        # 4. Synthesize final result
        final_output = await self.coordinator.synthesize_final(
            task, self.workspace, role_outputs
        )

        duration_ms = int((time.monotonic() - start) * 1000)

        result = TeamResult(
            team_name=self.definition.name,
            task=task,
            final_output=final_output,
            role_outputs=role_outputs,
            messages_exchanged=self.message_bus.message_count,
            rounds_used=rounds_used,
            duration_ms=duration_ms,
            success=True,
        )

        logger.info(
            "team_execution_finished",
            team=self.definition.name,
            rounds=rounds_used,
            messages=self.message_bus.message_count,
            duration_ms=duration_ms,
        )

        return result

    async def _run_round(
        self,
        assignments: dict[str, str],
        parent_session: Session | None,
    ) -> dict[str, str]:
        """Run all roles that have assignments in parallel.

        Returns a dict of role_name -> output for this round.
        """
        # Build tasks for roles that have assignments
        role_map: dict[str, TeamRole] = {
            r.name: r for r in self.definition.roles
        }

        async def _run_single_role(role_name: str, assignment: str) -> tuple[str, str]:
            """Run a single role and return (role_name, output)."""
            role = role_map.get(role_name)
            if not role:
                return role_name, f"Error: Role '{role_name}' not found"

            try:
                output = await self._execute_role(role, assignment, parent_session)
                return role_name, output
            except Exception as e:
                logger.warning(
                    "team_role_failed",
                    role=role_name,
                    error=str(e),
                )
                return role_name, f"Error: {e}"

        # Run all assigned roles in parallel
        tasks = [
            _run_single_role(role_name, assignment)
            for role_name, assignment in assignments.items()
            if role_name in role_map
        ]

        if not tasks:
            return {}

        results = await asyncio.gather(*tasks, return_exceptions=False)
        return dict(results)

    async def _execute_role(
        self,
        role: TeamRole,
        assignment: str,
        parent_session: Session | None,
    ) -> str:
        """Execute a single role's task with workspace and message context.

        Builds an enhanced task prompt that includes:
        - Role identity and responsibility
        - Current workspace state
        - Pending peer messages
        - The specific assignment
        """
        # Gather context from workspace and messages
        workspace_summary = await self.workspace.get_summary()
        peer_messages = await self.message_bus.receive(role.name)
        messages_text = self.message_bus.format_messages(peer_messages)

        # Build enhanced task for the agent
        enhanced_task = (
            f"[Your Role: {role.name}]\n"
            f"{role.responsibility}\n\n"
            f"[Task Assignment]\n"
            f"{assignment}\n\n"
            f"{workspace_summary}\n\n"
            f"{messages_text}\n\n"
            f"[Instructions]\n"
            f"- Focus on your assigned task as {role.name}\n"
            f"- Produce clear, actionable output\n"
            f"- If you have data to share with teammates, include it in your response"
        )

        # Run the agent
        result = await self.agents.run_agent(
            role.agent_name,
            enhanced_task,
            parent_session=parent_session,
        )

        # If result is a dict (structured output), convert to string for storage
        if isinstance(result, dict):
            import json
            result_str = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            result_str = result

        # Auto-write role output to workspace
        await self.workspace.write(
            f"output:{role.name}",
            result_str[:5000],  # Limit size
            writer_role=role.name,
        )

        return result_str


class TeamManager:
    """Registry and lifecycle manager for Agent Teams.

    Analogous to AgentManager but for teams. Manages team definitions,
    tracks running teams, and enforces concurrency limits.
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        model_router: ModelRouter,
        config: KuroConfig,
    ) -> None:
        self.agents = agent_manager
        self.model = model_router
        self.config = config

        # Team definitions registry
        self._definitions: dict[str, TeamDefinition] = {}

        # Running teams
        self._running: dict[str, TeamRunner] = {}

    def register(self, definition: TeamDefinition) -> None:
        """Register a new team definition."""
        self._definitions[definition.name] = definition
        logger.info(
            "team_registered",
            name=definition.name,
            roles=[r.name for r in definition.roles],
        )

    def unregister(self, name: str) -> bool:
        """Remove a team definition. Returns True if found."""
        if name in self._definitions:
            del self._definitions[name]
            return True
        return False

    def has_team(self, name: str) -> bool:
        """Check if a team with the given name is registered."""
        return name in self._definitions

    def get_definition(self, name: str) -> TeamDefinition | None:
        """Get a team definition by name."""
        return self._definitions.get(name)

    def list_definitions(self) -> list[TeamDefinition]:
        """List all registered team definitions."""
        return list(self._definitions.values())

    async def run_team(
        self,
        name: str,
        task: str,
        parent_session: Session | None = None,
    ) -> TeamResult:
        """Run a registered team with the given task.

        Args:
            name: Team name to run.
            task: Task description for the team.
            parent_session: Caller's session for approval callbacks.

        Returns:
            TeamResult with final output and metadata.
        """
        defn = self._definitions.get(name)
        if defn is None:
            return TeamResult(
                team_name=name,
                task=task,
                final_output=f"Error: Team '{name}' not found.",
                success=False,
                error=f"Team '{name}' not found",
            )

        # Concurrency check
        max_concurrent = getattr(
            self.config, "_teams_max_concurrent", 2
        )
        if hasattr(self.config, "teams"):
            max_concurrent = self.config.teams.max_concurrent_teams

        if len(self._running) >= max_concurrent:
            return TeamResult(
                team_name=name,
                task=task,
                final_output=f"Error: Maximum concurrent teams ({max_concurrent}) reached.",
                success=False,
                error="Concurrency limit reached",
            )

        runner = TeamRunner(
            definition=defn,
            agent_manager=self.agents,
            model_router=self.model,
            config=self.config,
        )

        run_key = f"{name}:{id(runner)}"
        self._running[run_key] = runner
        try:
            return await runner.run(task, parent_session)
        finally:
            self._running.pop(run_key, None)

    @property
    def definition_count(self) -> int:
        """Number of registered team definitions."""
        return len(self._definitions)

    @property
    def running_count(self) -> int:
        """Number of currently running teams."""
        return len(self._running)
