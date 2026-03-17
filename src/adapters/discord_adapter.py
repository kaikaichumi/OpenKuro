"""Discord adapter: full implementation using discord.py v2+.

Features:
- Async bot with Intents (message_content, guilds)
- Per-channel+user sessions with conversation history
- discord.ui.Button for tool approval
- Smart message splitting (2000 char limit)
- Prefix commands: !help, !model, !models, !clear, !trust
- User/channel whitelist (optional, empty = allow all)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from typing import Any
from uuid import uuid4

import structlog

from src.adapters.base import BaseAdapter
from src.config import KuroConfig
from src.core.engine import ApprovalCallback, Engine
from src.core.types import Session
from src.openai_catalog import (
    OPENAI_CODEX_OAUTH_MODELS,
    is_codex_oauth_model_supported,
    normalize_openai_model,
)
from src.tools.base import RiskLevel, ToolContext

logger = structlog.get_logger()
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Approval timeout in seconds
DEFAULT_APPROVAL_TIMEOUT = 60


class DiscordApprovalCallback(ApprovalCallback):
    """Approval callback that uses Discord buttons (discord.ui.View).

    When a tool needs approval, sends a message with Allow/Deny/Trust
    buttons. The user presses a button, which resolves an asyncio Future
    that the engine is awaiting.
    """

    def __init__(self, approval_policy=None) -> None:
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._bot = None  # Set by DiscordAdapter after bot is ready
        self._channel_map: dict[str, int] = {}  # session_id -> channel_id
        self._timeout: int = DEFAULT_APPROVAL_TIMEOUT
        self.approval_policy = approval_policy

    def set_bot(self, bot) -> None:
        """Set the bot instance (called by DiscordAdapter)."""
        self._bot = bot

    def register_channel(self, session_id: str, channel_id: int) -> None:
        """Map a session to its Discord channel ID."""
        self._channel_map[session_id] = channel_id

    async def request_approval(
        self,
        tool_name: str,
        params: dict[str, Any],
        risk_level: RiskLevel,
        session: Session,
    ) -> bool:
        """Send button view to Discord and await user's decision."""
        if self._bot is None:
            logger.error("discord_approval_no_bot")
            return False

        channel_id = self._channel_map.get(session.id)
        if channel_id is None:
            # Fallback 1: session metadata cache (when session object is copied/reused).
            meta = getattr(session, "metadata", {}) or {}
            raw_meta_channel = meta.get("_discord_channel_id")
            if isinstance(raw_meta_channel, int):
                channel_id = raw_meta_channel
            elif isinstance(raw_meta_channel, str) and raw_meta_channel.isdigit():
                channel_id = int(raw_meta_channel)

            # Fallback 2: discord session user_id is "{channel_id}:{user_id}".
            if channel_id is None:
                raw_user = str(getattr(session, "user_id", "") or "")
                if ":" in raw_user:
                    maybe_channel = raw_user.split(":", 1)[0].strip()
                    if maybe_channel.isdigit():
                        channel_id = int(maybe_channel)

            if channel_id is not None:
                self._channel_map[session.id] = channel_id
                logger.info(
                    "discord_approval_channel_fallback",
                    session_id=session.id,
                    channel_id=channel_id,
                    tool=tool_name,
                )
        if channel_id is None:
            logger.error("discord_approval_no_channel", session_id=session.id)
            return False

        try:
            import discord

            channel = self._bot.get_channel(channel_id)
            if channel is None:
                channel = await self._bot.fetch_channel(channel_id)

            approval_id = str(uuid4())[:8]

            # Format parameters for display
            params_text = _format_params_discord(params)

            risk_emoji = {
                RiskLevel.LOW: "\u2705",        # ✅
                RiskLevel.MEDIUM: "\u26a0\ufe0f",     # ⚠️
                RiskLevel.HIGH: "\U0001f534",     # 🔴
                RiskLevel.CRITICAL: "\u2622\ufe0f",    # ☢️
            }
            emoji = risk_emoji.get(risk_level, "\u2753")  # ❓

            text = (
                f"\u26a1 **Approval Required**\n\n"
                f"**Tool:** `{tool_name}`\n"
                f"**Risk:** {emoji} {risk_level.value.upper()}\n"
                f"**Params:**\n{params_text}"
            )

            # Create Future before sending message
            loop = asyncio.get_running_loop()
            future: asyncio.Future[bool] = loop.create_future()
            self._pending[approval_id] = future

            # Store session reference for trust escalation
            self._pending[f"session:{approval_id}"] = session  # type: ignore[assignment]

            # Build the View with buttons
            view = _ApprovalView(
                approval_id=approval_id,
                risk_level=risk_level,
                callback=self,
            )

            await channel.send(content=text, view=view)

            # Wait for user response with timeout
            try:
                result = await asyncio.wait_for(future, timeout=self._timeout)
                return result
            except asyncio.TimeoutError:
                logger.info(
                    "discord_approval_timeout",
                    approval_id=approval_id,
                    tool=tool_name,
                )
                await channel.send(
                    f"\u23f0 Approval timed out for `{tool_name}`. Action denied."
                )
                return False
            finally:
                self._pending.pop(approval_id, None)
                self._pending.pop(f"session:{approval_id}", None)

        except Exception as e:
            logger.error("discord_approval_error", error=str(e))
            return False

    def handle_button(self, action: str, approval_id: str, extra: str | None = None) -> str | None:
        """Process a button press.

        Returns a status message or None.
        """
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return "This approval request has expired."

        if action == "approve":
            future.set_result(True)
            return "\u2705 Approved"
        elif action == "deny":
            future.set_result(False)
            return "\u274c Denied"
        elif action == "trust" and extra:
            # Trust escalation
            session = self._pending.get(f"session:{approval_id}")
            if session and hasattr(session, "trust_level"):
                session.trust_level = extra
                if self.approval_policy:
                    level_map = {
                        "low": RiskLevel.LOW, "medium": RiskLevel.MEDIUM,
                        "high": RiskLevel.HIGH, "critical": RiskLevel.CRITICAL,
                    }
                    rl = level_map.get(extra)
                    if rl:
                        self.approval_policy.elevate_session_trust(session.id, rl)
            future.set_result(True)
            return f"\U0001f513 Trusted {extra.upper()} actions for this session"

        return None


