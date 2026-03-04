"""Adapter manager: lifecycle management for messaging adapters.

Handles registration, concurrent startup, and graceful shutdown
of all configured messaging adapters.

Supports multi-instance adapters: the same adapter type (e.g. Discord)
can run multiple instances, each bound to a different AgentInstance
with its own bot token, memory, and personality.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog

from src.adapters.base import BaseAdapter
from src.config import KuroConfig
from src.core.engine import Engine

logger = structlog.get_logger()
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_env_var_name(value: str) -> bool:
    """Check whether *value* is a valid environment variable name."""
    return bool(_ENV_VAR_NAME_RE.fullmatch((value or "").strip()))


def _safe_env_ref(value: str) -> str:
    """Return a safe log representation for env var references."""
    ref = (value or "").strip()
    if not ref:
        return ""
    if _is_env_var_name(ref):
        return ref
    return "[invalid_or_secret]"


class AdapterManager:
    """Manages the lifecycle of multiple messaging adapters.

    Adapters are registered with composite keys (e.g. "discord:main",
    "discord:customer-service") to support multiple instances of the
    same adapter type.

    On shutdown, all adapters are stopped gracefully.
    """

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        self.engine = engine
        self.config = config
        self._adapters: dict[str, BaseAdapter] = {}

    def register(self, adapter: BaseAdapter, instance_id: str | None = None) -> None:
        """Register an adapter for management.

        Args:
            adapter: The adapter instance to register.
            instance_id: Optional agent instance ID for multi-instance.
                         Key becomes "type:instance_id" (e.g. "discord:cs-bot").
                         If None, uses the adapter name as-is.
        """
        if instance_id:
            key = f"{adapter.name}:{instance_id}"
        else:
            key = adapter.name
        self._adapters[key] = adapter
        logger.info("adapter_registered", adapter=key)

    def get(self, name: str) -> BaseAdapter | None:
        """Get a registered adapter by name or composite key."""
        return self._adapters.get(name)

    def get_by_type(self, adapter_type: str) -> list[BaseAdapter]:
        """Get all adapters of a given type (e.g. all Discord adapters)."""
        return [
            a for key, a in self._adapters.items()
            if key == adapter_type or key.startswith(f"{adapter_type}:")
        ]

    @property
    def adapter_names(self) -> list[str]:
        """Get keys of all registered adapters."""
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
            adapter_name: Name or composite key of the adapter
                         (e.g., "discord", "discord:cs-bot", "telegram").
            user_id: Platform-specific user/channel identifier.
            message: The notification message to send.

        Returns:
            True if the message was sent successfully.
        """
        adapter = self._adapters.get(adapter_name)
        # Fallback: try base type if composite key not found
        if adapter is None and ":" not in adapter_name:
            for key, a in self._adapters.items():
                if key == adapter_name or key.startswith(f"{adapter_name}:"):
                    adapter = a
                    break
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

    async def remove_instance_adapters(self, instance_id: str) -> None:
        """Stop and remove all adapters bound to a specific instance ID."""
        keys = [
            key for key in self._adapters.keys()
            if ":" in key and key.split(":", 1)[1] == instance_id
        ]
        for key in keys:
            adapter = self._adapters.get(key)
            if adapter is None:
                continue
            await self._stop_adapter(key, adapter)
            self._adapters.pop(key, None)
            logger.info("instance_adapter_removed", adapter=key, instance_id=instance_id)

    async def sync_instance_adapter(self, agent_instance: Any) -> None:
        """Ensure adapter state matches the instance's current bot binding.

        - No binding: remove existing instance adapters.
        - Binding present: recreate and start the bound adapter.
        """
        instance_id = agent_instance.id
        binding = agent_instance.config.bot_binding

        # Always clear old adapters first to avoid duplicate bots.
        await self.remove_instance_adapters(instance_id)

        if not binding.adapter_type or not binding.bot_token_env:
            return
        if not _is_env_var_name(binding.bot_token_env):
            logger.error(
                "instance_adapter_invalid_token_env",
                instance_id=instance_id,
                adapter_type=binding.adapter_type,
                token_env=_safe_env_ref(binding.bot_token_env),
            )
            return

        adapter = _create_instance_adapter(
            adapter_type=binding.adapter_type,
            engine=self.engine,
            config=self.config,
            agent_instance=agent_instance,
            bot_token_env=binding.bot_token_env,
            overrides=binding.overrides,
        )
        if not adapter:
            logger.warning(
                "instance_adapter_unknown_type",
                instance_id=instance_id,
                adapter_type=binding.adapter_type,
            )
            return

        key = f"{adapter.name}:{instance_id}"
        self._adapters[key] = adapter
        agent_instance.bound_adapter = adapter
        logger.info(
            "instance_adapter_bound",
            instance_id=instance_id,
            adapter_type=binding.adapter_type,
            token_env=_safe_env_ref(binding.bot_token_env),
        )
        await self._start_adapter(key, adapter)

    @classmethod
    def from_config(
        cls,
        engine: Engine,
        config: KuroConfig,
        adapters: list[str] | None = None,
    ) -> AdapterManager:
        """Create an AdapterManager and register adapters based on config.

        Registers both:
        1. Main adapters (from config.adapters settings)
        2. Instance-bound adapters (from AgentInstance bot_binding)

        Args:
            engine: The core Engine instance.
            config: The application configuration.
            adapters: List of adapter names to enable. If None, uses
                     config to determine which adapters are enabled.

        Returns:
            Configured AdapterManager ready to start.
        """
        manager = cls(engine, config)

        # --- 1. Register main adapters (existing behavior) ---
        if adapters is None:
            adapters = []
            if config.adapters.telegram.enabled:
                adapters.append("telegram")
            if config.adapters.discord.enabled:
                adapters.append("discord")
            if config.adapters.slack.enabled:
                adapters.append("slack")
            if config.adapters.line.enabled:
                adapters.append("line")
            if config.adapters.email.enabled:
                adapters.append("email")

        for name in adapters:
            adapter = _create_adapter(name, engine, config)
            if adapter:
                manager.register(adapter)
            else:
                logger.warning("unknown_adapter", adapter=name)

        # --- 2. Register instance-bound adapters ---
        instance_manager = getattr(engine, "instance_manager", None)
        if instance_manager:
            for inst in instance_manager.list_all():
                binding = inst.config.bot_binding
                if not binding.adapter_type or not binding.bot_token_env:
                    continue
                if not _is_env_var_name(binding.bot_token_env):
                    logger.error(
                        "instance_adapter_invalid_token_env",
                        instance_id=inst.id,
                        adapter_type=binding.adapter_type,
                        token_env=_safe_env_ref(binding.bot_token_env),
                        message=(
                            "bot_token_env must be an environment variable name "
                            "(e.g. KURO_DISCORD_TOKEN_CS), not the token value"
                        ),
                    )
                    continue

                adapter = _create_instance_adapter(
                    adapter_type=binding.adapter_type,
                    engine=engine,
                    config=config,
                    agent_instance=inst,
                    bot_token_env=binding.bot_token_env,
                    overrides=binding.overrides,
                )
                if adapter:
                    manager.register(adapter, instance_id=inst.id)
                    inst.bound_adapter = adapter
                    logger.info(
                        "instance_adapter_bound",
                        instance_id=inst.id,
                        adapter_type=binding.adapter_type,
                        token_env=_safe_env_ref(binding.bot_token_env),
                    )
                else:
                    logger.warning(
                        "instance_adapter_unknown_type",
                        instance_id=inst.id,
                        adapter_type=binding.adapter_type,
                    )

        return manager


