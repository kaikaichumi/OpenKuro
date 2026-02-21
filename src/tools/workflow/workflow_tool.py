"""Workflow management tools for the LLM to create and run workflows."""

from __future__ import annotations

import json
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class WorkflowRunTool(BaseTool):
    """Run a registered workflow by name."""

    name = "workflow_run"
    description = (
        "Run a registered multi-step workflow. "
        "Workflows chain tools and agents together with variable passing. "
        "Use workflow_list to see available workflows."
    )
    parameters = {
        "type": "object",
        "properties": {
            "workflow_name": {
                "type": "string",
                "description": "Name of the workflow to execute",
            },
            "variables": {
                "type": "object",
                "description": "Optional initial variables to pass to the workflow",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["workflow_name"],
    }
    risk_level = RiskLevel.MEDIUM

    def __init__(self, workflow_engine: Any) -> None:
        super().__init__()
        self._engine = workflow_engine

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        name = params["workflow_name"]
        variables = params.get("variables", {})

        run = await self._engine.run_workflow(name, initial_vars=variables)

        if run.status == "failed":
            return ToolResult.error(f"Workflow failed: {run.error}")

        # Build summary
        lines = [f"Workflow '{name}' completed ({run.status})"]
        lines.append(f"Steps: {len(run.step_results)}")

        for step_result in run.step_results:
            status = step_result.get("status", "?")
            step_name = step_result.get("step", "?")
            icon = "✓" if status == "ok" else "○" if status == "skipped" else "✗"
            preview = step_result.get("result_preview", "")[:100]
            lines.append(f"  {icon} {step_name}: {preview}")

        # Include final variables
        if run.variables:
            lines.append("\nOutput variables:")
            for var, val in run.variables.items():
                lines.append(f"  {var}: {val[:200]}")

        return ToolResult.ok("\n".join(lines))


class WorkflowListTool(BaseTool):
    """List all registered workflows."""

    name = "workflow_list"
    description = (
        "List all registered multi-step workflows. "
        "Shows workflow names, descriptions, step counts, and trigger info."
    )
    parameters = {
        "type": "object",
        "properties": {},
    }
    risk_level = RiskLevel.LOW

    def __init__(self, workflow_engine: Any) -> None:
        super().__init__()
        self._engine = workflow_engine

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        workflows = self._engine.list_workflows()

        if not workflows:
            return ToolResult.ok("No workflows registered. Create one with workflow_create.")

        lines = [f"Registered workflows ({len(workflows)}):"]
        for wf in workflows:
            trigger_info = ""
            if wf.trigger.schedule:
                trigger_info = f" [trigger: {wf.trigger.schedule}]"
            enabled = "enabled" if wf.enabled else "disabled"
            lines.append(
                f"  - {wf.name}: {wf.description or 'No description'} "
                f"({len(wf.steps)} steps, {enabled}){trigger_info}"
            )

        # Show recent runs
        runs = self._engine.get_recent_runs(5)
        if runs:
            lines.append("\nRecent runs:")
            for run in runs:
                lines.append(
                    f"  [{run.run_id}] {run.workflow_name}: {run.status} "
                    f"({run.started_at[:19]})"
                )

        return ToolResult.ok("\n".join(lines))


class WorkflowCreateTool(BaseTool):
    """Create a new workflow from a JSON definition."""

    name = "workflow_create"
    description = (
        "Create a new multi-step workflow. Define steps that chain tools and agents. "
        "Each step can use {{variable}} templates from previous step outputs. "
        "Built-in variables: {{date}}, {{time}}, {{datetime}}."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Unique workflow name",
            },
            "description": {
                "type": "string",
                "description": "What this workflow does",
            },
            "steps": {
                "type": "array",
                "description": "Array of workflow steps",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "Tool name to execute (e.g., shell_execute, file_write)",
                        },
                        "agent": {
                            "type": "string",
                            "description": "Agent name to delegate to",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Prompt for agent, supports {{var}} templates",
                        },
                        "params": {
                            "type": "object",
                            "description": "Tool parameters, supports {{var}} templates",
                        },
                        "output": {
                            "type": "string",
                            "description": "Variable name to store step result",
                        },
                        "condition": {
                            "type": "string",
                            "description": "Condition to check before executing (e.g., '{{var}} != empty')",
                        },
                        "name": {
                            "type": "string",
                            "description": "Step display name",
                        },
                    },
                },
            },
            "trigger_schedule": {
                "type": "string",
                "description": "Optional schedule trigger (e.g., 'daily 09:00', 'weekly mon 10:00')",
            },
        },
        "required": ["name", "steps"],
    }
    risk_level = RiskLevel.MEDIUM

    def __init__(self, workflow_engine: Any) -> None:
        super().__init__()
        self._engine = workflow_engine

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        from src.core.workflow import WorkflowDefinition, WorkflowStep, WorkflowTrigger

        name = params["name"]
        description = params.get("description", "")
        steps_data = params.get("steps", [])

        if not steps_data:
            return ToolResult.error("Workflow must have at least one step")

        # Check if already exists
        if self._engine.get(name):
            return ToolResult.error(f"Workflow '{name}' already exists. Remove it first.")

        # Parse steps
        steps = []
        for s in steps_data:
            steps.append(WorkflowStep(
                tool=s.get("tool"),
                agent=s.get("agent"),
                prompt=s.get("prompt"),
                params=s.get("params", {}),
                output=s.get("output"),
                condition=s.get("condition"),
                name=s.get("name"),
            ))

        trigger = WorkflowTrigger(schedule=params.get("trigger_schedule"))

        definition = WorkflowDefinition(
            name=name,
            description=description,
            trigger=trigger,
            steps=steps,
        )

        self._engine.register(definition)

        # Save to disk
        try:
            await self._engine.save()
        except Exception:
            pass

        return ToolResult.ok(
            f"Workflow '{name}' created with {len(steps)} steps.\n"
            f"Run it with: workflow_run(workflow_name='{name}')"
        )


class WorkflowDeleteTool(BaseTool):
    """Delete a registered workflow."""

    name = "workflow_delete"
    description = "Delete a registered workflow by name."
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the workflow to delete",
            },
        },
        "required": ["name"],
    }
    risk_level = RiskLevel.MEDIUM

    def __init__(self, workflow_engine: Any) -> None:
        super().__init__()
        self._engine = workflow_engine

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        name = params["name"]

        if self._engine.unregister(name):
            try:
                await self._engine.save()
            except Exception:
                pass
            return ToolResult.ok(f"Workflow '{name}' deleted.")

        return ToolResult.error(f"Workflow '{name}' not found.")
