"""Working memory: in-memory conversation context for current session.

Manages the sliding window of messages that are sent to the LLM,
handling token budget and context window limits.
"""

from __future__ import annotations

from src.core.types import Message, Role


class WorkingMemory:
    """In-memory conversation context with sliding window management."""

    def __init__(self, max_messages: int = 50) -> None:
        """Initialize working memory.

        Args:
            max_messages: Maximum number of messages to keep in context.
                          Oldest messages (except system) are dropped when exceeded.
        """
        self.max_messages = max_messages

    def trim(self, messages: list[Message]) -> list[Message]:
        """Trim messages to fit within the context window.

        Preserves:
        - The system message (always first)
        - The most recent messages up to max_messages
        """
        if len(messages) <= self.max_messages:
            return messages

        # Keep system message + most recent messages
        system_msgs = [m for m in messages if m.role == Role.SYSTEM]
        non_system = [m for m in messages if m.role != Role.SYSTEM]

        # Keep the latest messages that fit
        keep_count = self.max_messages - len(system_msgs)
        trimmed = non_system[-keep_count:] if keep_count > 0 else []

        return system_msgs + trimmed

    def estimate_tokens(self, messages: list[Message]) -> int:
        """Rough estimate of token count for messages.

        Uses ~4 chars per token as a simple heuristic.
        Not accurate but sufficient for budget management.
        """
        total_chars = sum(len(m.content or "") for m in messages)
        return total_chars // 4
