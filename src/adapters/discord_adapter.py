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
from src.core.agents import AgentRunner
from src.core.engine import ApprovalCallback, Engine
from src.core.security.approval import ApprovalPolicy
from src.core.types import AgentDefinition, Session
from src.openai_catalog import (
    OPENAI_CODEX_OAUTH_MODELS,
    OPENAI_OFFICIAL_MODELS,
    is_codex_oauth_model_supported,
    is_openai_compatible_local_base_url,
    normalize_openai_model,
)
from src.tools.base import RiskLevel, ToolContext

logger = structlog.get_logger()
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FIX_SCOPES = {"full", "errors", "performance", "config"}
_COMPUTER_OPERATION_TOOL_NAMES = {
    "computer_use",
    "mouse_action",
    "keyboard_action",
    "screen_info",
    "screenshot",
}
_WEB_OPERATION_TOOL_NAMES = {
    "web_navigate",
    "web_click",
    "web_type",
    "web_get_text",
    "web_screenshot",
}

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
    Session key strategy:
    - DM: f"{channel_id}:{user_id}" (per-user personal memory)
    - Guild text channel: f"{channel_id}:shared" (shared channel memory)

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
        self._default_channel_id: int | None = None
        self._default_channel_name: str | None = None
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

    def _is_local_model_target(self, model_name: str | None) -> bool:
        """Return True when model points to a local runtime endpoint."""
        target = str(model_name or "").strip()
        if not target:
            return False
        provider = target.split("/", 1)[0].strip().lower()
        if provider in {"ollama", "llama"}:
            return True
        if provider == "openai":
            cfg = self.config.models.providers.get("openai")
            base_url = str(getattr(cfg, "base_url", "") or "").strip()
            return is_openai_compatible_local_base_url(base_url)
        return False

    def _get_oauth_status(self) -> dict[str, Any]:
        """Get OAuth status with Discord-friendly fallback to codex auth file."""
        if not self._oauth:
            return {"configured": False, "logged_in": False}

        try:
            status = self._oauth.get_status(None)
        except Exception:
            status = {"configured": True, "logged_in": False}

        if status.get("logged_in"):
            return status

        # Discord adapter has no browser cookie session; fallback to ~/.codex/auth.json.
        loader = getattr(self._oauth, "_load_from_codex_auth_file", None)
        if callable(loader):
            try:
                sess = loader()
            except Exception:
                sess = None
            if sess and getattr(sess, "access_token", ""):
                return {
                    "configured": True,
                    "logged_in": True,
                    "scope": getattr(sess, "scope", ""),
                    "email": getattr(sess, "email", None),
                    "account_id": getattr(sess, "account_id", None),
                    "plan_type": getattr(sess, "plan_type", None),
                }

        return status

    async def _get_oauth_auth_context(self) -> dict[str, str] | None:
        """Get OAuth auth context for model requests, with codex-file fallback."""
        if not self._oauth:
            return None

        try:
            auth_context = await self._oauth.get_auth_context(None)
        except Exception:
            auth_context = None
        if auth_context and auth_context.get("access_token"):
            return auth_context

        loader = getattr(self._oauth, "_load_from_codex_auth_file", None)
        if callable(loader):
            try:
                sess = loader()
            except Exception:
                sess = None
            if sess and getattr(sess, "access_token", ""):
                return {
                    "access_token": str(getattr(sess, "access_token", "") or ""),
                    "account_id": str(getattr(sess, "account_id", "") or ""),
                    "plan_type": str(getattr(sess, "plan_type", "") or ""),
                    "email": str(getattr(sess, "email", "") or ""),
                }

        return None

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
                        self._default_channel_name = str(getattr(channel, "name", "") or "")
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
                    self._handle_message(
                        message,
                        content,
                        image_urls=image_urls,
                        is_dm=is_dm,
                    ),
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

    @staticmethod
    def _parse_fix_command_args(args_text: str) -> tuple[str, str]:
        """Parse !fix arguments: !fix [scope] [description]."""
        raw = (args_text or "").strip()
        if not raw:
            return "full", ""

        parts = raw.split(None, 1)
        head = parts[0].strip().lower()
        if head in _FIX_SCOPES:
            detail = parts[1].strip() if len(parts) > 1 else ""
            return head, detail

        return "full", raw

    def _resolve_fix_agent_model(self) -> str:
        """Pick model for the dedicated fix agent."""
        diag = getattr(self.config, "diagnostics", None)
        repair_model = str(getattr(diag, "repair_model", "main") or "main").strip()
        if repair_model.lower().startswith("oauth:"):
            repair_model = repair_model.split(":", 1)[1].strip()
        elif repair_model.lower().startswith("api:"):
            repair_model = repair_model.split(":", 1)[1].strip()
        if repair_model and repair_model.lower() != "main":
            return repair_model
        default_model = str(self.config.models.default or "").strip()
        if default_model.lower().startswith("oauth:"):
            default_model = default_model.split(":", 1)[1].strip()
        elif default_model.lower().startswith("api:"):
            default_model = default_model.split(":", 1)[1].strip()
        return default_model or "gemini/gemini-3-flash-preview"

    @staticmethod
    def _build_fix_agent_task(scope: str, detail: str) -> str:
        lines = [
            f"Run a deep repair workflow with scope '{scope}'.",
            "Inspect errors/logs and root cause first, then apply fixes directly when safe.",
            "Use tools freely (including file edits, shell, diagnostics, and restart if needed).",
            "After fixing, validate with concrete checks/tests and report exact changes.",
            "Final report must include: root cause, actions taken, verification results, and remaining risks.",
        ]
        detail_text = detail.strip()
        if detail_text:
            lines.append(f"User-provided repair request: {detail_text}")
        return "\n".join(lines)

    async def _run_fix_agent(
        self,
        session: Session,
        *,
        scope: str,
        detail: str,
    ) -> str:
        """Run a dedicated high-privilege fix agent for manual !fix command."""
        target_engine = self.effective_engine
        fix_model = self._resolve_fix_agent_model()
        task = self._build_fix_agent_task(scope, detail)

        # Isolated security override for this run only.
        override_cfg = self.config.model_copy(deep=True)
        override_cfg.security.full_access_mode = True
        override_cfg.security.max_risk_level = "critical"
        override_cfg.security.disabled_tools = []
        override_policy = ApprovalPolicy(override_cfg.security)

        defn = AgentDefinition(
            name=f"fix_agent_{uuid4().hex[:8]}",
            model=fix_model,
            system_prompt=(
                "You are Kuro Fix Agent, a dedicated repair specialist. "
                "Act decisively, prioritize restoring service stability, and "
                "make concrete fixes instead of only giving advice."
            ),
            max_tool_rounds=8,
            temperature=0.2,
            complexity_tier="expert",
            created_by="runtime",
            inherit_context=True,
            max_depth=1,
        )

        logger.info(
            "discord_fix_agent_start",
            session_id=session.id,
            model=fix_model,
            scope=scope,
            has_detail=bool(detail.strip()),
        )

        try:
            runner = AgentRunner(
                definition=defn,
                model_router=target_engine.model,
                tool_system=target_engine.tools,
                config=override_cfg,
                approval_policy=override_policy,
                approval_callback=self._approval_cb,
                audit_log=target_engine.audit,
                parent_session=session,
                parent_context=list(session.messages),
                depth=0,
                agent_manager=getattr(target_engine, "agent_manager", None),
            )
            result = await runner.run(task)
            text = result if isinstance(result, str) else str(result)
            clean = (text or "").strip()
            if clean:
                return clean
            return "\u26a0 Fix agent finished but returned empty content."
        except Exception as e:
            logger.error(
                "discord_fix_agent_failed",
                session_id=session.id,
                model=fix_model,
                scope=scope,
                error=str(e),
            )
            return f"\u274c Fix agent failed: {str(e)[:300]}"

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

    def _session_key(self, channel_id: int, user_id: int, *, is_dm: bool) -> str:
        """Generate a session key.

        DM keeps user-isolated memory; guild channels use shared memory.
        """
        if is_dm:
            return f"{channel_id}:{user_id}"
        return f"{channel_id}:shared"

    async def _handle_command(self, message, cmd_text: str) -> None:
        """Handle prefix commands (!help, !model, etc.)."""
        import discord

        parts = cmd_text.strip().split(None, 1)
        cmd = parts[0].lower() if parts else ""
        args_text = parts[1] if len(parts) > 1 else ""

        is_dm = isinstance(message.channel, discord.DMChannel)
        session_key = self._session_key(
            message.channel.id,
            message.author.id,
            is_dm=is_dm,
        )
        session = self.get_or_create_session(session_key)
        session.metadata["_discord_channel_id"] = message.channel.id
        session.metadata["_discord_user_id"] = message.author.id
        session.metadata["_discord_channel_name"] = str(getattr(message.channel, "name", "") or "")
        session.metadata["_discord_username"] = str(message.author)
        session.metadata["username"] = str(message.author)
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
                f"`{prefix}skills` — List all skills and status\n"
                f"`{prefix}skill <name>` — Toggle one skill on/off\n"
                f"`{prefix}agents` — List available sub-agents\n"
                f"`{prefix}delegate <agent> <task>` — Delegate task to a sub-agent\n"
                f"`{prefix}stats` — Dashboard overview\n"
                f"`{prefix}costs` — Token usage & cost breakdown\n"
                f"`{prefix}security` — Security report\n"
                f"`{prefix}fix [scope] [issue/goal]` - Run dedicated fix agent (temporary full access)\n"
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
                    oauth_status = self._get_oauth_status()
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
                oauth_status = self._get_oauth_status()
                if self._oauth:
                    oauth_models: list[str] = list(OPENAI_CODEX_OAUTH_MODELS)
                    for m in OPENAI_OFFICIAL_MODELS:
                        candidate = normalize_openai_model(m)
                        if (
                            is_codex_oauth_model_supported(candidate)
                            and candidate not in oauth_models
                        ):
                            oauth_models.append(candidate)
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
                    await message.channel.send("目前沒有可用模型。")
                else:
                    lines = ["**可用模型列表：**"]
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
                            title = "OpenAI 相容（本地）"
                        elif provider == "openai-oauth":
                            title = "OpenAI（OAuth 訂閱）"
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
                    if self._oauth and not oauth_status.get("logged_in"):
                        lines.append(
                            "\nOAuth 提示：使用 `oauth:openai/...` 模型前，"
                            "請先在此機器執行 `codex login`。"
                        )
                    await message.channel.send("\n".join(lines))
            except Exception as e:
                await message.channel.send(f"\u274c Error listing models: {str(e)[:200]}")

        elif cmd == "skills":
            sm = getattr(self.effective_engine, "skills", None)
            prefix = self.config.adapters.discord.command_prefix
            if sm is None:
                await message.channel.send("\u274c Skills system is unavailable.")
                return

            sub = args_text.strip()
            sub_lower = sub.lower()

            if sub_lower == "available":
                available = sm.list_available_skills()
                if not available:
                    await message.channel.send(
                        "\U0001f4e6 No installable skills found in the built-in catalog."
                    )
                    return
                lines = ["\U0001f4e6 **Installable Skills:**"]
                for item in sorted(
                    available,
                    key=lambda it: str(it.get("name", "")).lower(),
                ):
                    name = str(item.get("name", "")).strip()
                    if not name:
                        continue
                    desc = str(item.get("description", "")).strip() or "No description"
                    installed = bool(item.get("installed"))
                    state = "installed" if installed else "not installed"
                    lines.append(f"- `{name}` — {desc} ({state})")
                lines.append(
                    f"\nInstall with `{prefix}skills install <name>`"
                )
                await self._send_chunked_message(
                    message.channel,
                    "\n".join(lines),
                )
                return

            if sub_lower.startswith("install "):
                skill_name = sub[8:].strip()
                if not skill_name:
                    await message.channel.send(
                        f"\u274c Usage: `{prefix}skills install <name>`"
                    )
                    return
                installed_path = sm.install_skill(skill_name)
                if not installed_path:
                    await message.channel.send(
                        f"\u274c Failed to install `{skill_name}`. "
                        f"Use `{prefix}skills available` to see valid names."
                    )
                    return
                for skill in sm.list_skills():
                    sm.activate(skill.name)
                await message.channel.send(
                    f"\u2705 Installed `{skill_name}` and enabled all skills by default."
                )
                return

            skills = sorted(sm.list_skills(), key=lambda s: s.name.lower())
            if not skills:
                await message.channel.send(
                    "\U0001f9e9 No skills found. Put SKILL.md under `~/.kuro/skills/<name>/`."
                )
                return

            active = getattr(sm, "_active", set())
            lines = ["\U0001f9e9 **Skills:**"]
            for skill in skills:
                status = "\U0001f7e2 ON" if skill.name in active else "\u26aa OFF"
                desc = skill.description or "No description"
                lines.append(f"- {status} `{skill.name}` — {desc}")
            lines.append("")
            lines.append(f"Toggle: `{prefix}skill <name>`")
            lines.append(f"Enable: `{prefix}skill on <name>`")
            lines.append(f"Disable: `{prefix}skill off <name>`")
            lines.append(f"All ON/OFF: `{prefix}skill all on|off`")
            lines.append(f"Catalog: `{prefix}skills available`")
            lines.append(f"Install: `{prefix}skills install <name>`")
            await self._send_chunked_message(
                message.channel,
                "\n".join(lines),
            )

        elif cmd == "skill":
            sm = getattr(self.effective_engine, "skills", None)
            prefix = self.config.adapters.discord.command_prefix
            if sm is None:
                await message.channel.send("\u274c Skills system is unavailable.")
                return

            raw = args_text.strip()
            if not raw:
                await message.channel.send(
                    f"\u274c Usage: `{prefix}skill <name>` or `{prefix}skill on|off <name>`"
                )
                return

            skills = sm.list_skills()
            if not skills:
                await message.channel.send(
                    f"\U0001f9e9 No skills found. Use `{prefix}skills` to verify catalog loading."
                )
                return

            raw_lower = raw.lower()
            if raw_lower in {"all on", "on all", "all off", "off all"}:
                enable = "on" in raw_lower.split()
                changed = 0
                for skill in skills:
                    ok = sm.activate(skill.name) if enable else sm.deactivate(skill.name)
                    if ok:
                        changed += 1
                state = "ON" if enable else "OFF"
                await message.channel.send(
                    f"\u2705 Set all skills to **{state}** ({changed}/{len(skills)} changed)."
                )
                return

            action = "toggle"
            skill_name = raw
            parts = raw.split(None, 1)
            if len(parts) == 2 and parts[0].lower() in {"on", "off"}:
                action = parts[0].lower()
                skill_name = parts[1].strip()

            if not skill_name:
                await message.channel.send(
                    f"\u274c Usage: `{prefix}skill <name>` or `{prefix}skill on|off <name>`"
                )
                return

            target_name = skill_name
            target_skill = sm.get_skill(target_name)
            if target_skill is None:
                for skill in skills:
                    if skill.name.lower() == skill_name.lower():
                        target_name = skill.name
                        target_skill = skill
                        break
            if target_skill is None:
                await message.channel.send(
                    f"\u274c Skill `{skill_name}` not found. Use `{prefix}skills` to list loaded skills."
                )
                return

            active = getattr(sm, "_active", set())
            if action == "on":
                sm.activate(target_name)
            elif action == "off":
                sm.deactivate(target_name)
            else:
                if target_name in active:
                    sm.deactivate(target_name)
                else:
                    sm.activate(target_name)

            active_now = target_name in getattr(sm, "_active", set())
            state = "ON" if active_now else "OFF"
            await message.channel.send(
                f"\U0001f9e9 Skill `{target_name}` is now **{state}**."
            )

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
            scope, detail = self._parse_fix_command_args(args_text)
            detail_preview = detail.strip()
            if len(detail_preview) > 180:
                detail_preview = detail_preview[:177] + "..."

            status = (
                f"\U0001f6e0\ufe0f Launching dedicated fix agent "
                f"(scope: `{scope}`, temporary full-access mode)..."
            )
            if detail_preview:
                status += f"\nIssue: {detail_preview}"
            await message.channel.send(status)

            report = await self._run_fix_agent(
                session,
                scope=scope,
                detail=detail,
            )
            await self._send_chunked_message(
                message.channel,
                report,
                fallback="\u26a0 Fix agent returned empty content.",
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
        self,
        message,
        content: str,
        *,
        image_urls: list[str] | None = None,
        is_dm: bool = False,
    ) -> None:
        """Process a regular chat message through the engine."""
        session_key = self._session_key(
            message.channel.id,
            message.author.id,
            is_dm=is_dm,
        )
        session = self.get_or_create_session(session_key)
        session.metadata["_discord_channel_id"] = message.channel.id
        session.metadata["_discord_user_id"] = message.author.id
        session.metadata["_discord_channel_name"] = str(getattr(message.channel, "name", "") or "")
        session.metadata["_discord_username"] = str(message.author)
        session.metadata["username"] = str(message.author)

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

        # Build image payloads for model input:
        # - cloud models: keep remote URLs to avoid huge base64 token overhead
        # - local models: download to local temp files for reliable access
        image_inputs: list[str] = []
        temp_image_paths: list[str] = []
        if image_urls:
            is_local_target = (
                effective_mode != "oauth"
                and self._is_local_model_target(str(selected_model or ""))
            )
            if is_local_target:
                temp_image_paths = await self._download_images(image_urls)
                image_inputs = temp_image_paths
            else:
                image_inputs = list(image_urls[:5])  # same cap as downloader

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
                    auth_context = await self._get_oauth_auth_context()
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
                    auth_context = await self._get_oauth_auth_context()
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
                message_count_before = len(session.messages)

                # Process through engine (routes to agent instance if bound)
                with model_ctx:
                    response = await self.process_incoming(
                        content, session, model=model,
                        images=image_inputs if image_inputs else None,
                    )

                # Collect any new images generated during processing (screenshots, etc.)
                new_images = [
                    p for p in self._collect_generated_images(session)
                    if p not in images_before
                ]
                # Backward-compatible fallback for older sessions/messages.
                if not new_images:
                    new_images = self._collect_new_images(session, legacy_before)
                recent_tool_names = self._collect_recent_tool_names(
                    session,
                    start_index=message_count_before,
                )
                used_visual_tools = any(
                    (name in _COMPUTER_OPERATION_TOOL_NAMES) or (name in _WEB_OPERATION_TOOL_NAMES)
                    for name in recent_tool_names
                )
                if used_visual_tools and not new_images:
                    auto_capture = await self._capture_post_action_screenshot(
                        session,
                        recent_tool_names=recent_tool_names,
                    )
                    if auto_capture and auto_capture not in new_images:
                        new_images.append(auto_capture)

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
        self._cleanup_temp_images(temp_image_paths)

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

        try:
            from src.core.security.egress import EgressBroker
            egress = EgressBroker(getattr(self.config, "egress_policy", None))
        except Exception:
            egress = None

        paths: list[str] = []
        async with aiohttp.ClientSession() as http:
            for url in urls[:5]:  # limit to 5 images
                try:
                    if egress is not None:
                        allowed, reason = egress.check_url(
                            url,
                            tool_name="discord_image_download",
                        )
                        if not allowed:
                            logger.info(
                                "discord_image_download_blocked",
                                url=url[:120],
                                reason=reason,
                            )
                            continue
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
                        max_image_bytes = 20 * 1024 * 1024
                        if egress is not None:
                            policy_cap = int(getattr(egress, "max_response_bytes", 0) or 0)
                            if policy_cap > 0:
                                max_image_bytes = min(max_image_bytes, policy_cap)
                            data = await egress.read_limited_bytes(resp, max_bytes=max_image_bytes)
                        else:
                            data = await resp.read()
                        if len(data) > max_image_bytes:
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

    @staticmethod
    def _collect_recent_tool_names(session: Any, start_index: int) -> list[str]:
        """Collect tool names from messages appended after start_index."""
        names: list[str] = []
        msgs = getattr(session, "messages", []) or []
        for msg in msgs[max(0, int(start_index or 0)):]:
            role_obj = getattr(msg, "role", None)
            role = str(getattr(role_obj, "value", role_obj or "")).strip().lower()
            if role != "tool":
                continue
            raw_name = getattr(msg, "name", "")
            name = str(raw_name or "").strip()
            if name:
                names.append(name)
        return names

    async def _capture_post_action_screenshot(
        self,
        session: Session,
        *,
        recent_tool_names: list[str] | None = None,
    ) -> str | None:
        """Capture a screenshot after computer/web operation tool usage."""
        try:
            disabled = getattr(self.config.security, "disabled_tools", []) or []
            disabled_set = {str(t).strip() for t in disabled if str(t).strip()}
        except Exception:
            disabled_set = set()

        recent_set = {str(n).strip() for n in (recent_tool_names or []) if str(n).strip()}
        prefer_web = bool(recent_set & _WEB_OPERATION_TOOL_NAMES)

        capture_plan: list[tuple[str, dict[str, Any]]] = []
        if prefer_web and "web_screenshot" not in disabled_set:
            capture_plan.append(("web_screenshot", {"full_page": False}))
        if "screenshot" not in disabled_set:
            capture_plan.append(("screenshot", {"monitor": 1}))

        for tool_name, params in capture_plan:
            try:
                result = await self.effective_engine.tools.execute(
                    tool_name,
                    params,
                    self._build_tool_context(session),
                )
            except Exception as e:
                logger.debug(
                    "discord_post_action_capture_failed",
                    tool=tool_name,
                    error=str(e),
                )
                continue

            if not result.success:
                logger.debug(
                    "discord_post_action_capture_tool_failed",
                    tool=tool_name,
                    error=result.error or "",
                )
                continue

            path = str(result.image_path or "").strip()
            if not path:
                continue
            try:
                generated = session.metadata.setdefault("generated_images", [])
                if isinstance(generated, list) and path not in generated:
                    generated.append(path)
            except Exception:
                pass
            return path

        return None

    @staticmethod
    def _fit_image_for_discord(path: str, max_bytes: int) -> str | None:
        """Best-effort re-encode/resize so the image fits Discord upload limits."""
        import tempfile
        from pathlib import Path

        try:
            from PIL import Image
        except Exception:
            return None

        src = Path(path)
        if not src.is_file():
            return None

        try:
            with Image.open(src) as raw_img:
                base = raw_img.convert("RGB")
                orig_w, orig_h = base.size
                scale = 1.0

                def _resample_mode() -> Any:
                    if hasattr(Image, "Resampling"):
                        return Image.Resampling.LANCZOS
                    return Image.LANCZOS

                for attempt in range(10):
                    if scale < 1.0:
                        w = max(1, int(orig_w * scale))
                        h = max(1, int(orig_h * scale))
                        candidate_img = base.resize((w, h), _resample_mode())
                    else:
                        candidate_img = base

                    quality = max(30, 92 - attempt * 7)
                    tmp = tempfile.NamedTemporaryFile(
                        suffix=".jpg",
                        prefix="kuro_discord_img_",
                        delete=False,
                    )
                    tmp_path = tmp.name
                    tmp.close()
                    try:
                        candidate_img.save(
                            tmp_path,
                            format="JPEG",
                            quality=quality,
                            optimize=True,
                            progressive=True,
                        )
                        if Path(tmp_path).stat().st_size <= max_bytes:
                            return tmp_path
                    except Exception:
                        try:
                            Path(tmp_path).unlink()
                        except Exception:
                            pass
                    else:
                        try:
                            Path(tmp_path).unlink()
                        except Exception:
                            pass

                    if attempt in {1, 3, 5, 7}:
                        scale *= 0.75
        except Exception:
            return None

        return None

    async def _send_images(self, channel: Any, image_paths: list[str]) -> None:
        """Send image files as Discord attachments."""
        import discord
        from pathlib import Path

        max_bytes = 8 * 1024 * 1024
        try:
            guild = getattr(channel, "guild", None)
            guild_limit = int(getattr(guild, "filesize_limit", 0) or 0) if guild else 0
            if guild_limit > 0:
                max_bytes = guild_limit
        except Exception:
            pass

        for path in image_paths[:5]:  # limit to 5 images
            temp_prepared: str | None = None
            try:
                p = Path(path)
                if not p.is_file():
                    continue

                send_path = p
                if p.stat().st_size > max_bytes:
                    prepared = self._fit_image_for_discord(str(p), max_bytes)
                    if not prepared:
                        logger.warning(
                            "discord_image_too_large",
                            path=str(p),
                            size=p.stat().st_size,
                            limit=max_bytes,
                        )
                        continue
                    temp_prepared = prepared
                    send_path = Path(prepared)

                await channel.send(file=discord.File(str(send_path), filename=send_path.name))
            except Exception as e:
                logger.debug("discord_send_image_failed", path=path, error=str(e))
            finally:
                if temp_prepared:
                    try:
                        Path(temp_prepared).unlink()
                    except Exception:
                        pass

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

    @property
    def default_channel_name(self) -> str | None:
        """Get the default notification channel name when available."""
        return self._default_channel_name

    @property
    def bot_display_name(self) -> str | None:
        """Get the Discord bot display name for UI labeling."""
        if self._bot is not None and getattr(self._bot, "user", None) is not None:
            return str(self._bot.user)
        return None

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
