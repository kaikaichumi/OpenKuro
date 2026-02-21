"""Adapter manager: lifecycle management for messaging adapters.

Handles registration, concurrent startup, and graceful shutdown
of all configured messaging adapters.
"""

from __future__ import annotations

import asyncio

import structlog

from src.adapters.base import BaseAdapter
from src.config import KuroConfig
from src.core.engine import Engine

logger = structlog.get_logger()


class AdapterManager:
    """Manages the lifecycle of multiple messaging adapters.

    Adapters are registered, then started concurrently.
    On shutdown, all adapters are stopped gracefully.
    """

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        self.engine = engine
        self.config = config
        self._adapters: dict[str, BaseAdapter] = {}

    def register(self, adapter: BaseAdapter) -> None:
        """Register an adapter for management."""
        self._adapters[adapter.name] = adapter
        logger.info("adapter_registered", adapter=adapter.name)

    def get(self, name: str) -> BaseAdapter | None:
        """Get a registered adapter by name."""
        return self._adapters.get(name)

    @property
    def adapter_names(self) -> list[str]:
        """Get names of all registered adapters."""
        return list(self._adapters.keys())

    async def start_all(self) -> None:
        """Start all registered adapters concurrently.

        If any adapter fails to start, it logs the error but
        continues starting the others.
        """
        if not self._adapters:
            logger.warning("no_adapters_registered")
            return

        logger.info(
            "adapters_starting",
            adapters=list(self._adapters.keys()),
        )

        tasks = []
        for name, adapter in self._adapters.items():
            tasks.append(self._start_adapter(name, adapter))

        await asyncio.gather(*tasks)

        running = [n for n, a in self._adapters.items() if hasattr(a, "_app") and a._app]
        logger.info("adapters_started", running=running)

    async def _start_adapter(self, name: str, adapter: BaseAdapter) -> None:
        """Start a single adapter with error handling."""
        try:
            await adapter.start()
            logger.info("adapter_started", adapter=name)
        except NotImplementedError as e:
            logger.warning("adapter_not_implemented", adapter=name, error=str(e))
        except Exception as e:
            logger.error("adapter_start_failed", adapter=name, error=str(e))

    async def send_notification(
        self,
        adapter_name: str,
        user_id: str,
        message: str,
    ) -> bool:
        """Send a proactive notification through a specific adapter.

        Used by the scheduler and workflow engine to push results
        to users via Discord/Telegram.

        Args:
            adapter_name: Name of the adapter (e.g., "discord", "telegram").
            user_id: Platform-specific user/channel identifier.
            message: The notification message to send.

        Returns:
            True if the message was sent successfully.
        """
        adapter = self._adapters.get(adapter_name)
        if adapter is None:
            logger.warning(
                "notification_adapter_not_found",
                adapter=adapter_name,
            )
            return False
        try:
            return await adapter.send_notification(user_id, message)
        except Exception as e:
            logger.error(
                "notification_send_failed",
                adapter=adapter_name,
                error=str(e),
            )
            return False

    async def stop_all(self) -> None:
        """Stop all registered adapters gracefully."""
        logger.info("adapters_stopping")

        tasks = []
        for name, adapter in self._adapters.items():
            tasks.append(self._stop_adapter(name, adapter))

        await asyncio.gather(*tasks, return_exceptions=True)

        # Give adapters time to cleanup (fixes Windows pipe cleanup warnings)
        await asyncio.sleep(0.1)

        logger.info("adapters_stopped")

    async def _stop_adapter(self, name: str, adapter: BaseAdapter) -> None:
        """Stop a single adapter with error handling."""
        try:
            await adapter.stop()
        except Exception as e:
            logger.warning("adapter_stop_error", adapter=name, error=str(e))

    @classmethod
    def from_config(
        cls,
        engine: Engine,
        config: KuroConfig,
        adapters: list[str] | None = None,
    ) -> AdapterManager:
        """Create an AdapterManager and register adapters based on config.

        Args:
            engine: The core Engine instance.
            config: The application configuration.
            adapters: List of adapter names to enable. If None, uses
                     config to determine which adapters are enabled.

        Returns:
            Configured AdapterManager ready to start.
        """
        manager = cls(engine, config)

        # Determine which adapters to enable
        if adapters is None:
            adapters = []
            if config.adapters.telegram.enabled:
                adapters.append("telegram")
            if config.adapters.discord.enabled:
                adapters.append("discord")

        for name in adapters:
            if name == "telegram":
                from src.adapters.telegram_adapter import TelegramAdapter
                adapter = TelegramAdapter(engine, config)
                manager.register(adapter)
            elif name == "discord":
                from src.adapters.discord_adapter import DiscordAdapter
                adapter = DiscordAdapter(engine, config)
                manager.register(adapter)
            elif name == "line":
                from src.adapters.line_adapter import LineAdapter
                adapter = LineAdapter(engine, config)
                manager.register(adapter)
            else:
                logger.warning("unknown_adapter", adapter=name)

        return manager
