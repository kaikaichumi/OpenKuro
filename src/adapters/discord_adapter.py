"""Discord adapter stub.

To be implemented in a future phase. Install discord.py:
    poetry add discord.py

Then implement the DiscordAdapter following the BaseAdapter interface.
"""

from __future__ import annotations

from src.adapters.base import BaseAdapter
from src.config import KuroConfig
from src.core.engine import Engine


class DiscordAdapter(BaseAdapter):
    """Discord messaging adapter (not yet implemented).

    Planned features:
    - discord.py v2+ with slash commands
    - Per-channel or per-user sessions
    - Reaction-based approval
    - Embed formatting for rich responses
    - Voice channel support (optional)
    """

    name = "discord"

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        super().__init__(engine, config)

    async def start(self) -> None:
        """Start the Discord bot."""
        raise NotImplementedError(
            "Discord adapter is not yet implemented. "
            "Install discord.py and implement this adapter, or "
            "contribute to the project!"
        )

    async def stop(self) -> None:
        """Stop the Discord bot."""
        pass
