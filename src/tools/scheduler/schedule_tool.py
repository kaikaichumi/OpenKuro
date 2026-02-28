"""Scheduler management tools."""

from __future__ import annotations

from typing import Any

from src.core.scheduler import ScheduleType, TaskScheduler
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class ScheduleAddTool(BaseTool):
    """Add a new scheduled task."""

    _auto_discover = False  # Requires scheduler dependency injection
    name = "schedule_add"
    description = (
        "Schedule a tool or sub-agent to run automatically at specific times. "
        "Supports daily, weekly, hourly, interval, and one-time schedules. "
        "Use task_type='agent' with tool_name set to the agent name "
        "(e.g. 'researcher') and agent_task describing what the agent should do. "
        "Use task_type='tool' (default) for regular tool execution."
    )
    risk_level = RiskLevel.LOW

    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Unique task identifier (e.g., 'stock-monitor-daily')"
            },
            "name": {
                "type": "string",
                "description": "Human-readable task name"
            },
            "task_type": {
                "type": "string",
                "enum": ["tool", "agent"],
                "description": "Type of task: 'tool' to run a tool, 'agent' to delegate to a sub-agent",
                "default": "tool"
            },
            "tool_name": {
                "type": "string",
                "description": "Name of the tool or agent to execute"
            },
            "parameters": {
                "type": "object",
                "description": "Parameters to pass to the tool (ignored for agent tasks)",
                "default": {}
            },
            "agent_task": {
                "type": "string",
                "description": "Task description for the agent (required when task_type='agent')"
            },
            "schedule_type": {
                "type": "string",
                "enum": ["daily", "weekly", "hourly", "interval", "once"],
                "description": "Type of schedule"
            },
            "schedule_time": {
                "type": "string",
                "description": "Time in HH:MM format (for daily/weekly). Example: '09:00'"
            },
            "schedule_days": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Days of week (0=Monday, 6=Sunday) for weekly schedules"
            },
            "interval_minutes": {
                "type": "integer",
                "description": "Interval in minutes (for interval schedules)"
            },
            "notify": {
                "type": "boolean",
                "description": "Send notification when task completes (default: true). "
                               "Auto-detected from the current session's adapter/channel.",
                "default": True
            },
            "notify_adapter": {
                "type": "string",
                "enum": ["discord", "telegram"],
                "description": "Override notification adapter (e.g., 'discord', 'telegram'). "
                               "If omitted, auto-detected from the current session."
            },
            "notify_channel": {
                "type": "string",
                "description": "Override notification channel/chat ID. "
                               "If omitted, auto-detected from the current session."
            }
        },
        "required": ["task_id", "name", "tool_name", "schedule_type"]
    }

    def __init__(self, scheduler: TaskScheduler):
        super().__init__()
        self.scheduler = scheduler

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Add a scheduled task."""
        try:
            task_type = params.get("task_type", "tool")

            # Validate agent tasks
            if task_type == "agent" and not params.get("agent_task"):
                return ToolResult.fail(
                    "agent_task is required when task_type='agent'"
                )

            # Resolve notification target:
            # Priority: explicit params > auto-detect from session > scheduler default
            notify_adapter = params.get("notify_adapter")
            notify_user_id = params.get("notify_channel")

            if not (notify_adapter and notify_user_id):
                # Auto-detect from current session
                if params.get("notify", True) and context.session:
                    adapter = getattr(context.session, "adapter", "cli")
                    user_id = getattr(context.session, "user_id", "local")
                    if adapter in ("discord", "telegram"):
                        notify_adapter = notify_adapter or adapter
                        if not notify_user_id:
                            # Discord session keys are "channel_id:user_id" — extract channel_id
                            if adapter == "discord" and ":" in user_id:
                                notify_user_id = user_id.split(":")[0]
                            else:
                                notify_user_id = user_id

            task = self.scheduler.add_task(
                task_id=params["task_id"],
                name=params["name"],
                tool_name=params["tool_name"],
                parameters=params.get("parameters", {}),
                schedule_type=ScheduleType(params["schedule_type"]),
                schedule_time=params.get("schedule_time"),
                schedule_days=params.get("schedule_days"),
                interval_minutes=params.get("interval_minutes"),
                notify_adapter=notify_adapter,
                notify_user_id=notify_user_id,
                task_type=task_type,
                agent_task=params.get("agent_task"),
            )

            schedule_info = self._format_schedule(task)
            next_run = task.next_run.strftime("%Y-%m-%d %H:%M") if task.next_run else "N/A"
            notify_info = f"Notify: {notify_adapter}" if notify_adapter else "Notify: off"

            type_label = "Agent" if task_type == "agent" else "Tool"
            target = task.tool_name
            if task_type == "agent":
                target += f" — {task.agent_task}"

            return ToolResult.ok(
                f"Scheduled task '{task.name}' (ID: {task.id})\n\n"
                f"Type: {type_label}\n"
                f"Target: {target}\n"
                f"Schedule: {schedule_info}\n"
                f"Next run: {next_run}\n"
                f"{notify_info}"
            )

        except ValueError as e:
            return ToolResult.fail(str(e))
        except Exception as e:
            return ToolResult.fail(f"Failed to add task: {str(e)}")

    def _format_schedule(self, task) -> str:
        """Format schedule information for display."""
        if task.schedule_type == ScheduleType.DAILY:
            return f"Daily at {task.schedule_time}"
        elif task.schedule_type == ScheduleType.WEEKLY:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            day_names = [days[d] for d in task.schedule_days]
            return f"Weekly on {', '.join(day_names)} at {task.schedule_time}"
        elif task.schedule_type == ScheduleType.HOURLY:
            return "Every hour"
        elif task.schedule_type == ScheduleType.INTERVAL:
            return f"Every {task.interval_minutes} minutes"
        elif task.schedule_type == ScheduleType.ONCE:
            return f"Once at {task.schedule_time}"
        return "Unknown"


class ScheduleListTool(BaseTool):
    """List all scheduled tasks."""

    _auto_discover = False
    name = "schedule_list"
    description = "List all scheduled tasks with their status and next run times."
    risk_level = RiskLevel.LOW

    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }

    def __init__(self, scheduler: TaskScheduler):
        super().__init__()
        self.scheduler = scheduler

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """List all scheduled tasks."""
        tasks = self.scheduler.list_tasks()

        if not tasks:
            return ToolResult.ok("No scheduled tasks.")

        lines = ["📅 Scheduled Tasks\n"]

        for i, task in enumerate(tasks, 1):
            status = "✅ Enabled" if task.enabled else "⏸️ Disabled"
            next_run = task.next_run.strftime("%Y-%m-%d %H:%M") if task.next_run else "N/A"
            last_run = task.last_run.strftime("%Y-%m-%d %H:%M") if task.last_run else "Never"

            schedule_info = self._format_schedule(task)

            type_label = "Agent" if task.task_type == "agent" else "Tool"
            target_info = task.tool_name
            if task.task_type == "agent" and task.agent_task:
                target_info += f" — {task.agent_task[:60]}"

            lines.append(f"{i}. {task.name} ({status})")
            lines.append(f"   ID: {task.id}")
            lines.append(f"   {type_label}: {target_info}")
            lines.append(f"   Schedule: {schedule_info}")
            lines.append(f"   Next run: {next_run}")
            lines.append(f"   Last run: {last_run}")
            lines.append(f"   Run count: {task.run_count}")
            lines.append("")

        return ToolResult.ok("\n".join(lines))

    def _format_schedule(self, task) -> str:
        """Format schedule information."""
        if task.schedule_type == ScheduleType.DAILY:
            return f"Daily at {task.schedule_time}"
        elif task.schedule_type == ScheduleType.WEEKLY:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            day_names = [days[d] for d in task.schedule_days]
            return f"Weekly on {', '.join(day_names)} at {task.schedule_time}"
        elif task.schedule_type == ScheduleType.HOURLY:
            return "Every hour"
        elif task.schedule_type == ScheduleType.INTERVAL:
            return f"Every {task.interval_minutes} minutes"
        elif task.schedule_type == ScheduleType.ONCE:
            return f"Once at {task.schedule_time}"
        return "Unknown"


class ScheduleRemoveTool(BaseTool):
    """Remove a scheduled task."""

    _auto_discover = False
    name = "schedule_remove"
    description = "Remove a scheduled task by its ID."
    risk_level = RiskLevel.LOW

    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID to remove"
            }
        },
        "required": ["task_id"]
    }

    def __init__(self, scheduler: TaskScheduler):
        super().__init__()
        self.scheduler = scheduler

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Remove a scheduled task."""
        task_id = params["task_id"]

        if self.scheduler.remove_task(task_id):
            return ToolResult.ok(f"✅ Removed scheduled task '{task_id}'")
        else:
            return ToolResult.fail(f"Task '{task_id}' not found")


