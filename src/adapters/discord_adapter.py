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
from typing import Any
from uuid import uuid4

import structlog

from src.adapters.base import BaseAdapter
from src.config import KuroConfig
from src.core.engine import ApprovalCallback, Engine
from src.core.types import Session
from src.tools.base import RiskLevel

logger = structlog.get_logger()

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
    """

    name = "discord"

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        super().__init__(engine, config)

        self._bot = None
        self._approval_cb = DiscordApprovalCallback(
            approval_policy=engine.approval_policy,
        )
        self._approval_cb._timeout = config.adapters.discord.approval_timeout

        # Replace the engine's approval callback with Discord's
        self.engine.approval_cb = self._approval_cb

    async def start(self) -> None:
        """Initialize the Discord bot and start it."""
        token = self.config.adapters.discord.get_bot_token()
        if not token:
            env_var = self.config.adapters.discord.bot_token_env
            raise RuntimeError(
                f"Discord bot token not found. "
                f"Set the {env_var} environment variable."
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

        # Register event handlers
        @self._bot.event
        async def on_ready():
            logger.info(
                "discord_started",
                bot_user=str(self._bot.user),
                bot_id=self._bot.user.id if self._bot.user else None,
                guild_count=len(self._bot.guilds),
            )

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
            if not content:
                return

            # Handle prefix commands
            if content.startswith(prefix):
                await self._handle_command(message, content[len(prefix):])
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

            await self._handle_message(message, content)

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

        if cmd == "help":
            prefix = self.config.adapters.discord.command_prefix
            await message.channel.send(
                f"\U0001f3d4 **Kuro AI Assistant**\n\n"
                f"**Commands:**\n"
                f"`{prefix}help` — Show this help\n"
                f"`{prefix}model` — Show current model\n"
                f"`{prefix}model <name>` — Switch AI model\n"
                f"`{prefix}models` — List available models\n"
                f"`{prefix}agents` — List available sub-agents\n"
                f"`{prefix}delegate <agent> <task>` — Delegate task to a sub-agent\n"
                f"`{prefix}clear` — Clear conversation history\n"
                f"`{prefix}trust` — Show/set trust level\n\n"
                f"**Usage:**\n"
                f"- In DMs: just type your message\n"
                f"- In servers: mention me or use commands"
            )

        elif cmd == "model":
            if args_text:
                model_name = args_text.strip()
                session.metadata["model_override"] = model_name
                await message.channel.send(
                    f"\u2705 Model switched to: `{model_name}`"
                )
            else:
                current = session.metadata.get(
                    "model_override", self.config.models.default
                )
                await message.channel.send(
                    f"\U0001f916 Current model: `{current}`"
                )

        elif cmd == "models":
            try:
                groups = await self.engine.model.list_models_grouped()
                if not groups:
                    await message.channel.send("No models available.")
                else:
                    lines = ["**Available models:**"]
                    for provider, models in groups.items():
                        lines.append(f"\n**{provider.capitalize()}:**")
                        active_model = session.metadata.get(
                            "model_override", self.config.models.default
                        )
                        for m in models:
                            marker = " \u2705" if m == active_model else ""
                            lines.append(f"  `{m}`{marker}")
                    await message.channel.send("\n".join(lines))
            except Exception as e:
                await message.channel.send(f"\u274c Error listing models: {str(e)[:200]}")

        elif cmd == "agents":
            agent_manager = getattr(self.engine, "agent_manager", None)
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

            agent_manager = getattr(self.engine, "agent_manager", None)
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

            # Register channel for approval callbacks
            self._approval_cb.register_channel(session.id, message.channel.id)

            # Send typing indicator and run agent
            async with message.channel.typing():
                await message.channel.send(
                    f"\u26a1 Delegating to **{agent_name}** "
                    f"(model: `{defn.model}`)..."
                )
                try:
                    result = await agent_manager.delegate(agent_name, task)

                    # Send result (split if needed)
                    max_len = self.config.adapters.discord.max_message_length
                    header = f"\U0001f4e8 **{agent_name}** responded:\n\n"
                    chunks = split_message(header + result, max_len)
                    for chunk in chunks:
                        await message.channel.send(chunk)
                except Exception as e:
                    logger.error(
                        "discord_delegate_error",
                        agent=agent_name,
                        error=str(e),
                    )
                    await message.channel.send(
                        f"\u274c Agent `{agent_name}` failed: {str(e)[:200]}"
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
                self.engine.approval_policy.elevate_session_trust(
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

    async def _handle_message(self, message, content: str) -> None:
        """Process a regular chat message through the engine."""
        session_key = self._session_key(message.channel.id, message.author.id)
        session = self.get_or_create_session(session_key)

        # Register channel for approval callbacks
        self._approval_cb.register_channel(session.id, message.channel.id)

        logger.info(
            "discord_message",
            user_id=message.author.id,
            username=str(message.author),
            channel_id=message.channel.id,
            text_len=len(content),
        )

        # Send typing indicator
        async with message.channel.typing():
            try:
                # Get model override if set
                model = session.metadata.get("model_override")

                # Process through engine
                response = await self.engine.process_message(
                    content, session, model=model
                )

                # Send response (split if needed)
                max_len = self.config.adapters.discord.max_message_length
                chunks = split_message(response, max_len)
                for chunk in chunks:
                    await message.channel.send(chunk)

            except Exception as e:
                logger.error(
                    "discord_process_error",
                    user_id=message.author.id,
                    error=str(e),
                )
                await message.channel.send(
                    f"\u274c Error processing message: {str(e)[:200]}"
                )


def split_message(text: str, max_len: int = 2000) -> list[str]:
    """Split a message into chunks respecting Discord's length limit.

    Splitting priority:
    1. By code block boundaries (``` markers)
    2. By paragraph (double newline)
    3. By line (single newline)
    4. By space (word boundary)
    5. By character (last resort)
    """
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

    return chunks


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
