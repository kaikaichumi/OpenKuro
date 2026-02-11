"""LINE adapter stub.

To be implemented in a future phase. Install line-bot-sdk:
    poetry add line-bot-sdk

Then implement the LineAdapter following the BaseAdapter interface.
"""

from __future__ import annotations

from src.adapters.base import BaseAdapter
from src.config import KuroConfig
from src.core.engine import Engine


class LineAdapter(BaseAdapter):
    """LINE messaging adapter (not yet implemented).

    Planned features:
    - LINE Messaging API v3 with webhook
    - Per-user sessions
    - Quick reply buttons for approval
    - Flex message formatting
    - Rich menu integration
    """

    name = "line"

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        super().__init__(engine, config)

    async def start(self) -> None:
        """Start the LINE bot."""
        raise NotImplementedError(
            "LINE adapter is not yet implemented. "
            "Install line-bot-sdk and implement this adapter, or "
            "contribute to the project!"
        )

    async def stop(self) -> None:
        """Stop the LINE bot."""
        pass
