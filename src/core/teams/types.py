"""Data types for the Agent Teams system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass
class TeamRole:
    """A role within an Agent Team.

    Each role maps to a registered AgentDefinition and has a specific
    responsibility within the team workflow.
    """

    name: str  # Role name, e.g. "researcher", "analyst", "writer"
    agent_name: str  # Corresponding AgentDefinition name in AgentManager
    responsibility: str = ""  # Human-readable description of what this role does
    receives_from: list[str] = field(default_factory=list)  # Roles that send to this one
    sends_to: list[str] = field(default_factory=list)  # Roles this one sends to


@dataclass
class TeamDefinition:
    """Definition of an Agent Team.

    A team is a group of agents that collaborate on a task using
    shared workspace and inter-agent messaging.
    """

    name: str  # Unique team name, e.g. "research-team"
    description: str = ""
    roles: list[TeamRole] = field(default_factory=list)
    coordinator_model: str | None = None  # Model for the coordinator LLM
    max_rounds: int = 5  # Maximum coordination rounds
    timeout_seconds: int = 300  # Overall team execution timeout
    created_by: str = "user"  # "user" | "config" | "runtime"


@dataclass
class TeamMessage:
    """A message exchanged between team members via the MessageBus."""

    id: str = field(default_factory=lambda: str(uuid4()))
    from_role: str = ""
    to_role: str | None = None  # None = broadcast to all
    content: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    msg_type: str = "data"  # "data" | "request" | "status" | "complete"


@dataclass
class TeamResult:
    """Result of a team execution."""

    team_name: str
    task: str
    final_output: str
    role_outputs: dict[str, str] = field(default_factory=dict)
    messages_exchanged: int = 0
    rounds_used: int = 0
    duration_ms: int = 0
    success: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for logging and API responses."""
        return {
            "team_name": self.team_name,
            "task": self.task[:200],
            "final_output": self.final_output[:500],
            "role_outputs": {k: v[:200] for k, v in self.role_outputs.items()},
            "messages_exchanged": self.messages_exchanged,
            "rounds_used": self.rounds_used,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error": self.error,
        }
