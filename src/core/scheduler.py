"""Task scheduler: cron-like scheduling for tools and skills.

Allows scheduling recurring tasks to be executed at specific times.
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
    tool_name: str  # Tool to execute
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

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
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
        }

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

        # Load existing tasks
        self._load_tasks()

    def set_executor(self, executor: Callable) -> None:
        """Set the task executor function.

        Args:
            executor: Async function that executes a tool.
                     Signature: async def executor(tool_name: str, params: dict) -> str
        """
        self._executor = executor

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
    ) -> ScheduledTask:
        """Add a new scheduled task.

        Args:
            task_id: Unique task identifier
            name: Human-readable task name
            tool_name: Name of the tool to execute
            parameters: Parameters to pass to the tool
            schedule_type: Type of schedule (daily, weekly, interval, etc.)
            schedule_time: Time string "HH:MM" for daily/weekly tasks
            schedule_days: List of weekday numbers (0=Monday) for weekly tasks
            interval_minutes: Interval in minutes for interval tasks

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

    def list_tasks(self) -> list[ScheduledTask]:
        """List all scheduled tasks."""
        return list(self.tasks.values())

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
        """Execute a scheduled task."""
        logger.info("task_executing", task_id=task.id, tool=task.tool_name)

        try:
            # Execute the tool
            result = await self._executor(task.tool_name, task.parameters)

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

        except Exception as e:
            logger.error("task_execution_failed", task_id=task.id, error=str(e))

            # Still update next run time even if failed
            task.last_run = datetime.now()
            task.next_run = self._calculate_next_run(task)
            self._save_tasks()

    def _load_tasks(self) -> None:
        """Load tasks from storage."""
        if not self.storage_path.exists():
            return

        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            self.tasks = {
                task_data["id"]: ScheduledTask.from_dict(task_data)
                for task_data in data.get("tasks", [])
            }
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