class _ApprovalView:
    """Discord UI View with approval buttons.

    This is a factory that creates the actual discord.ui.View at runtime
    to avoid importing discord at module level.
    """

    def __new__(
        cls,
        approval_id: str,
        risk_level: RiskLevel,
        callback: DiscordApprovalCallback,
    ):
        import discord

        class ApprovalView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=callback._timeout)

            @discord.ui.button(label="Allow", style=discord.ButtonStyle.success, custom_id=f"approve:{approval_id}")
            async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                status = callback.handle_button("approve", approval_id)
                await interaction.response.edit_message(
                    content=f"{interaction.message.content}\n\n**Result:** {status}",
                    view=None,
                )

            @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"deny:{approval_id}")
            async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                status = callback.handle_button("deny", approval_id)
                await interaction.response.edit_message(
                    content=f"{interaction.message.content}\n\n**Result:** {status}",
                    view=None,
                )

            @discord.ui.button(label="Trust this level", style=discord.ButtonStyle.secondary, custom_id=f"trust:{approval_id}")
            async def trust_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                status = callback.handle_button("trust", approval_id, risk_level.value)
                await interaction.response.edit_message(
                    content=f"{interaction.message.content}\n\n**Result:** {status}",
                    view=None,
                )

        return ApprovalView()


