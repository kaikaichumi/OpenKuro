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
        "Schedule a tool to run automatically at specific times. "
        "Supports daily, weekly, hourly, and interval schedules."
    )
    risk_level = RiskLevel.MEDIUM

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
            "tool_name": {
                "type": "string",
                "description": "Name of the tool to execute"
            },
            "parameters": {
                "type": "object",
                "description": "Parameters to pass to the tool (optional)",
                "default": {}
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
                               "Notifications are sent to the adapter/channel where the task was created.",
                "default": True
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
            # Capture notification target from current session
            notify_adapter = None
            notify_user_id = None
            if params.get("notify", True) and context.session:
                adapter = getattr(context.session, "adapter", "cli")
                user_id = getattr(context.session, "user_id", "local")
                # Only enable notifications for platform adapters (not CLI)
                if adapter in ("discord", "telegram"):
                    notify_adapter = adapter
                    # Discord session keys are "channel_id:user_id" — extract channel_id
                    # since send_notification sends to the channel, not the user
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
            )

            schedule_info = self._format_schedule(task)
            next_run = task.next_run.strftime("%Y-%m-%d %H:%M") if task.next_run else "N/A"
            notify_info = f"Notify: {notify_adapter}" if notify_adapter else "Notify: off"

            return ToolResult.ok(
                f"✅ Scheduled task '{task.name}' (ID: {task.id})\n\n"
                f"Tool: {task.tool_name}\n"
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

            lines.append(f"{i}. {task.name} ({status})")
            lines.append(f"   ID: {task.id}")
            lines.append(f"   Tool: {task.tool_name}")
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
    risk_level = RiskLevel.MEDIUM

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
