"""Base adapter interface for messaging platforms.

All messaging adapters (Telegram, Discord, LINE, etc.) should implement
this interface. The adapter manages per-user sessions and delegates
message processing to the core Engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import structlog

from src.config import KuroConfig
from src.core.engine import Engine
from src.core.types import Session

logger = structlog.get_logger()


class BaseAdapter(ABC):
    """Abstract base class for messaging platform adapters.

    Subclasses must implement:
    - start(): Initialize and begin receiving messages
    - stop(): Gracefully shutdown the adapter

    The adapter maintains a session map (user_key -> Session) so each
    user gets their own conversation context.
    """

    name: str = "base"

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        self.engine = engine
        self.config = config
        self._sessions: dict[str, Session] = {}

    @abstractmethod
    async def start(self) -> None:
        """Start the adapter (connect, begin polling/listening)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the adapter."""
        ...

    def get_or_create_session(self, user_key: str) -> Session:
        """Get an existing session or create a new one for the user.

        Args:
            user_key: Platform-specific user identifier (e.g., Telegram user ID).

        Returns:
            The user's Session instance.
        """
        if user_key not in self._sessions:
            self._sessions[user_key] = Session(
                adapter=self.name,
                user_id=user_key,
            )
            logger.info(
                "session_created",
                adapter=self.name,
                user_key=user_key,
                session_id=self._sessions[user_key].id,
            )
        return self._sessions[user_key]

    def clear_session(self, user_key: str) -> None:
        """Clear (reset) a user's session."""
        if user_key in self._sessions:
            del self._sessions[user_key]
            logger.info("session_cleared", adapter=self.name, user_key=user_key)

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)
