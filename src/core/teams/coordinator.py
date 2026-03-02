"""TeamCoordinator: LLM-driven orchestration for agent teams.

The coordinator uses a language model to:
1. Plan initial task assignments for each team role
2. Evaluate progress after each round and decide next steps
3. Synthesize final results from all role outputs
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

from src.core.teams.types import TeamMessage, TeamRole
from src.core.teams.workspace import SharedWorkspace

if TYPE_CHECKING:
    from src.core.model_router import ModelRouter
    from src.config import KuroConfig

logger = structlog.get_logger()


class TeamCoordinator:
    """LLM-powered coordinator that orchestrates team execution.

    Responsible for:
    - Breaking down the user task into role-specific assignments
    - Evaluating each round's progress and deciding next actions
    - Determining when the team has completed the task
    - Synthesizing all role outputs into a final response
    """

    def __init__(
        self,
        model_router: ModelRouter,
        config: KuroConfig,
        coordinator_model: str | None = None,
    ) -> None:
        self.model = model_router
        self.config = config
        self._coordinator_model = coordinator_model

    async def plan_initial_assignments(
        self,
        task: str,
        roles: list[TeamRole],
    ) -> dict[str, str]:
        """Use LLM to create initial task assignments for each role.

        Returns a dict mapping role_name -> assignment_text.
        """
        role_descriptions = "\n".join(
            f"- {r.name} (agent: {r.agent_name}): {r.responsibility}"
            for r in roles
        )

        prompt = (
            "You are a team coordinator. Break down the following task into "
            "specific assignments for each team member.\n\n"
            f"Task: {task[:2000]}\n\n"
            f"Team Members:\n{role_descriptions}\n\n"
            "Rules:\n"
            "- Each member should get a focused, actionable assignment\n"
            "- Assignments should leverage each member's specialty\n"
            "- Members can communicate via messages and share data via workspace\n"
            "- Be specific about what each member should produce\n\n"
            "Respond with ONLY valid JSON:\n"
            "{\n"
            '  "assignments": {\n'
            '    "role_name": "specific assignment text",\n'
            '    ...\n'
            "  }\n"
            "}"
        )

        try:
            response = await self.model.complete(
                messages=[
                    {"role": "system", "content": "You are a team coordination AI. Respond only with JSON."},
                    {"role": "user", "content": prompt},
                ],
                model=self._coordinator_model,
                tools=None,
                temperature=0.3,
                max_tokens=1500,
            )

            data = self._parse_json(response.content or "")
            assignments = data.get("assignments", {})

            # Ensure all roles have assignments
            for role in roles:
                if role.name not in assignments:
                    assignments[role.name] = (
                        f"Support the team with your expertise as {role.name}. "
                        f"Your responsibility: {role.responsibility}"
                    )

            logger.info(
                "team_assignments_planned",
                role_count=len(assignments),
                task_preview=task[:80],
            )
            return assignments

        except Exception as e:
            logger.warning("team_planning_failed", error=str(e))
            # Fallback: give everyone the same generic task
            return {
                r.name: (
                    f"As the {r.name}, work on this task: {task[:500]}. "
                    f"Focus on: {r.responsibility}"
                )
                for r in roles
            }

    async def evaluate_round(
        self,
        task: str,
        workspace: SharedWorkspace,
        messages: list[TeamMessage],
        role_outputs: dict[str, str],
        round_num: int,
        max_rounds: int,
    ) -> dict[str, str] | None:
        """Evaluate progress and decide next actions.

        Returns:
            dict of role->assignment for the next round, or None if task is complete.
        """
        workspace_summary = await workspace.get_summary()

        # Build progress report
        outputs_text = ""
        for role, output in role_outputs.items():
            outputs_text += f"\n--- {role} ---\n{output[:1000]}\n"

        msg_summary = ""
        if messages:
            msg_summary = "\n".join(
                f"  [{m.from_role}→{m.to_role or 'all'}]: {m.content[:200]}"
                for m in messages[-10:]  # Last 10 messages
            )

        prompt = (
            f"You are evaluating round {round_num + 1}/{max_rounds} of a team task.\n\n"
            f"Original task: {task[:1000]}\n\n"
            f"Workspace state:\n{workspace_summary}\n\n"
            f"Role outputs this round:{outputs_text}\n\n"
            f"Recent messages:\n{msg_summary or '(none)'}\n\n"
            "Decide:\n"
            "1. Is the task COMPLETE? (all required work done, ready to synthesize)\n"
            "2. If not complete, what should each role do next?\n\n"
            "Respond with ONLY valid JSON:\n"
            "{\n"
            '  "complete": true/false,\n'
            '  "reason": "brief explanation",\n'
            '  "next_assignments": {\n'
            '    "role_name": "next task" (only if complete=false)\n'
            "  }\n"
            "}"
        )

        try:
            response = await self.model.complete(
                messages=[
                    {"role": "system", "content": "You are a team coordination AI. Respond only with JSON."},
                    {"role": "user", "content": prompt},
                ],
                model=self._coordinator_model,
                tools=None,
                temperature=0.2,
                max_tokens=1000,
            )

            data = self._parse_json(response.content or "")
            is_complete = data.get("complete", False)

            logger.info(
                "team_round_evaluated",
                round=round_num + 1,
                complete=is_complete,
                reason=data.get("reason", "")[:100],
            )

            if is_complete:
                return None

            return data.get("next_assignments", {})

        except Exception as e:
            logger.warning("team_evaluation_failed", error=str(e), round=round_num)
            # If we're past half the rounds, assume complete
            if round_num >= max_rounds // 2:
                return None
            return {}  # Empty dict = no new assignments, effectively complete

    async def synthesize_final(
        self,
        task: str,
        workspace: SharedWorkspace,
        role_outputs: dict[str, str],
    ) -> str:
        """Combine all role outputs into a coherent final response."""
        workspace_data = await workspace.read_all()

        outputs_text = ""
        for role, output in role_outputs.items():
            outputs_text += f"\n--- {role} ---\n{output[:3000]}\n"

        workspace_text = ""
        if workspace_data:
            workspace_text = "\nWorkspace data:\n"
            for k, v in workspace_data.items():
                workspace_text += f"  {k}: {str(v)[:500]}\n"

        prompt = (
            "Synthesize the following team results into a unified, coherent response.\n\n"
            f"Original task: {task[:1500]}\n\n"
            f"Team member outputs:{outputs_text}\n"
            f"{workspace_text}\n"
            "Instructions:\n"
            "- Combine all outputs into ONE coherent response\n"
            "- Address the original task directly\n"
            "- Resolve any conflicts between outputs\n"
            "- Use the user's language (detect from the original task)\n"
            "- Do NOT mention that a team was used — present as a unified answer"
        )

        try:
            response = await self.model.complete(
                messages=[
                    {"role": "system", "content": "You are a helpful assistant synthesizing team results."},
                    {"role": "user", "content": prompt},
                ],
                model=self._coordinator_model,
                tools=None,
                temperature=0.5,
                max_tokens=4096,
            )
            return response.content or "Failed to synthesize team results."

        except Exception as e:
            logger.warning("team_synthesis_failed", error=str(e))
            # Fallback: concatenate outputs
            parts = []
            for role, output in role_outputs.items():
                parts.append(f"**{role}**\n{output}")
            return "\n\n---\n\n".join(parts)

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        """Parse JSON from LLM response, handling markdown code blocks."""
        text = content.strip()
        if "```" in text:
            parts = text.split("```")
            for part in parts[1:]:
                clean = part.strip()
                if clean.startswith("json"):
                    clean = clean[4:].strip()
                if clean.startswith("{"):
                    text = clean
                    break

        return json.loads(text)
