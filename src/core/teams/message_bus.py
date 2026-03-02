"""MessageBus: async inter-agent messaging for team collaboration.

Provides point-to-point and broadcast messaging between team roles,
backed by per-role asyncio.Queues.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import structlog

from src.core.teams.types import TeamMessage

logger = structlog.get_logger()


class MessageBus:
    """Async message bus for inter-agent communication within a team.

    Each role gets a dedicated asyncio.Queue. Messages can be sent to
    a specific role (point-to-point) or broadcast to all roles.
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[TeamMessage]] = {}
        self._all_messages: list[TeamMessage] = []

    def register_role(self, role_name: str) -> None:
        """Register a role on the message bus, creating its message queue."""
        if role_name not in self._queues:
            self._queues[role_name] = asyncio.Queue()
            logger.debug("message_bus_role_registered", role=role_name)

    def unregister_role(self, role_name: str) -> None:
        """Remove a role from the message bus."""
        self._queues.pop(role_name, None)

    async def send(self, msg: TeamMessage) -> None:
        """Send a message to a specific role or broadcast to all.

        Args:
            msg: The message to send. If to_role is None, broadcasts to all.
        """
        self._all_messages.append(msg)

        if msg.to_role:
            # Point-to-point
            queue = self._queues.get(msg.to_role)
            if queue:
                await queue.put(msg)
                logger.debug(
                    "message_bus_sent",
                    from_role=msg.from_role,
                    to_role=msg.to_role,
                    msg_type=msg.msg_type,
                )
            else:
                logger.warning(
                    "message_bus_target_not_found",
                    from_role=msg.from_role,
                    to_role=msg.to_role,
                )
        else:
            # Broadcast to all except sender
            for role_name, queue in self._queues.items():
                if role_name != msg.from_role:
                    await queue.put(msg)
            logger.debug(
                "message_bus_broadcast",
                from_role=msg.from_role,
                recipients=len(self._queues) - 1,
            )

    async def receive(self, role_name: str) -> list[TeamMessage]:
        """Receive all pending messages for a role (non-blocking).

        Returns all queued messages immediately without waiting.
        """
        messages: list[TeamMessage] = []
        queue = self._queues.get(role_name)
        if not queue:
            return messages

        while not queue.empty():
            try:
                msg = queue.get_nowait()
                messages.append(msg)
            except asyncio.QueueEmpty:
                break

        return messages

    async def send_simple(
        self,
        from_role: str,
        to_role: str | None,
        content: str,
        msg_type: str = "data",
    ) -> TeamMessage:
        """Convenience method to create and send a message in one call."""
        msg = TeamMessage(
            id=str(uuid4()),
            from_role=from_role,
            to_role=to_role,
            content=content,
            timestamp=datetime.now(timezone.utc),
            msg_type=msg_type,
        )
        await self.send(msg)
        return msg

    @property
    def all_messages(self) -> list[TeamMessage]:
        """Get all messages ever sent through this bus."""
        return list(self._all_messages)

    @property
    def message_count(self) -> int:
        """Total number of messages sent."""
        return len(self._all_messages)

    @property
    def registered_roles(self) -> list[str]:
        """List of registered role names."""
        return list(self._queues.keys())

    def format_messages(self, messages: list[TeamMessage]) -> str:
        """Format a list of messages as human-readable text for LLM context."""
        if not messages:
            return "[No peer messages]"

        lines = ["[Peer Messages]"]
        for msg in messages:
            sender = msg.from_role
            target = f"→{msg.to_role}" if msg.to_role else "→all"
            lines.append(f"  [{sender}{target}] ({msg.msg_type}): {msg.content[:300]}")
        return "\n".join(lines)