class DiscordAdapter(BaseAdapter):
    """Full Discord bot adapter using discord.py v2+.

    Uses message content intent for prefix commands.
    Session key: f"{channel_id}:{user_id}" — each user in each channel
    gets their own session.

    Supports binding to an AgentInstance for multi-bot scenarios:
    each instance gets its own Discord bot with separate token,
    routing messages through the instance's Engine.
    """

    name = "discord"

    def __init__(
        self,
        engine: Engine,
        config: KuroConfig,
        agent_instance: Any = None,
        bot_token_override: str | None = None,
        config_overrides: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(engine, config, agent_instance=agent_instance)

        self._bot = None
        self._bot_token_override = bot_token_override
        self._config_overrides = config_overrides or {}
        try:
            from src.ui.openai_oauth import OpenAIOAuthManager
            self._oauth = OpenAIOAuthManager()
        except Exception:
            self._oauth = None

        # Use the effective engine's approval policy
        target_engine = self.effective_engine
        self._approval_cb = DiscordApprovalCallback(
            approval_policy=target_engine.approval_policy,
        )
        self._approval_cb._timeout = self._get_discord_config("approval_timeout", config.adapters.discord.approval_timeout)

        # Replace the target engine's approval callback with Discord's
        target_engine.approval_cb = self._approval_cb

    def _get_discord_config(self, key: str, default: Any = None) -> Any:
        """Get a Discord config value, checking overrides first."""
        return self._config_overrides.get(key, default)

    @staticmethod
    def _safe_env_label(value: str) -> str:
        """Return a safe env-var label for error messages."""
        ref = (value or "").strip()
        if _ENV_VAR_NAME_RE.fullmatch(ref):
            return ref
        return "<DISCORD_BOT_TOKEN_ENV>"

    @staticmethod
    def _parse_model_selection(raw: str) -> tuple[str | None, str]:
        value = (raw or "").strip()
        if not value:
            return None, "api"
        if value.startswith("oauth:"):
            model = value[len("oauth:"):].strip()
            return (model or None), "oauth"
        if value.startswith("api:"):
            model = value[len("api:"):].strip()
            return (model or None), "api"
        return value, "api"

    async def start(self) -> None:
        """Initialize the Discord bot and start it."""
        # Use token override (from AgentInstance binding) or default config
        if self._bot_token_override:
            import os
            token = os.environ.get(self._bot_token_override, "")
            env_var = self._bot_token_override
        else:
            token = self.config.adapters.discord.get_bot_token()
            env_var = self.config.adapters.discord.bot_token_env
        if not token:
            raise RuntimeError(
                f"Discord bot token not found. "
                f"Set the {self._safe_env_label(env_var)} environment variable."
            )

        import discord

        prefix = self.config.adapters.discord.command_prefix

        # Setup intents
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = False  # Not needed for basic operation

        self._bot = discord.Client(intents=intents)
        self._approval_cb.set_bot(self._bot)

        self._default_channel_id: int | None = None

        # Register event handlers
        @self._bot.event
        async def on_ready():
            logger.info(
                "discord_started",
                bot_user=str(self._bot.user),
                bot_id=self._bot.user.id if self._bot.user else None,
                guild_count=len(self._bot.guilds),
            )
            # Discover a default notification channel
            for guild in self._bot.guilds:
                for channel in guild.text_channels:
                    if self._is_channel_allowed(channel.id):
                        self._default_channel_id = channel.id
                        logger.info("discord_default_channel", channel_id=channel.id, channel_name=channel.name)
                        break
                if self._default_channel_id:
                    break

        @self._bot.event
        async def on_message(message: discord.Message):
            # Ignore own messages
            if message.author == self._bot.user:
                return

            # Ignore bot messages
            if message.author.bot:
                return

            # Check user whitelist
            if not self._is_user_allowed(message.author.id):
                return

            # Check channel whitelist
            if not self._is_channel_allowed(message.channel.id):
                return

            content = message.content.strip()

            # Extract image URLs from attachments and embeds
            image_urls: list[str] = []
            for att in message.attachments:
                if att.content_type and att.content_type.startswith("image/"):
                    image_urls.append(att.url)
                elif att.filename and att.filename.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
                ):
                    image_urls.append(att.url)
            # Also check for image URLs in embeds
            for embed in message.embeds:
                if embed.image and embed.image.url:
                    image_urls.append(embed.image.url)
                if embed.thumbnail and embed.thumbnail.url:
                    image_urls.append(embed.thumbnail.url)
            # Extract image URLs pasted directly in message text
            _IMG_URL_RE = re.compile(
                r'(https?://\S+\.(?:png|jpg|jpeg|gif|webp|bmp)(?:\?\S*)?)',
                re.IGNORECASE,
            )
            for url_match in _IMG_URL_RE.findall(content):
                if url_match not in image_urls:
                    image_urls.append(url_match)

            # If no text content but has images, use a placeholder
            if not content and not image_urls:
                return
            if not content and image_urls:
                content = "(image attached)"

            # Handle prefix commands
            if content.startswith(prefix):
                asyncio.create_task(
                    self._safe_handle(
                        self._handle_command(message, content[len(prefix):]),
                        message.channel,
                    )
                )
                return

            # Check if bot is mentioned or in DM
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = self._bot.user in message.mentions if self._bot.user else False

            # In guilds (servers), only respond when mentioned
            if not is_dm and not is_mentioned:
                return

            # Remove bot mention from message if present
            if is_mentioned and self._bot.user:
                content = content.replace(f"<@{self._bot.user.id}>", "").strip()
                content = content.replace(f"<@!{self._bot.user.id}>", "").strip()

            if not content:
                return

            asyncio.create_task(
                self._safe_handle(
                    self._handle_message(message, content, image_urls=image_urls),
                    message.channel,
                )
            )

        logger.info("discord_starting", bot_token="***" + token[-4:])

        # Start the bot (runs until stopped)
        # Use asyncio.create_task so it doesn't block
        self._task = asyncio.create_task(self._bot.start(token))

        # Wait briefly for the bot to connect
        await asyncio.sleep(2)

    async def stop(self) -> None:
        """Stop the Discord bot gracefully."""
        if self._bot:
            try:
                await self._bot.close()
            except Exception as e:
                logger.warning("discord_stop_error", error=str(e))
            logger.info("discord_stopped")

    async def _safe_handle(self, coro, channel) -> None:
        """Run a handler coroutine in a background task with error handling.

        This allows on_message to return immediately so Discord can
        process new messages while a sub-agent or the main engine is busy.
        """
        try:
            await coro
        except Exception as e:
            logger.error("discord_handler_error", error=str(e))
            try:
                await channel.send(f"\u274c Error: {str(e)[:200]}")
            except Exception:
                pass

    def _build_tool_context(self, session: Session) -> ToolContext:
        """Build a ToolContext bound to the current Discord session."""
        engine = self.effective_engine
        active_model = str(
            session.metadata.get("_active_model")
            or session.metadata.get("model_override")
            or engine.model.default_model
        )
        return ToolContext(
            session_id=session.id,
            config=self.config,
            model_router=engine.model,
            active_model=active_model,
            allowed_directories=[str(p) for p in self.config.sandbox.allowed_directories],
            max_execution_time=self.config.sandbox.max_execution_time,
            max_output_size=self.config.sandbox.max_output_size,
            agent_manager=getattr(engine, "agent_manager", None),
            team_manager=getattr(engine, "team_manager", None),
            instance_manager=getattr(engine, "instance_manager", None),
            memory_manager=getattr(engine, "memory", None),
            agent_instance_id=getattr(engine, "agent_instance_id", None),
            session=session,
        )

    async def _send_chunked_message(
        self,
        channel: Any,
        text: str,
        *,
        max_len: int | None = None,
        fallback: str | None = None,
    ) -> int:
        """Send non-empty chunks only (Discord rejects empty messages)."""
        limit = max_len or self.config.adapters.discord.max_message_length
        raw_chunks = split_message(text or "", limit)
        chunks = [c for c in raw_chunks if isinstance(c, str) and c.strip()]

        if not chunks:
            if fallback and fallback.strip():
                chunks = [fallback]
            else:
                logger.warning("discord_empty_outgoing_message_suppressed")
                return 0

        sent = 0
        for chunk in chunks:
            await channel.send(chunk)
            sent += 1
        return sent

    async def _run_diagnose_and_repair(
        self,
        session: Session,
        *,
        scope: str = "full",
        auto_fix: bool = True,
    ) -> str:
        """Run diagnose_and_repair tool directly for manual/auto self-heal."""
        scope_norm = str(scope or "full").strip().lower()
        if scope_norm not in {"full", "errors", "performance", "config"}:
            scope_norm = "full"

        try:
            result = await self.effective_engine.tools.execute(
                "diagnose_and_repair",
                {"scope": scope_norm, "auto_fix": bool(auto_fix)},
                self._build_tool_context(session),
            )
            if result.success:
                return result.output or "\u2705 Self-repair completed."
            return f"\u274c Self-repair failed: {result.error or 'unknown error'}"
        except Exception as e:
            return f"\u274c Self-repair failed: {str(e)[:300]}"

    async def _maybe_auto_repair(
        self,
        message: Any,
        session: Session,
        *,
        trigger_error: str,
    ) -> None:
        """Auto-trigger diagnose_and_repair after consecutive adapter errors."""
        diag = getattr(self.config, "diagnostics", None)
        if not diag or not getattr(diag, "enabled", True):
            return
        if not bool(getattr(diag, "auto_diagnose_on_error", True)):
            return

        try:
            threshold = int(getattr(diag, "error_threshold", 3))
        except Exception:
            threshold = 3
        threshold = max(1, threshold)

        streak = int(session.metadata.get("_discord_error_streak", 0) or 0)
        if streak < threshold:
            return
        if session.metadata.get("_discord_auto_repair_running"):
            return

        session.metadata["_discord_auto_repair_running"] = True
        try:
            logger.warning(
                "discord_auto_repair_triggered",
                session_id=session.id,
                streak=streak,
                threshold=threshold,
                error=trigger_error[:120],
            )
            report = await self._run_diagnose_and_repair(
                session,
                scope="errors",
                auto_fix=True,
            )
            await self._send_chunked_message(
                message.channel,
                "\U0001f6e0\ufe0f Detected repeated errors. Auto-repair has been triggered.\n\n"
                + report,
                fallback="\U0001f6e0\ufe0f Auto-repair triggered. Check logs for details.",
            )
            session.metadata["_discord_error_streak"] = 0
        finally:
            session.metadata["_discord_auto_repair_running"] = False

    def _is_user_allowed(self, user_id: int) -> bool:
        """Check if the user is in the whitelist (if configured)."""
        allowed = self.config.adapters.discord.allowed_user_ids
        if not allowed:
            return True  # Empty list = allow all
        return user_id in allowed

    def _is_channel_allowed(self, channel_id: int) -> bool:
        """Check if the channel is in the whitelist (if configured)."""
        allowed = self.config.adapters.discord.allowed_channel_ids
        if not allowed:
            return True  # Empty list = allow all
        return channel_id in allowed

    def _session_key(self, channel_id: int, user_id: int) -> str:
        """Generate a session key from channel + user IDs."""
        return f"{channel_id}:{user_id}"

    async def _handle_command(self, message, cmd_text: str) -> None:
        """Handle prefix commands (!help, !model, etc.)."""
        import discord

        parts = cmd_text.strip().split(None, 1)
        cmd = parts[0].lower() if parts else ""
        args_text = parts[1] if len(parts) > 1 else ""

        session_key = self._session_key(message.channel.id, message.author.id)
        session = self.get_or_create_session(session_key)
        session.metadata["_discord_channel_id"] = message.channel.id
        session.metadata["_discord_user_id"] = message.author.id
        # Keep approval routing fresh for command-driven flows (e.g. !delegate).
        self._approval_cb.register_channel(session.id, message.channel.id)

        if cmd == "help":
            prefix = self.config.adapters.discord.command_prefix
            await message.channel.send(
                f"\U0001f3d4 **Kuro AI Assistant**\n\n"
                f"**Commands:**\n"
                f"`{prefix}help` — Show this help\n"
                f"`{prefix}model` — Show current model\n"
                f"`{prefix}model <name>` — Switch AI model\n"
                f"`{prefix}model oauth:<openai/model>` — Switch OpenAI model via OAuth\n"
                f"`{prefix}models` — List available models\n"
                f"`{prefix}agents` — List available sub-agents\n"
                f"`{prefix}delegate <agent> <task>` — Delegate task to a sub-agent\n"
                f"`{prefix}stats` — Dashboard overview\n"
                f"`{prefix}costs` — Token usage & cost breakdown\n"
                f"`{prefix}security` — Security report\n"
                f"`{prefix}fix [scope]` — Force self-repair (scope: full/errors/performance/config)\n"
                f"`{prefix}clear` — Clear conversation history\n"
                f"`{prefix}trust` — Show/set trust level\n\n"
                f"**Usage:**\n"
                f"- In DMs: just type your message\n"
                f"- In servers: mention me or use commands"
            )

        elif cmd == "model":
            if args_text:
                model_name, auth_mode = self._parse_model_selection(args_text)
                if auth_mode == "oauth":
                    if not model_name or not model_name.startswith("openai/"):
                        await message.channel.send(
                            "\u274c OAuth mode only supports `openai/...` models."
                        )
                        return
                    if not is_codex_oauth_model_supported(model_name):
                        await message.channel.send(
                            f"\u274c `{model_name}` is not supported in OpenAI OAuth mode."
                        )
                        return
                    if not self._oauth:
                        await message.channel.send(
                            "\u274c OpenAI OAuth support is unavailable in this runtime."
                        )
                        return
                    oauth_status = self._oauth.get_status(None)
                    if not oauth_status.get("logged_in"):
                        await message.channel.send(
                            "\u274c OpenAI OAuth is not available for Discord right now. "
                            "Run `codex login` on this machine first."
                        )
                        return
                session.metadata["model_override"] = model_name
                session.metadata["model_auth_mode"] = auth_mode
                logger.info(
                    "discord_model_override_set",
                    user_id=message.author.id,
                    channel_id=message.channel.id,
                    model=model_name,
                    mode=auth_mode,
                    session_id=session.id,
                )
                await message.channel.send(
                    f"\u2705 Model switched to: `{model_name}` ({auth_mode.upper()})"
                )
            else:
                current = session.metadata.get(
                    "model_override", self.config.models.default
                )
                mode = str(session.metadata.get("model_auth_mode", "api")).strip().lower()
                if not (isinstance(current, str) and current.startswith("openai/")):
                    mode = "api"
                await message.channel.send(
                    f"\U0001f916 Current model: `{current}` ({mode.upper()})"
                )

        elif cmd == "models":
            try:
                groups = await self.effective_engine.model.list_models_grouped()
                oauth_status = self._oauth.get_status(None) if self._oauth else {}
                if oauth_status.get("logged_in"):
                    oauth_models: list[str] = list(OPENAI_CODEX_OAUTH_MODELS)
                    extra_oauth = [
                        m.strip()
                        for m in os.environ.get("OPENAI_CODEX_OAUTH_MODELS", "").split(",")
                        if m.strip()
                    ]
                    for m in extra_oauth:
                        candidate = normalize_openai_model(m)
                        if (
                            is_codex_oauth_model_supported(candidate)
                            and candidate not in oauth_models
                        ):
                            oauth_models.append(candidate)
                    groups["openai-oauth"] = oauth_models
                if not groups:
                    await message.channel.send("No models available.")
                else:
                    lines = ["**Available models:**"]
                    active_model = session.metadata.get(
                        "model_override", self.config.models.default
                    )
                    active_mode = str(
                        session.metadata.get("model_auth_mode", "api")
                    ).strip().lower()
                    if not (isinstance(active_model, str) and active_model.startswith("openai/")):
                        active_mode = "api"
                    for provider, models in groups.items():
                        title = provider.capitalize()
                        if provider == "openai-compatible":
                            title = "OpenAI-Compatible (Local)"
                        elif provider == "openai-oauth":
                            title = "OpenAI (OAuth Subscription)"
                        lines.append(f"\n**{title}:**")
                        for m in models:
                            display = f"oauth:{m}" if provider == "openai-oauth" else m
                            is_active = (
                                m == active_model
                                and (
                                    (provider == "openai-oauth" and active_mode == "oauth")
                                    or (provider != "openai-oauth" and active_mode != "oauth")
                                )
                            )
                            marker = " \u2705" if is_active else ""
                            lines.append(f"  `{display}`{marker}")
                    await message.channel.send("\n".join(lines))
            except Exception as e:
                await message.channel.send(f"\u274c Error listing models: {str(e)[:200]}")

        elif cmd == "agents":
            agent_manager = getattr(self.effective_engine, "agent_manager", None)
            if agent_manager is None:
                await message.channel.send(
                    "\u274c Agent system is disabled. Enable it in config.yaml:\n"
                    "```yaml\nagents:\n  enabled: true\n```"
                )
                return

            definitions = agent_manager.list_definitions()
            if not definitions:
                await message.channel.send(
                    "\U0001f916 No agents registered. "
                    "Add agents in config.yaml under `agents.predefined`."
                )
                return

            lines = ["\U0001f916 **Available Sub-Agents:**\n"]
            for defn in definitions:
                tools_info = ""
                if defn.allowed_tools:
                    tools_info = f"\n    Tools: {', '.join(defn.allowed_tools)}"
                lines.append(
                    f"  **{defn.name}**\n"
                    f"    Model: `{defn.model}`\n"
                    f"    Rounds: {defn.max_tool_rounds}"
                    f"{tools_info}"
                )
            running = agent_manager.running_count
            if running:
                lines.append(f"\n\u26a1 Running: {running}")

            prefix = self.config.adapters.discord.command_prefix
            lines.append(
                f"\n**Usage:** `{prefix}delegate <agent_name> <task>`"
            )
            await message.channel.send("\n".join(lines))

        elif cmd == "delegate":
            # Parse: !delegate <agent_name> <task>
            delegate_parts = args_text.strip().split(None, 1)
            if len(delegate_parts) < 2:
                prefix = self.config.adapters.discord.command_prefix
                await message.channel.send(
                    f"\u274c Usage: `{prefix}delegate <agent_name> <task>`\n"
                    f"Example: `{prefix}delegate fast Summarize today's news`"
                )
                return

            agent_name = delegate_parts[0]
            task = delegate_parts[1]

            agent_manager = getattr(self.effective_engine, "agent_manager", None)
            if agent_manager is None:
                await message.channel.send("\u274c Agent system is not available.")
                return

            defn = agent_manager.get_definition(agent_name)
            if defn is None:
                prefix = self.config.adapters.discord.command_prefix
                available = [d.name for d in agent_manager.list_definitions()]
                await message.channel.send(
                    f"\u274c Agent `{agent_name}` not found.\n"
                    f"Available: {', '.join(available) if available else 'none'}\n"
                    f"Use `{prefix}agents` to see all agents."
                )
                return

            # Send typing indicator and run agent
            async with message.channel.typing():
                await message.channel.send(
                    f"\u26a1 Delegating to **{agent_name}** "
                    f"(model: `{defn.model}`)..."
                )
                try:
                    result = await agent_manager.delegate(
                        agent_name,
                        task,
                        parent_session=session,
                    )

                    # Send result (split if needed)
                    max_len = self.config.adapters.discord.max_message_length
                    header = f"\U0001f4e8 **{agent_name}** responded:\n\n"
                    await self._send_chunked_message(
                        message.channel,
                        header + result,
                        max_len=max_len,
                        fallback="\u26a0 Agent returned empty content.",
                    )
                except Exception as e:
                    logger.error(
                        "discord_delegate_error",
                        agent=agent_name,
                        error=str(e),
                    )
                    await message.channel.send(
                        f"\u274c Agent `{agent_name}` failed: {str(e)[:200]}"
                    )

        elif cmd in ("stats", "costs", "security", "diagnose"):
            from src.adapters.dashboard_commands import (
                handle_costs_command,
                handle_diagnose_command,
                handle_security_command,
                handle_stats_command,
            )

            max_len = self.config.adapters.discord.max_message_length - 100
            if cmd == "stats":
                result = await handle_stats_command(max_len)
            elif cmd == "costs":
                result = await handle_costs_command(max_len)
            elif cmd == "diagnose":
                result = await handle_diagnose_command(max_len)
            else:
                result = await handle_security_command(max_len)

            chunks = split_message(result, self.config.adapters.discord.max_message_length)
            for chunk in chunks:
                if chunk and chunk.strip():
                    await message.channel.send(f"```\n{chunk}\n```")

        elif cmd in ("fix", "repair"):
            scope = (
                args_text.strip().split(None, 1)[0]
                if args_text.strip()
                else "full"
            )
            await message.channel.send(
                f"\U0001f6e0\ufe0f Running self-repair (scope: `{scope}`)..."
            )
            report = await self._run_diagnose_and_repair(
                session,
                scope=scope,
                auto_fix=True,
            )
            await self._send_chunked_message(
                message.channel,
                report,
                fallback="\u26a0 Self-repair returned empty content.",
            )

        elif cmd == "clear":
            self.clear_session(session_key)
            await message.channel.send(
                "\U0001f5d1 Conversation cleared. Starting fresh!"
            )

        elif cmd == "trust":
            args_text = args_text.strip().lower()
            if args_text in ("low", "medium", "high", "critical"):
                session.trust_level = args_text
                level_map = {
                    "low": RiskLevel.LOW, "medium": RiskLevel.MEDIUM,
                    "high": RiskLevel.HIGH, "critical": RiskLevel.CRITICAL,
                }
                self.effective_engine.approval_policy.elevate_session_trust(
                    session.id, level_map[args_text]
                )
                await message.channel.send(
                    f"\U0001f513 Trust level set to: **{args_text.upper()}**"
                )
            else:
                await message.channel.send(
                    f"\U0001f512 Current trust level: **{session.trust_level.upper()}**\n"
                    f"Usage: `{self.config.adapters.discord.command_prefix}trust low|medium|high|critical`"
                )

        else:
            prefix = self.config.adapters.discord.command_prefix
            await message.channel.send(
                f"Unknown command: `{cmd}`. Type `{prefix}help` for available commands."
            )

    async def _handle_message(
        self, message, content: str, *, image_urls: list[str] | None = None,
    ) -> None:
        """Process a regular chat message through the engine."""
        session_key = self._session_key(message.channel.id, message.author.id)
        session = self.get_or_create_session(session_key)
        session.metadata["_discord_channel_id"] = message.channel.id
        session.metadata["_discord_user_id"] = message.author.id

        # Register channel for approval callbacks
        self._approval_cb.register_channel(session.id, message.channel.id)
        selected_model = session.metadata.get("model_override") or self.config.models.default
        selected_mode = str(session.metadata.get("model_auth_mode", "api")).strip().lower()
        effective_mode = (
            selected_mode
            if isinstance(selected_model, str) and selected_model.startswith("openai/")
            else "api"
        )

        logger.info(
            "discord_message",
            user_id=message.author.id,
            username=str(message.author),
            channel_id=message.channel.id,
            model=selected_model,
            model_auth_mode=effective_mode,
            text_len=len(content),
            images=len(image_urls) if image_urls else 0,
        )

        # Download images to temp files for multimodal processing
        image_paths: list[str] = []
        if image_urls:
            image_paths = await self._download_images(image_urls)

        # Send typing indicator
        async with message.channel.typing():
            try:
                # Get model override if set
                model = session.metadata.get("model_override")
                session.metadata["model_auth_mode"] = selected_mode
                if model:
                    session.metadata["model_override"] = model

                # Cache OAuth context in session metadata for internal tool model calls
                # (e.g., diagnostics repair model) that happen in the same request.
                cached_provider_ctx = None
                if self._oauth:
                    auth_context = await self._oauth.get_auth_context(None)
                    if auth_context and auth_context.get("access_token"):
                        cached_provider_ctx = {
                            "mode": "codex_oauth",
                            "access_token": auth_context.get("access_token", ""),
                            "account_id": auth_context.get("account_id", ""),
                            "plan_type": auth_context.get("plan_type", ""),
                            "email": auth_context.get("email", ""),
                            "originator": "codex_cli_rs",
                        }
                if cached_provider_ctx:
                    session.metadata["_openai_oauth_provider_ctx"] = cached_provider_ctx
                else:
                    session.metadata.pop("_openai_oauth_provider_ctx", None)

                oauth_mode = (
                    effective_mode == "oauth"
                    and isinstance(model, str)
                    and model.startswith("openai/")
                )
                model_ctx = contextlib.nullcontext()
                if oauth_mode:
                    if not is_codex_oauth_model_supported(model or ""):
                        await message.channel.send(
                            f"\u274c OAuth model not supported: `{model}`"
                        )
                        return
                    if not self._oauth:
                        await message.channel.send(
                            "\u274c OpenAI OAuth support is unavailable in this runtime."
                        )
                        return
                    auth_context = await self._oauth.get_auth_context(None)
                    if not auth_context or not auth_context.get("access_token"):
                        await message.channel.send(
                            "\u274c OpenAI OAuth token missing. Run `codex login` first."
                        )
                        return
                    provider_ctx = cached_provider_ctx or {
                        "mode": "codex_oauth",
                        "access_token": auth_context.get("access_token", ""),
                        "account_id": auth_context.get("account_id", ""),
                        "plan_type": auth_context.get("plan_type", ""),
                        "email": auth_context.get("email", ""),
                        "originator": "codex_cli_rs",
                    }
                    target_engine = self.effective_engine
                    if hasattr(target_engine.model, "provider_auth_override"):
                        model_ctx = target_engine.model.provider_auth_override("openai", provider_ctx)
                    elif hasattr(target_engine.model, "provider_api_key_override"):
                        model_ctx = target_engine.model.provider_api_key_override(
                            "openai",
                            str(auth_context.get("access_token", "")),
                        )
                    logger.info(
                        "discord_oauth_model_request",
                        user_id=message.author.id,
                        channel_id=message.channel.id,
                        model=model,
                    )

                # Record generated image paths before processing
                images_before = set(self._collect_generated_images(session))
                legacy_before = self._collect_session_images(session)

                # Process through engine (routes to agent instance if bound)
                with model_ctx:
                    response = await self.process_incoming(
                        content, session, model=model,
                        images=image_paths if image_paths else None,
                    )

                # Collect any new images generated during processing (screenshots, etc.)
                new_images = [
                    p for p in self._collect_generated_images(session)
                    if p not in images_before
                ]
                # Backward-compatible fallback for older sessions/messages.
                if not new_images:
                    new_images = self._collect_new_images(session, legacy_before)

                # Send response (split if needed)
                max_len = self.config.adapters.discord.max_message_length
                response_text = response if isinstance(response, str) else str(response or "")
                empty_response = False
                if response_text.strip():
                    session.metadata["_discord_error_streak"] = 0
                else:
                    empty_response = True
                    logger.warning(
                        "discord_empty_model_response",
                        session_id=session.id,
                        user_id=message.author.id,
                        model=selected_model,
                    )
                    session.metadata["_discord_error_streak"] = int(
                        session.metadata.get("_discord_error_streak", 0) or 0
                    ) + 1
                    response_text = (
                        "\u26a0\ufe0f Model returned an empty response. "
                        "Please try again, or run `!fix errors` to trigger self-repair."
                    )

                await self._send_chunked_message(
                    message.channel,
                    response_text,
                    max_len=max_len,
                    fallback="\u26a0 Empty response suppressed.",
                )
                if empty_response:
                    await self._maybe_auto_repair(
                        message,
                        session,
                        trigger_error="empty_model_response",
                    )

                # Send generated images as attachments
                await self._send_images(message.channel, new_images)

            except Exception as e:
                session.metadata["_discord_error_streak"] = int(
                    session.metadata.get("_discord_error_streak", 0) or 0
                ) + 1
                session.metadata["_discord_last_error"] = str(e)
                logger.error(
                    "discord_process_error",
                    user_id=message.author.id,
                    error=str(e),
                )
                await message.channel.send(
                    f"\u274c Error processing message: {str(e)[:200]}"
                )
                await self._maybe_auto_repair(
                    message,
                    session,
                    trigger_error=str(e),
                )
        # Clean up downloaded temp images
        self._cleanup_temp_images(image_paths)

    # ── Image helpers ──────────────────────────────────────────

    async def _download_images(self, urls: list[str]) -> list[str]:
        """Download image URLs to temporary files.  Returns list of file paths."""
        import tempfile
        from pathlib import Path

        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp_not_available", hint="pip install aiohttp")
            return []

        paths: list[str] = []
        async with aiohttp.ClientSession() as http:
            for url in urls[:5]:  # limit to 5 images
                try:
                    async with http.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status != 200:
                            continue
                        ct = resp.content_type or ""
                        ext = ".png"
                        if "jpeg" in ct or "jpg" in ct:
                            ext = ".jpg"
                        elif "gif" in ct:
                            ext = ".gif"
                        elif "webp" in ct:
                            ext = ".webp"
                        data = await resp.read()
                        if len(data) > 20 * 1024 * 1024:  # skip >20 MB
                            continue
                        tmp = tempfile.NamedTemporaryFile(
                            suffix=ext, prefix="kuro_discord_", delete=False
                        )
                        tmp.write(data)
                        tmp.close()
                        paths.append(tmp.name)
                except Exception as e:
                    logger.debug("discord_image_download_failed", url=url[:80], error=str(e))
        return paths

    def _collect_generated_images(self, session: Any) -> list[str]:
        """Collect generated image file paths tracked in session metadata."""
        from pathlib import Path

        raw = session.metadata.get("generated_images", [])
        if not isinstance(raw, list):
            return []

        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            p = Path(item)
            if not p.is_file():
                continue
            path_str = str(p)
            if path_str in seen:
                continue
            seen.add(path_str)
            out.append(path_str)
        return out

    def _collect_session_images(self, session: Any) -> set[str]:
        """Collect image paths already present in session messages."""
        paths: set[str] = set()
        for msg in session.messages:
            if isinstance(msg.content, list):
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            paths.add(url[:60])  # use prefix as key
        return paths

    def _collect_new_images(self, session: Any, before: set[str]) -> list[str]:
        """Find image paths in tool results that were added during processing."""
        from pathlib import Path
        import re

        img_path_re = re.compile(
            r"([A-Za-z]:[\\/][^\n\r\"<>|:*?]+?\.(?:png|jpg|jpeg|gif|webp|bmp))",
            re.IGNORECASE,
        )
        new_images: list[str] = []
        for msg in session.messages:
            if msg.role.value == "tool" and isinstance(msg.content, list):
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        key = url[:60] if url.startswith("data:") else url
                        if key and key not in before:
                            # Try to find the original file path from the text part
                            for p2 in msg.content:
                                if isinstance(p2, dict) and p2.get("type") == "text":
                                    text = p2.get("text", "")
                                    for line in text.split("\n"):
                                        candidate = None
                                        if "saved:" in line.lower() or "path:" in line.lower():
                                            candidate = line.split(":", 1)[-1].strip()
                                        else:
                                            m = img_path_re.search(line)
                                            if m:
                                                candidate = m.group(1).strip()
                                        if candidate and Path(candidate).is_file():
                                            new_images.append(candidate)
                                            break
        return new_images

    async def _send_images(self, channel: Any, image_paths: list[str]) -> None:
        """Send image files as Discord attachments."""
        import discord
        from pathlib import Path

        for path in image_paths[:5]:  # limit to 5 images
            try:
                p = Path(path)
                if p.is_file() and p.stat().st_size < 8 * 1024 * 1024:  # Discord 8MB limit
                    await channel.send(file=discord.File(str(p), filename=p.name))
            except Exception as e:
                logger.debug("discord_send_image_failed", path=path, error=str(e))

    @staticmethod
    def _cleanup_temp_images(paths: list[str]) -> None:
        """Remove temporary downloaded image files."""
        import os
        for p in paths:
            try:
                if p and os.path.exists(p) and "kuro_discord_" in p:
                    os.unlink(p)
            except Exception:
                pass

    @property
    def default_channel_id(self) -> int | None:
        """Get the default notification channel ID (first allowed text channel)."""
        return self._default_channel_id

    async def send_notification(self, user_id: str, message: str) -> bool:
        """Send a proactive notification to a Discord channel.

        Args:
            user_id: The channel ID (as string) to send to.
            message: The notification message.

        Returns:
            True if sent successfully.
        """
        if self._bot is None or not self._bot.is_ready():
            logger.warning("discord_notify_bot_not_ready")
            return False

        try:
            # Handle session key format "channel_id:user_id" from older tasks
            raw_id = user_id.split(":")[0] if ":" in user_id else user_id
            channel_id = int(raw_id)
            channel = self._bot.get_channel(channel_id)
            if channel is None:
                channel = await self._bot.fetch_channel(channel_id)

            await self._send_chunked_message(
                channel,
                message,
                fallback="\u26a0 Notification content was empty.",
            )

            logger.info("discord_notification_sent", channel_id=channel_id)
            return True

        except Exception as e:
            logger.error("discord_notification_failed", error=str(e), user_id=user_id)
            return False


