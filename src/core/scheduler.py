"""Task scheduler: cron-like scheduling for tools and sub-agents.

Allows scheduling recurring tasks — both tool executions and
sub-agent delegations — at specific times or intervals.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import structlog

from src.config import get_kuro_home

logger = structlog.get_logger()

# Type alias for notification callback:
# (adapter_name, user_id, message) -> success
NotificationCallback = Callable[[str, str, str], Any]


class ScheduleType(str, Enum):
    """Types of schedule patterns."""

    ONCE = "once"  # Run once at a specific time
    DAILY = "daily"  # Run every day at a specific time
    WEEKLY = "weekly"  # Run on specific days of the week
    HOURLY = "hourly"  # Run every N hours
    INTERVAL = "interval"  # Run every N minutes


@dataclass
class ScheduledTask:
    """A scheduled task definition."""

    id: str  # Unique task ID
    name: str  # Human-readable name
    tool_name: str  # Tool name (task_type="tool") or agent name (task_type="agent")
    parameters: dict[str, Any] = field(default_factory=dict)  # Tool parameters
    schedule_type: ScheduleType = ScheduleType.DAILY
    schedule_time: str | None = None  # "09:00" for daily/weekly
    schedule_days: list[int] | None = None  # [0, 1, 2] for Monday, Tuesday, Wednesday
    interval_minutes: int | None = None  # For interval type
    enabled: bool = True
    last_run: datetime | None = None
    next_run: datetime | None = None
    run_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now())
    notify_adapter: str | None = None   # "discord" / "telegram" / None
    notify_user_id: str | None = None   # Channel/chat ID for notifications
    task_type: str = "tool"             # "tool" | "agent"
    agent_task: str | None = None       # Task description for agent (when task_type="agent")

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "tool_name": self.tool_name,
            "parameters": self.parameters,
            "schedule_type": self.schedule_type.value,
            "schedule_time": self.schedule_time,
            "schedule_days": self.schedule_days,
            "interval_minutes": self.interval_minutes,
            "enabled": self.enabled,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "run_count": self.run_count,
            "created_at": self.created_at.isoformat(),
            "notify_adapter": self.notify_adapter,
            "notify_user_id": self.notify_user_id,
            "task_type": self.task_type,
        }
        if self.agent_task is not None:
            d["agent_task"] = self.agent_task
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledTask:
        """Create from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            tool_name=data["tool_name"],
            parameters=data.get("parameters", {}),
            schedule_type=ScheduleType(data["schedule_type"]),
            schedule_time=data.get("schedule_time"),
            schedule_days=data.get("schedule_days"),
            interval_minutes=data.get("interval_minutes"),
            enabled=data.get("enabled", True),
            last_run=datetime.fromisoformat(data["last_run"]) if data.get("last_run") else None,
            next_run=datetime.fromisoformat(data["next_run"]) if data.get("next_run") else None,
            run_count=data.get("run_count", 0),
            created_at=datetime.fromisoformat(data["created_at"]),
            notify_adapter=data.get("notify_adapter"),
            notify_user_id=data.get("notify_user_id"),
            task_type=data.get("task_type", "tool"),
            agent_task=data.get("agent_task"),
        )


