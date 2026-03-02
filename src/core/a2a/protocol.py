"""A2A Protocol: data types for cross-instance agent communication.

Defines the wire format for agent capability advertisement, task requests,
and responses between Kuro instances.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass
class AgentCapability:
    """Advertised capability of a remote agent.

    Sent by remote instances via GET /a2a/capabilities so that
    other instances can discover what agents are available.
    """

    agent_name: str
    instance_id: str  # Unique Kuro instance identifier
    model: str
    tools: list[str] = field(default_factory=list)
    specialties: list[str] = field(default_factory=list)  # ["coding", "research"]
    endpoint: str = ""  # Base URL of the remote instance
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "instance_id": self.instance_id,
            "model": self.model,
            "tools": self.tools,
            "specialties": self.specialties,
            "endpoint": self.endpoint,
            "last_seen": self.last_seen.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentCapability:
        last_seen = data.get("last_seen")
        if isinstance(last_seen, str):
            last_seen = datetime.fromisoformat(last_seen)
        else:
            last_seen = datetime.now(timezone.utc)
        return cls(
            agent_name=data["agent_name"],
            instance_id=data["instance_id"],
            model=data.get("model", ""),
            tools=data.get("tools", []),
            specialties=data.get("specialties", []),
            endpoint=data.get("endpoint", ""),
            last_seen=last_seen,
        )


@dataclass
class A2ARequest:
    """Cross-instance task delegation request."""

    id: str = field(default_factory=lambda: str(uuid4()))
    from_instance: str = ""
    to_instance: str = ""
    agent_name: str = ""
    task: str = ""
    context: dict[str, Any] | None = None  # Optional context data
    timeout_seconds: int = 120
    priority: str = "normal"  # "low" | "normal" | "high"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from_instance": self.from_instance,
            "to_instance": self.to_instance,
            "agent_name": self.agent_name,
            "task": self.task,
            "context": self.context,
            "timeout_seconds": self.timeout_seconds,
            "priority": self.priority,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> A2ARequest:
        ts = data.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        else:
            ts = datetime.now(timezone.utc)
        return cls(
            id=data.get("id", str(uuid4())),
            from_instance=data.get("from_instance", ""),
            to_instance=data.get("to_instance", ""),
            agent_name=data.get("agent_name", ""),
            task=data.get("task", ""),
            context=data.get("context"),
            timeout_seconds=data.get("timeout_seconds", 120),
            priority=data.get("priority", "normal"),
            timestamp=ts,
        )


@dataclass
class A2AResponse:
    """Cross-instance task delegation response."""

    request_id: str = ""
    success: bool = False
    result: str | dict[str, Any] | None = None
    error: str | None = None
    duration_ms: int = 0
    model_used: str = ""
    instance_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "success": self.success,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "model_used": self.model_used,
            "instance_id": self.instance_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> A2AResponse:
        return cls(
            request_id=data.get("request_id", ""),
            success=data.get("success", False),
            result=data.get("result"),
            error=data.get("error"),
            duration_ms=data.get("duration_ms", 0),
            model_used=data.get("model_used", ""),
            instance_id=data.get("instance_id", ""),
        )