def split_message(text: str, max_len: int = 2000) -> list[str]:
    """Split a message into chunks respecting Discord's length limit.

    Splitting priority:
    1. By code block boundaries (``` markers)
    2. By paragraph (double newline)
    3. By line (single newline)
    4. By space (word boundary)
    5. By character (last resort)
    """
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Find the best split point
        split_at = max_len

        # Try paragraph break
        para_idx = remaining.rfind("\n\n", 0, max_len)
        if para_idx > max_len // 4:
            split_at = para_idx + 2  # Include the double newline
        else:
            # Try line break
            line_idx = remaining.rfind("\n", 0, max_len)
            if line_idx > max_len // 4:
                split_at = line_idx + 1
            else:
                # Try space (word boundary)
                space_idx = remaining.rfind(" ", 0, max_len)
                if space_idx > max_len // 4:
                    split_at = space_idx + 1

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    return [c for c in chunks if c]


def _format_params_discord(params: dict[str, Any]) -> str:
    """Format tool parameters for Discord display."""
    if not params:
        return "`(none)`"
    lines = []
    for k, v in params.items():
        val_str = str(v)
        if len(val_str) > 80:
            val_str = val_str[:80] + "..."
        lines.append(f"  `{k}`: {val_str}")
    return "\n".join(lines)
