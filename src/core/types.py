"""Shared data types for the Kuro assistant."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class Role(str, Enum):
    """Message role in conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class Message:
    """A single message in the conversation."""

    role: Role
    content: str
    name: str | None = None  # Tool name for tool messages
    tool_call_id: str | None = None  # For tool result messages
    tool_calls: list[ToolCall] | None = None  # For assistant messages with tool calls
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_litellm(self) -> dict[str, Any]:
        """Convert to LiteLLM-compatible message dict."""
        msg: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.name:
            msg["name"] = self.name
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_litellm() for tc in self.tool_calls]
            # When there are tool calls, content may be None
            if not self.content:
                msg["content"] = None
        return msg


@dataclass
class ToolCall:
    """A tool call requested by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_litellm(self) -> dict[str, Any]:
        """Convert to LiteLLM tool_call format."""
        import json

        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments),
            },
        }

    @classmethod
    def from_litellm(cls, raw: dict[str, Any]) -> ToolCall:
        """Parse from LiteLLM response tool_call."""
        import json

        func = raw.get("function", {})
        args_str = func.get("arguments", "{}")
        try:
            arguments = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            arguments = {"_raw": args_str}

        return cls(
            id=raw.get("id", str(uuid4())),
            name=func.get("name", "unknown"),
            arguments=arguments,
        )


@dataclass
class Session:
    """A conversation session."""

    id: str = field(default_factory=lambda: str(uuid4()))
    adapter: str = "cli"  # Source adapter (cli, web, telegram, discord, line)
    user_id: str = "local"  # Platform-specific user identifier
    messages: list[Message] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trust_level: str = "low"  # Current session trust level

    def add_message(self, msg: Message) -> None:
        """Append a message to the session."""
        self.messages.append(msg)

    def get_litellm_messages(self) -> list[dict[str, Any]]:
        """Get all messages in LiteLLM-compatible format."""
        return [m.to_litellm() for m in self.messages]


@dataclass
class AgentDefinition:
    """Runtime definition for a sub-agent."""

    name: str
    model: str
    system_prompt: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    max_tool_rounds: int = 5
    temperature: float | None = None
    max_tokens: int | None = None
    created_by: str = "user"  # "user" | "config"


@dataclass
class ModelResponse:
    """Response from an LLM model call."""

    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)