class TaskScheduler:
    """Manages scheduled task execution."""

    def __init__(self, storage_path: Path | None = None):
        """Initialize the scheduler.

        Args:
            storage_path: Path to store scheduled tasks. Defaults to ~/.kuro/scheduler.json
        """
        self.storage_path = storage_path or (get_kuro_home() / "scheduler.json")
        self.tasks: dict[str, ScheduledTask] = {}
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._executor: Callable | None = None
        self._agent_executor: Callable | None = None
        self._agent_checker: Callable | None = None  # Check if name is a registered agent
        self._notification_callback: NotificationCallback | None = None
        self._default_notify_adapter: str | None = None
        self._default_notify_target: str | None = None

        # Load existing tasks
        self._load_tasks()

    def set_executor(self, executor: Callable) -> None:
        """Set the tool executor function.

        Args:
            executor: Async function that executes a tool.
                     Signature: async def executor(tool_name: str, params: dict) -> str
        """
        self._executor = executor

    def set_agent_executor(
        self,
        agent_executor: Callable,
        agent_checker: Callable | None = None,
    ) -> None:
        """Set the agent executor function.

        Args:
            agent_executor: Async function that runs a sub-agent.
                           Signature: async def(agent_name: str, task: str) -> str
            agent_checker: Optional sync function to check if a name is a registered agent.
                          Signature: def(name: str) -> bool
        """
        self._agent_executor = agent_executor
        self._agent_checker = agent_checker

    def set_notification_callback(self, callback: NotificationCallback) -> None:
        """Set the notification callback for task results.

        Args:
            callback: Async function with signature:
                     (adapter_name: str, user_id: str, message: str) -> bool
        """
        self._notification_callback = callback

    def set_default_notification(self, adapter: str, target: str) -> None:
        """Set default notification target for tasks without explicit notify settings.

        Args:
            adapter: Adapter name (e.g., "discord", "telegram").
            target: Channel/chat ID for notifications.
        """
        self._default_notify_adapter = adapter
        self._default_notify_target = target
        logger.info(
            "scheduler_default_notify_set",
            adapter=adapter,
            target=target,
        )

    def add_task(
        self,
        task_id: str,
        name: str,
        tool_name: str,
        parameters: dict[str, Any] | None = None,
        schedule_type: ScheduleType = ScheduleType.DAILY,
        schedule_time: str | None = None,
        schedule_days: list[int] | None = None,
        interval_minutes: int | None = None,
        notify_adapter: str | None = None,
        notify_user_id: str | None = None,
        task_type: str = "tool",
        agent_task: str | None = None,
    ) -> ScheduledTask:
        """Add a new scheduled task.

        Args:
            task_id: Unique task identifier
            name: Human-readable task name
            tool_name: Name of the tool to execute, or agent name when task_type="agent"
            parameters: Parameters to pass to the tool (ignored for agent tasks)
            schedule_type: Type of schedule (daily, weekly, interval, etc.)
            schedule_time: Time string "HH:MM" for daily/weekly tasks
            schedule_days: List of weekday numbers (0=Monday) for weekly tasks
            interval_minutes: Interval in minutes for interval tasks
            task_type: "tool" to execute a tool, "agent" to delegate to a sub-agent
            agent_task: Task description for the agent (required when task_type="agent")

        Returns:
            The created ScheduledTask

        Raises:
            ValueError: If a task with the same ID already exists
        """
        if task_id in self.tasks:
            raise ValueError(f"Task with ID '{task_id}' already exists")

        task = ScheduledTask(
            id=task_id,
            name=name,
            tool_name=tool_name,
            parameters=parameters or {},
            schedule_type=schedule_type,
            schedule_time=schedule_time,
            schedule_days=schedule_days,
            interval_minutes=interval_minutes,
            notify_adapter=notify_adapter,
            notify_user_id=notify_user_id,
            task_type=task_type,
            agent_task=agent_task,
        )

        # Calculate next run time
        task.next_run = self._calculate_next_run(task)

        self.tasks[task_id] = task
        self._save_tasks()

        logger.info(
            "task_scheduled",
            task_id=task_id,
            tool=tool_name,
            schedule=schedule_type.value,
            next_run=task.next_run.isoformat() if task.next_run else None,
        )

        return task

    def remove_task(self, task_id: str) -> bool:
        """Remove a scheduled task.

        Args:
            task_id: Task identifier

        Returns:
            True if task was removed, False if not found
        """
        if task_id in self.tasks:
            del self.tasks[task_id]
            self._save_tasks()
            logger.info("task_removed", task_id=task_id)
            return True
        return False

    def enable_task(self, task_id: str) -> bool:
        """Enable a task."""
        if task_id in self.tasks:
            self.tasks[task_id].enabled = True
            self.tasks[task_id].next_run = self._calculate_next_run(self.tasks[task_id])
            self._save_tasks()
            return True
        return False

    def disable_task(self, task_id: str) -> bool:
        """Disable a task."""
        if task_id in self.tasks:
            self.tasks[task_id].enabled = False
            self._save_tasks()
            return True
        return False

    def get_task(self, task_id: str) -> ScheduledTask | None:
        """Get a task by ID."""
        return self.tasks.get(task_id)

    def list_tasks(self, include_expired_once: bool = False) -> list[ScheduledTask]:
        """List scheduled tasks.

        By default, excludes completed/expired one-time tasks.
        Only returns:
        - Future one-time tasks (next_run is set)
        - All recurring tasks (daily, weekly, hourly, interval)

        Args:
            include_expired_once: If True, also include expired once tasks.
        """
        if include_expired_once:
            return list(self.tasks.values())

        result = []
        for task in self.tasks.values():
            if task.schedule_type == ScheduleType.ONCE:
                # Only include if it hasn't run yet (next_run is set)
                if task.next_run is not None:
                    result.append(task)
            else:
                # Recurring tasks are always shown
                result.append(task)
        return result

    def _calculate_next_run(self, task: ScheduledTask) -> datetime | None:
        """Calculate the next run time for a task."""
        now = datetime.now()

        if task.schedule_type == ScheduleType.ONCE:
            # One-time task
            if task.schedule_time:
                # Parse time
                hour, minute = map(int, task.schedule_time.split(":"))
                next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_run <= now:
                    return None  # Already passed
                return next_run
            return None

        elif task.schedule_type == ScheduleType.DAILY:
            # Daily at specific time
            if not task.schedule_time:
                return None
            hour, minute = map(int, task.schedule_time.split(":"))
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                # Already passed today, schedule for tomorrow
                next_run += timedelta(days=1)
            return next_run

        elif task.schedule_type == ScheduleType.WEEKLY:
            # Weekly on specific days
            if not task.schedule_time or not task.schedule_days:
                return None
            hour, minute = map(int, task.schedule_time.split(":"))

            # Find next matching weekday
            for day_offset in range(8):  # Check next 7 days + today
                check_date = now + timedelta(days=day_offset)
                if check_date.weekday() in task.schedule_days:
                    next_run = check_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if next_run > now:
                        return next_run
            return None

        elif task.schedule_type == ScheduleType.INTERVAL:
            # Run every N minutes
            if not task.interval_minutes:
                return None
            if task.last_run:
                return task.last_run + timedelta(minutes=task.interval_minutes)
            else:
                # First run
                return now + timedelta(minutes=task.interval_minutes)

        elif task.schedule_type == ScheduleType.HOURLY:
            # Run every hour
            next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            return next_run

        return None

    async def start(self) -> None:
        """Start the scheduler loop."""
        if self._running:
            logger.warning("scheduler_already_running")
            return

        if not self._executor:
            logger.error("scheduler_no_executor")
            raise RuntimeError("Executor function not set. Call set_executor() first.")

        self._running = True
        self._loop_task = asyncio.create_task(self._scheduler_loop())
        logger.info("scheduler_started", task_count=len(self.tasks))

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        logger.info("scheduler_stopped")

    async def _scheduler_loop(self) -> None:
        """Main scheduler loop."""
        while self._running:
            try:
                now = datetime.now()

                # Check each task
                for task in list(self.tasks.values()):
                    if not task.enabled:
                        continue

                    if task.next_run and now >= task.next_run:
                        # Time to run this task
                        asyncio.create_task(self._execute_task(task))

                # Sleep for 30 seconds before next check
                await asyncio.sleep(30)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("scheduler_loop_error", error=str(e))
                await asyncio.sleep(60)  # Wait a bit before retrying

    async def _execute_task(self, task: ScheduledTask) -> None:
        """Execute a scheduled task (tool, agent, or internal)."""
        logger.info(
            "task_executing",
            task_id=task.id,
            tool=task.tool_name,
            task_type=task.task_type,
        )

        try:
            # Internal tasks with direct executor (lifecycle, learning, etc.)
            if hasattr(task, "_direct_executor") and task._direct_executor:
                result = await task._direct_executor(task.tool_name, task.parameters)
                task.last_run = datetime.now()
                task.run_count += 1
                task.next_run = self._calculate_next_run(task)
                self._save_tasks()
                logger.info("internal_task_executed", task_id=task.id, result=str(result)[:200])
                return

            if task.task_type == "agent":
                # --- Agent task ---
                if self._agent_executor is None:
                    raise RuntimeError(
                        "Agent executor not configured. Cannot run agent tasks."
                    )
                result = await self._agent_executor(
                    task.tool_name, task.agent_task or task.name
                )
            else:
                # --- Tool task (with smart detection fallback) ---
                try:
                    result = await self._executor(task.tool_name, task.parameters)
                except RuntimeError as tool_err:
                    # If tool not found, check if it's a registered agent
                    if "Unknown tool" in str(tool_err) and self._agent_checker:
                        if self._agent_checker(task.tool_name):
                            logger.info(
                                "task_auto_corrected_to_agent",
                                task_id=task.id,
                                name=task.tool_name,
                            )
                            # Auto-correct task_type for future runs
                            task.task_type = "agent"
                            if not task.agent_task:
                                # Build agent_task from name + parameters
                                parts = [task.name]
                                if task.parameters:
                                    for k, v in task.parameters.items():
                                        parts.append(f"{k}: {v}")
                                task.agent_task = "\n".join(parts)
                            if self._agent_executor is None:
                                raise RuntimeError(
                                    "Agent executor not configured"
                                ) from tool_err
                            result = await self._agent_executor(
                                task.tool_name, task.agent_task
                            )
                        else:
                            raise
                    else:
                        raise

            # Update task statistics
            task.last_run = datetime.now()
            task.run_count += 1
            task.next_run = self._calculate_next_run(task)

            # Save updated task
            self._save_tasks()

            logger.info(
                "task_executed",
                task_id=task.id,
                tool=task.tool_name,
                next_run=task.next_run.isoformat() if task.next_run else None,
            )

            # Log result (truncated)
            result_preview = result[:200] if isinstance(result, str) else str(result)[:200]
            logger.debug("task_result", task_id=task.id, result=result_preview)

            # Send notification on success
            await self._notify_result(task, result)

            # Remove completed one-time tasks
            if task.schedule_type == ScheduleType.ONCE:
                self._remove_completed_once_task(task)

        except Exception as e:
            logger.error("task_execution_failed", task_id=task.id, error=str(e))

            # Still update next run time even if failed
            task.last_run = datetime.now()
            task.next_run = self._calculate_next_run(task)
            self._save_tasks()

            # Send error notification
            await self._notify_error(task, e)

            # Even failed one-time tasks should be removed (they won't re-run)
            if task.schedule_type == ScheduleType.ONCE and task.next_run is None:
                self._remove_completed_once_task(task)

    def _remove_completed_once_task(self, task: ScheduledTask) -> None:
        """Remove a completed one-time task from the scheduler."""
        if task.id in self.tasks:
            del self.tasks[task.id]
            self._save_tasks()
            logger.info(
                "once_task_removed_after_execution",
                task_id=task.id,
                name=task.name,
            )

    def _resolve_notify_target(self, task: ScheduledTask) -> tuple[str | None, str | None]:
        """Resolve notification target: task-level first, then default fallback."""
        adapter = task.notify_adapter or self._default_notify_adapter
        target = task.notify_user_id or self._default_notify_target
        return adapter, target

    async def _notify_result(self, task: ScheduledTask, result: str) -> None:
        """Send a success notification for a completed task."""
        adapter, target = self._resolve_notify_target(task)
        if not (adapter and target and self._notification_callback):
            return

        try:
            result_preview = result[:1500] if isinstance(result, str) else str(result)[:1500]
            msg = (
                f"\U0001f4cb Scheduled task completed: **{task.name}**\n\n"
                f"Result:\n{result_preview}"
            )
            await self._notification_callback(adapter, target, msg)
        except Exception as e:
            logger.error("task_notify_failed", task_id=task.id, error=str(e))

    async def _notify_error(self, task: ScheduledTask, error: Exception) -> None:
        """Send an error notification for a failed task."""
        adapter, target = self._resolve_notify_target(task)
        if not (adapter and target and self._notification_callback):
            return

        try:
            msg = (
                f"\u274c Scheduled task failed: **{task.name}**\n\n"
                f"Error: {str(error)[:500]}"
            )
            await self._notification_callback(adapter, target, msg)
        except Exception as e:
            logger.error("task_error_notify_failed", task_id=task.id, error=str(e))

    def _load_tasks(self) -> None:
        """Load tasks from storage, cleaning up expired one-time tasks."""
        if not self.storage_path.exists():
            return

        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            loaded: dict[str, ScheduledTask] = {}
            removed_count = 0
            now = datetime.now()

            for task_data in data.get("tasks", []):
                task = ScheduledTask.from_dict(task_data)

                # Skip one-time tasks that have already executed or are past due
                if task.schedule_type == ScheduleType.ONCE:
                    if task.last_run is not None:
                        # Already executed → discard
                        removed_count += 1
                        continue
                    if task.next_run is not None and task.next_run < now:
                        # Past due and never ran → discard
                        removed_count += 1
                        continue

                loaded[task.id] = task

            self.tasks = loaded

            if removed_count > 0:
                # Persist the cleaned-up list
                self._save_tasks()
                logger.info(
                    "scheduler_expired_once_tasks_cleaned",
                    removed=removed_count,
                )

            logger.info("scheduler_tasks_loaded", count=len(self.tasks))
        except Exception as e:
            logger.error("scheduler_load_failed", error=str(e))

    def _save_tasks(self) -> None:
        """Save tasks to storage."""
        try:
            data = {
                "tasks": [task.to_dict() for task in self.tasks.values()],
                "updated_at": datetime.now().isoformat(),
            }

            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.storage_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("scheduler_save_failed", error=str(e))