class ScheduleEnableTool(BaseTool):
    """Enable a scheduled task."""

    _auto_discover = False
    name = "schedule_enable"
    description = "Enable a disabled scheduled task."
    risk_level = RiskLevel.LOW

    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID to enable"
            }
        },
        "required": ["task_id"]
    }

    def __init__(self, scheduler: TaskScheduler):
        super().__init__()
        self.scheduler = scheduler

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Enable a scheduled task."""
        task_id = params["task_id"]

        if self.scheduler.enable_task(task_id):
            task = self.scheduler.get_task(task_id)
            next_run = task.next_run.strftime("%Y-%m-%d %H:%M") if task.next_run else "N/A"
            return ToolResult.ok(f"✅ Enabled task '{task_id}'\nNext run: {next_run}")
        else:
            return ToolResult.fail(f"Task '{task_id}' not found")


class ScheduleDisableTool(BaseTool):
    """Disable a scheduled task."""

    _auto_discover = False
    name = "schedule_disable"
    description = "Disable a scheduled task without removing it."
    risk_level = RiskLevel.LOW

    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID to disable"
            }
        },
        "required": ["task_id"]
    }

    def __init__(self, scheduler: TaskScheduler):
        super().__init__()
        self.scheduler = scheduler

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Disable a scheduled task."""
        task_id = params["task_id"]

        if self.scheduler.disable_task(task_id):
            return ToolResult.ok(f"⏸️ Disabled task '{task_id}'")
        else:
            return ToolResult.fail(f"Task '{task_id}' not found")


