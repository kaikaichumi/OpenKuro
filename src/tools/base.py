"""Base tool interface and shared types for the tool system."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(Enum):
    """Risk classification for tool operations."""

    LOW = "low"  # Read-only (list files, get time, read calendar)
    MEDIUM = "medium"  # Modifications within sandbox (write files, clipboard)
    HIGH = "high"  # System-level (shell commands, install packages)
    CRITICAL = "critical"  # Destructive / external (delete files, send messages)

    def __le__(self, other: RiskLevel) -> bool:
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        return order.index(self) <= order.index(other)

    def __lt__(self, other: RiskLevel) -> bool:
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        return order.index(self) < order.index(other)

    def __ge__(self, other: RiskLevel) -> bool:
        return not self.__lt__(other)

    def __gt__(self, other: RiskLevel) -> bool:
        return not self.__le__(other)


@dataclass
class ToolResult:
    """Result of a tool execution."""

    success: bool
    output: str = ""
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, output: str = "", **data: Any) -> ToolResult:
        return cls(success=True, output=output, data=data)

    @classmethod
    def fail(cls, error: str, **data: Any) -> ToolResult:
        return cls(success=False, error=error, data=data)

    @classmethod
    def denied(cls, reason: str) -> ToolResult:
        return cls(success=False, error=f"Denied: {reason}")


@dataclass
class ToolContext:
    """Execution context passed to tools."""

    session_id: str
    working_directory: str | None = None
    allowed_directories: list[str] = field(default_factory=list)
    max_execution_time: int = 30
    max_output_size: int = 100_000
    agent_manager: Any = None  # AgentManager | None (Any avoids circular import)


class BaseTool(ABC):
    """Abstract base class for all tools.

    Each tool must define:
    - name: unique identifier used by the LLM
    - description: human-readable description shown to the LLM
    - parameters: JSON Schema dict describing expected parameters
    - risk_level: risk classification for the approval system
    """

    name: str
    description: str
    parameters: dict[str, Any]
    risk_level: RiskLevel

    @abstractmethod
    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool with given parameters and context."""
        ...

    def to_openai_tool(self) -> dict[str, Any]:
        """Convert to OpenAI-compatible tool definition for LLM function calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