def _create_adapter(
    name: str,
    engine: Engine,
    config: KuroConfig,
) -> BaseAdapter | None:
    """Create a main adapter by name."""
    if name == "telegram":
        from src.adapters.telegram_adapter import TelegramAdapter
        return TelegramAdapter(engine, config)
    elif name == "discord":
        from src.adapters.discord_adapter import DiscordAdapter
        return DiscordAdapter(engine, config)
    elif name == "slack":
        from src.adapters.slack_adapter import SlackAdapter
        return SlackAdapter(engine, config)
    elif name == "line":
        from src.adapters.line_adapter import LineAdapter
        return LineAdapter(engine, config)
    elif name == "email":
        from src.adapters.email_adapter import EmailAdapter
        return EmailAdapter(engine, config)
    return None


def _create_instance_adapter(
    adapter_type: str,
    engine: Engine,
    config: KuroConfig,
    agent_instance: Any,
    bot_token_env: str,
    overrides: dict[str, Any] | None = None,
) -> BaseAdapter | None:
    """Create an adapter bound to an AgentInstance.

    The adapter will route messages through the instance's Engine
    and use the specified bot token environment variable.
    """
    if adapter_type == "discord":
        from src.adapters.discord_adapter import DiscordAdapter
        return DiscordAdapter(
            engine=engine,
            config=config,
            agent_instance=agent_instance,
            bot_token_override=bot_token_env,
            config_overrides=overrides,
        )
    elif adapter_type == "telegram":
        from src.adapters.telegram_adapter import TelegramAdapter
        return TelegramAdapter(
            engine=engine,
            config=config,
            agent_instance=agent_instance,
            bot_token_override=bot_token_env,
            config_overrides=overrides,
        )
    elif adapter_type == "slack":
        from src.adapters.slack_adapter import SlackAdapter
        return SlackAdapter(
            engine=engine,
            config=config,
            agent_instance=agent_instance,
            bot_token_override=bot_token_env,
            config_overrides=overrides,
        )
    elif adapter_type == "line":
        from src.adapters.line_adapter import LineAdapter
        return LineAdapter(
            engine=engine,
            config=config,
            agent_instance=agent_instance,
        )
    elif adapter_type == "email":
        from src.adapters.email_adapter import EmailAdapter
        return EmailAdapter(
            engine=engine,
            config=config,
            agent_instance=agent_instance,
        )
    return None
