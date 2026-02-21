"""Workflow engine: composable multi-step automation with agent orchestration.

Workflows are YAML-defined sequences of steps that can:
- Execute tools with parameters
- Delegate to sub-agents with prompts
- Use template variables between steps
- Branch on conditions
- Trigger from scheduler or manually

Example workflow:
    name: daily-report
    trigger:
      schedule: "daily 09:00"
    steps:
      - tool: shell_execute
        params: { command: "git log --since='1 day ago' --oneline" }
        output: git_changes
      - agent: cloud
        prompt: "Summarize: {{git_changes}}"
        output: report
      - tool: file_write
        params: { path: "~/reports/{{date}}.md", content: "{{report}}" }
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

import aiofiles

from src.config import get_kuro_home

logger = logging.getLogger(__name__)


@dataclass
class WorkflowStep:
    """A single step in a workflow."""

    # One of these must be set:
    tool: str | None = None           # Tool name to execute
    agent: str | None = None          # Agent name to delegate to
    prompt: str | None = None         # Prompt for agent (supports {{var}})

    # Tool parameters (supports {{var}} templates)
    params: dict[str, Any] = field(default_factory=dict)

    # Output variable name (result stored here for later steps)
    output: str | None = None

    # Optional condition (simple expression like "{{var}} != empty")
    condition: str | None = None

    # Step name for logging
    name: str | None = None


@dataclass
class WorkflowTrigger:
    """When a workflow should run."""

    # Manual (default), or schedule expression
    schedule: str | None = None       # e.g., "daily 09:00", "hourly", "weekly mon 10:00"
    on_event: str | None = None       # Future: event-based triggers


@dataclass
class WorkflowDefinition:
    """Complete workflow definition."""

    name: str
    description: str = ""
    trigger: WorkflowTrigger = field(default_factory=WorkflowTrigger)
    steps: list[WorkflowStep] = field(default_factory=list)
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class WorkflowRun:
    """A single execution of a workflow."""

    workflow_name: str
    run_id: str = ""
    status: str = "pending"  # pending, running, completed, failed
    started_at: str = ""
    completed_at: str = ""
    step_results: list[dict[str, Any]] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    error: str | None = None


# Type for tool/agent execution callbacks
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]
AgentExecutor = Callable[[str, str], Awaitable[str]]


def _resolve_template(text: str, variables: dict[str, str]) -> str:
    """Replace {{var}} placeholders with variable values."""
    def replacer(match: re.Match) -> str:
        var_name = match.group(1).strip()
        if var_name == "date":
            return datetime.now().strftime("%Y-%m-%d")
        if var_name == "datetime":
            return datetime.now().strftime("%Y-%m-%d_%H-%M")
        if var_name == "time":
            return datetime.now().strftime("%H:%M")
        return variables.get(var_name, match.group(0))

    return re.sub(r"\{\{(\w+)\}\}", replacer, text)


def _resolve_params(params: dict[str, Any], variables: dict[str, str]) -> dict[str, Any]:
    """Resolve template variables in all parameter values."""
    resolved = {}
    for key, value in params.items():
        if isinstance(value, str):
            resolved[key] = _resolve_template(value, variables)
        else:
            resolved[key] = value
    return resolved


def _check_condition(condition: str, variables: dict[str, str]) -> bool:
    """Evaluate a simple condition string.

    Supported: "{{var}} != empty", "{{var}} == value", "{{var}}"
    """
    if not condition:
        return True

    resolved = _resolve_template(condition, variables)

    # "X != empty" — check if X is non-empty
    if "!= empty" in resolved:
        value = resolved.replace("!= empty", "").strip()
        return bool(value)

    # "X == Y"
    if "==" in resolved:
        parts = resolved.split("==", 1)
        return parts[0].strip() == parts[1].strip()

    # "X != Y"
    if "!=" in resolved:
        parts = resolved.split("!=", 1)
        return parts[0].strip() != parts[1].strip()

    # Truthy check
    return bool(resolved.strip())


class WorkflowEngine:
    """Engine for loading, managing, and executing workflows."""

    def __init__(
        self,
        storage_path: Path | None = None,
    ) -> None:
        self._storage_path = storage_path or (get_kuro_home() / "workflows.json")
        self._workflows: dict[str, WorkflowDefinition] = {}
        self._runs: list[WorkflowRun] = []
        self._tool_executor: ToolExecutor | None = None
        self._agent_executor: AgentExecutor | None = None
        self._notification_callback: Callable | None = None
        self._run_counter = 0

    # Type for notification callback: (adapter, user_id, message) -> bool
    NotifyCallback = Callable[[str, str, str], Awaitable[bool]]

    def set_executors(
        self,
        tool_executor: ToolExecutor | None = None,
        agent_executor: AgentExecutor | None = None,
    ) -> None:
        """Set the tool and agent execution callbacks."""
        self._tool_executor = tool_executor
        self._agent_executor = agent_executor

    def set_notification_callback(self, callback: Callable) -> None:
        """Set the notification callback for workflow completion.

        Args:
            callback: Async function (adapter_name, user_id, message) -> bool
        """
        self._notification_callback = callback

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def register(self, definition: WorkflowDefinition) -> None:
        """Register a workflow definition."""
        self._workflows[definition.name] = definition
        logger.info("Workflow registered: %s", definition.name)

    def unregister(self, name: str) -> bool:
        """Remove a workflow definition."""
        if name in self._workflows:
            del self._workflows[name]
            return True
        return False

    def get(self, name: str) -> WorkflowDefinition | None:
        """Get a workflow by name."""
        return self._workflows.get(name)

    def list_workflows(self) -> list[WorkflowDefinition]:
        """List all registered workflows."""
        return list(self._workflows.values())

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def run_workflow(
        self,
        name: str,
        initial_vars: dict[str, str] | None = None,
        notify_adapter: str | None = None,
        notify_user_id: str | None = None,
    ) -> WorkflowRun:
        """Execute a workflow by name.

        Args:
            name: Workflow name
            initial_vars: Optional initial variables

        Returns:
            WorkflowRun with results and status
        """
        workflow = self._workflows.get(name)
        if not workflow:
            return WorkflowRun(
                workflow_name=name,
                status="failed",
                error=f"Workflow '{name}' not found",
            )

        self._run_counter += 1
        run = WorkflowRun(
            workflow_name=name,
            run_id=f"run-{self._run_counter}",
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            variables=dict(initial_vars or {}),
        )

        try:
            for i, step in enumerate(workflow.steps):
                step_name = step.name or f"step-{i + 1}"

                # Check condition
                if step.condition and not _check_condition(step.condition, run.variables):
                    run.step_results.append({
                        "step": step_name,
                        "status": "skipped",
                        "reason": f"Condition not met: {step.condition}",
                    })
                    continue

                # Execute step
                result = await self._execute_step(step, run.variables)

                # Store result
                run.step_results.append({
                    "step": step_name,
                    "status": "ok" if result is not None else "error",
                    "output_var": step.output,
                    "result_preview": (result or "")[:200],
                })

                # Save output to variables
                if step.output and result is not None:
                    run.variables[step.output] = result

            run.status = "completed"

        except Exception as e:
            run.status = "failed"
            run.error = str(e)
            logger.error("Workflow '%s' failed at step: %s", name, e)

        run.completed_at = datetime.now(timezone.utc).isoformat()
        self._runs.append(run)

        # Keep only last 50 runs
        if len(self._runs) > 50:
            self._runs = self._runs[-50:]

        # Send notification if configured
        if notify_adapter and notify_user_id and self._notification_callback:
            try:
                if run.status == "completed":
                    msg = f"\u2705 Workflow '{name}' completed ({len(run.step_results)} steps)"
                    # Add final output preview
                    if run.variables:
                        last_var = list(run.variables.values())[-1]
                        msg += f"\n\nResult:\n{last_var[:1000]}"
                else:
                    msg = f"\u274c Workflow '{name}' failed: {run.error or 'unknown error'}"
                await self._notification_callback(notify_adapter, notify_user_id, msg)
            except Exception as e:
                logger.error("workflow_notify_failed", workflow=name, error=str(e))

        return run

    async def _execute_step(
        self,
        step: WorkflowStep,
        variables: dict[str, str],
    ) -> str | None:
        """Execute a single workflow step."""

        if step.tool:
            # Tool execution
            if not self._tool_executor:
                raise RuntimeError("No tool executor configured")

            resolved_params = _resolve_params(step.params, variables)
            result = await self._tool_executor(step.tool, resolved_params)
            return result

        elif step.agent and step.prompt:
            # Agent delegation
            if not self._agent_executor:
                raise RuntimeError("No agent executor configured")

            resolved_prompt = _resolve_template(step.prompt, variables)
            result = await self._agent_executor(step.agent, resolved_prompt)
            return result

        elif step.prompt:
            # Pure template resolution (no execution)
            return _resolve_template(step.prompt, variables)

        else:
            logger.warning("Step has no tool or agent defined")
            return None

    # ------------------------------------------------------------------
    # Run History
    # ------------------------------------------------------------------

    def get_recent_runs(self, limit: int = 10) -> list[WorkflowRun]:
        """Get recent workflow execution results."""
        return list(reversed(self._runs[-limit:]))

    def get_run(self, run_id: str) -> WorkflowRun | None:
        """Get a specific run by ID."""
        for run in self._runs:
            if run.run_id == run_id:
                return run
        return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def save(self) -> None:
        """Save workflow definitions to disk."""
        data = {}
        for name, wf in self._workflows.items():
            data[name] = {
                "name": wf.name,
                "description": wf.description,
                "enabled": wf.enabled,
                "created_at": wf.created_at,
                "trigger": {
                    "schedule": wf.trigger.schedule,
                    "on_event": wf.trigger.on_event,
                },
                "steps": [
                    {
                        "tool": s.tool,
                        "agent": s.agent,
                        "prompt": s.prompt,
                        "params": s.params,
                        "output": s.output,
                        "condition": s.condition,
                        "name": s.name,
                    }
                    for s in wf.steps
                ],
            }

        async with aiofiles.open(self._storage_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))

    async def load(self) -> None:
        """Load workflow definitions from disk."""
        if not self._storage_path.exists():
            return

        try:
            async with aiofiles.open(self._storage_path, "r", encoding="utf-8") as f:
                content = await f.read()

            data = json.loads(content)
            for name, wf_data in data.items():
                trigger = WorkflowTrigger(
                    schedule=wf_data.get("trigger", {}).get("schedule"),
                    on_event=wf_data.get("trigger", {}).get("on_event"),
                )
                steps = [
                    WorkflowStep(
                        tool=s.get("tool"),
                        agent=s.get("agent"),
                        prompt=s.get("prompt"),
                        params=s.get("params", {}),
                        output=s.get("output"),
                        condition=s.get("condition"),
                        name=s.get("name"),
                    )
                    for s in wf_data.get("steps", [])
                ]
                self._workflows[name] = WorkflowDefinition(
                    name=wf_data.get("name", name),
                    description=wf_data.get("description", ""),
                    trigger=trigger,
                    steps=steps,
                    enabled=wf_data.get("enabled", True),
                    created_at=wf_data.get("created_at", ""),
                )

            logger.info("Loaded %d workflows", len(self._workflows))

        except Exception as e:
            logger.error("Failed to load workflows: %s", e)


def parse_workflow_yaml(yaml_text: str) -> WorkflowDefinition | None:
    """Parse a workflow from YAML-like text (simple parser without PyYAML).

    This supports a simplified YAML format:
    ```
    name: workflow-name
    description: What it does
    trigger:
      schedule: daily 09:00
    steps:
      - tool: shell_execute
        params:
          command: git log --since='1 day ago'
        output: result
    ```
    """
    try:
        # Try to use yaml if available
        import yaml  # noqa: F811
        data = yaml.safe_load(yaml_text)
    except ImportError:
        # Fallback: try json
        try:
            data = json.loads(yaml_text)
        except json.JSONDecodeError:
            logger.warning("Cannot parse workflow: need PyYAML or JSON format")
            return None
    except Exception as e:
        logger.warning("Failed to parse workflow YAML: %s", e)
        return None

    if not isinstance(data, dict) or "name" not in data:
        return None

    trigger = WorkflowTrigger()
    if "trigger" in data and isinstance(data["trigger"], dict):
        trigger.schedule = data["trigger"].get("schedule")
        trigger.on_event = data["trigger"].get("on_event")

    steps = []
    for s in data.get("steps", []):
        if not isinstance(s, dict):
            continue
        steps.append(WorkflowStep(
            tool=s.get("tool"),
            agent=s.get("agent"),
            prompt=s.get("prompt"),
            params=s.get("params", {}),
            output=s.get("output"),
            condition=s.get("condition"),
            name=s.get("name"),
        ))

    return WorkflowDefinition(
        name=data["name"],
        description=data.get("description", ""),
        trigger=trigger,
        steps=steps,
    )