class ScheduleUpdateTool(BaseTool):
    """Update properties of an existing scheduled task."""

    _auto_discover = False
    name = "schedule_update"
    description = (
        "Update an existing scheduled task's properties: notification target, "
        "schedule time, agent_task description, or parameters."
    )
    risk_level = RiskLevel.LOW

    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID to update"
            },
            "notify_adapter": {
                "type": "string",
                "enum": ["discord", "telegram"],
                "description": "Set notification adapter (e.g., 'discord', 'telegram')"
            },
            "notify_channel": {
                "type": "string",
                "description": "Set notification channel/chat ID"
            },
            "schedule_time": {
                "type": "string",
                "description": "New schedule time in HH:MM format"
            },
            "agent_task": {
                "type": "string",
                "description": "New agent task description"
            },
            "parameters": {
                "type": "object",
                "description": "New tool parameters"
            },
        },
        "required": ["task_id"]
    }

    def __init__(self, scheduler: TaskScheduler):
        super().__init__()
        self.scheduler = scheduler

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Update a scheduled task."""
        task_id = params["task_id"]
        task = self.scheduler.get_task(task_id)
        if not task:
            return ToolResult.fail(f"Task '{task_id}' not found")

        changes = []

        if "notify_adapter" in params:
            task.notify_adapter = params["notify_adapter"]
            changes.append(f"notify_adapter → {params['notify_adapter']}")

        if "notify_channel" in params:
            task.notify_user_id = params["notify_channel"]
            changes.append(f"notify_channel → {params['notify_channel']}")

        # Auto-detect from session if adapter specified but no channel
        if "notify_adapter" in params and "notify_channel" not in params and not task.notify_user_id:
            if context.session:
                adapter = getattr(context.session, "adapter", "cli")
                user_id = getattr(context.session, "user_id", "local")
                if adapter == params["notify_adapter"]:
                    if adapter == "discord" and ":" in user_id:
                        task.notify_user_id = user_id.split(":")[0]
                    else:
                        task.notify_user_id = user_id
                    changes.append(f"notify_channel → {task.notify_user_id} (auto-detected)")

        if "schedule_time" in params:
            task.schedule_time = params["schedule_time"]
            task.next_run = self.scheduler._calculate_next_run(task)
            changes.append(f"schedule_time → {params['schedule_time']}")

        if "agent_task" in params:
            task.agent_task = params["agent_task"]
            changes.append(f"agent_task updated")

        if "parameters" in params:
            task.parameters = params["parameters"]
            changes.append(f"parameters updated")

        if not changes:
            return ToolResult.fail("No fields to update. Provide at least one field to change.")

        self.scheduler._save_tasks()

        notify_info = f"{task.notify_adapter or 'none'}:{task.notify_user_id or 'none'}"
        return ToolResult.ok(
            f"✅ Updated task '{task.name}' (ID: {task_id})\n\n"
            f"Changes:\n" + "\n".join(f"  • {c}" for c in changes) + "\n\n"
            f"Notify: {notify_info}"
        )
