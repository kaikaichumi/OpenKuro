"""Telegram adapter: full implementation using python-telegram-bot v21+.

Features:
- Async polling (no webhook needed)
- Per-user sessions with conversation history
- Inline keyboard buttons for tool approval
- Smart message splitting (4096 char limit)
- Slash commands: /start, /help, /model, /clear, /trust
- User whitelist (optional, empty = allow all)
"""

from __future__ import annotations

import asyncio
import re
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


class TelegramApprovalCallback(ApprovalCallback):
    """Approval callback that uses Telegram inline keyboard buttons.

    When a tool needs approval, sends an inline keyboard message to the
    user's chat. The user presses a button, which resolves an asyncio
    Future that the engine is awaiting.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._bot = None  # Set by TelegramAdapter after Application is built
        self._chat_ids: dict[str, int] = {}  # session_id -> chat_id
        self._timeout: int = DEFAULT_APPROVAL_TIMEOUT

    def set_bot(self, bot) -> None:
        """Set the bot instance (called by TelegramAdapter)."""
        self._bot = bot

    def register_chat(self, session_id: str, chat_id: int) -> None:
        """Map a session to its Telegram chat ID."""
        self._chat_ids[session_id] = chat_id

    async def request_approval(
        self,
        tool_name: str,
        params: dict[str, Any],
        risk_level: RiskLevel,
        session: Session,
    ) -> bool:
        """Send inline keyboard to Telegram and await user's decision."""
        if self._bot is None:
            logger.error("telegram_approval_no_bot")
            return False

        chat_id = self._chat_ids.get(session.id)
        if chat_id is None:
            logger.error("telegram_approval_no_chat", session_id=session.id)
            return False

        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            approval_id = str(uuid4())[:8]

            # Format parameters for display
            params_text = _format_params_telegram(params)

            risk_emoji = {
                RiskLevel.LOW: "\u2705",      # ✅
                RiskLevel.MEDIUM: "\u26a0\ufe0f",   # ⚠️
                RiskLevel.HIGH: "\U0001f534",   # 🔴
                RiskLevel.CRITICAL: "\u2622\ufe0f",  # ☢️
            }
            emoji = risk_emoji.get(risk_level, "\u2753")  # ❓

            text = (
                f"\u26a1 *Approval Required*\n\n"
                f"*Tool:* `{tool_name}`\n"
                f"*Risk:* {emoji} {risk_level.value.upper()}\n"
                f"*Params:*\n{params_text}"
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("\u2705 Allow", callback_data=f"approve:{approval_id}"),
                    InlineKeyboardButton("\u274c Deny", callback_data=f"deny:{approval_id}"),
                ],
                [
                    InlineKeyboardButton(
                        "\U0001f513 Trust this level",
                        callback_data=f"trust:{approval_id}:{risk_level.value}",
                    ),
                ],
            ])

            # Create Future before sending message
            loop = asyncio.get_running_loop()
            future: asyncio.Future[bool] = loop.create_future()
            self._pending[approval_id] = future

            # Store session reference for trust escalation
            self._pending[f"session:{approval_id}"] = session  # type: ignore[assignment]

            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

            # Wait for user response with timeout
            try:
                result = await asyncio.wait_for(future, timeout=self._timeout)
                return result
            except asyncio.TimeoutError:
                logger.info(
                    "telegram_approval_timeout",
                    approval_id=approval_id,
                    tool=tool_name,
                )
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=f"\u23f0 Approval timed out for `{tool_name}`. Action denied.",
                    parse_mode="Markdown",
                )
                return False
            finally:
                self._pending.pop(approval_id, None)
                self._pending.pop(f"session:{approval_id}", None)

        except Exception as e:
            logger.error("telegram_approval_error", error=str(e))
            return False

    async def handle_callback(self, callback_data: str) -> str | None:
        """Process an inline keyboard button press.

        Returns a status message or None.
        """
        parts = callback_data.split(":")
        if len(parts) < 2:
            return None

        action = parts[0]
        approval_id = parts[1]

        future = self._pending.get(approval_id)
        if future is None or future.done():
            return "This approval request has expired."

        if action == "approve":
            future.set_result(True)
            return "\u2705 Approved"
        elif action == "deny":
            future.set_result(False)
            return "\u274c Denied"
        elif action == "trust" and len(parts) >= 3:
            # Trust escalation
            risk_level = parts[2]
            session = self._pending.get(f"session:{approval_id}")
            if session and hasattr(session, "trust_level"):
                session.trust_level = risk_level
            future.set_result(True)
            return f"\U0001f513 Trusted {risk_level.upper()} actions for this session"

        return None


class TelegramAdapter(BaseAdapter):
    """Full Telegram bot adapter using python-telegram-bot v21+.

    Uses long-polling (no webhook server needed).
    Each Telegram user gets their own Session.
    """

    name = "telegram"

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        super().__init__(engine, config)

        self._app = None
        self._approval_cb = TelegramApprovalCallback()
        self._approval_cb._timeout = config.adapters.telegram.approval_timeout

        # Replace the engine's approval callback with Telegram's
        self.engine.approval_cb = self._approval_cb

    async def start(self) -> None:
        """Initialize the Telegram bot and start polling."""
        from telegram import Update
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        token = self.config.adapters.telegram.get_bot_token()
        if not token:
            env_var = self.config.adapters.telegram.bot_token_env
            raise RuntimeError(
                f"Telegram bot token not found. "
                f"Set the {env_var} environment variable."
            )

        # Build the Application
        self._app = Application.builder().token(token).build()

        # Set bot reference for approval callback
        self._approval_cb.set_bot(self._app.bot)

        # Register handlers
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler("help", self._on_help))
        self._app.add_handler(CommandHandler("model", self._on_model))
        self._app.add_handler(CommandHandler("clear", self._on_clear))
        self._app.add_handler(CommandHandler("trust", self._on_trust))
        self._app.add_handler(CallbackQueryHandler(self._on_callback_query))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

        logger.info("telegram_starting", bot_token="***" + token[-4:])

        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        # Get bot info
        bot_info = await self._app.bot.get_me()
        logger.info(
            "telegram_started",
            bot_username=bot_info.username,
            bot_id=bot_info.id,
        )

    async def stop(self) -> None:
        """Stop the Telegram bot gracefully."""
        if self._app:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning("telegram_stop_error", error=str(e))
            logger.info("telegram_stopped")

    def _is_user_allowed(self, user_id: int) -> bool:
        """Check if the user is in the whitelist (if configured)."""
        allowed = self.config.adapters.telegram.allowed_user_ids
        if not allowed:
            return True  # Empty list = allow all
        return user_id in allowed

    async def _on_start(self, update, context) -> None:
        """Handle /start command."""
        if not self._is_user_allowed(update.effective_user.id):
            return

        await update.message.reply_text(
            "\U0001f3d4 *Kuro AI Assistant*\n\n"
            "I'm your personal AI assistant. Send me a message and I'll help!\n\n"
            "*Commands:*\n"
            "/help - Show help\n"
            "/model - Show/switch model\n"
            "/clear - Clear conversation\n"
            "/trust - Show/set trust level\n",
            parse_mode="Markdown",
        )

    async def _on_help(self, update, context) -> None:
        """Handle /help command."""
        if not self._is_user_allowed(update.effective_user.id):
            return

        await update.message.reply_text(
            "*Available Commands:*\n\n"
            "/start - Welcome message\n"
            "/help - Show this help\n"
            "/model `<name>` - Switch AI model\n"
            "/model - Show current model\n"
            "/clear - Reset conversation history\n"
            "/trust `<level>` - Set trust (low/medium/high)\n\n"
            "*Tips:*\n"
            "- Just type naturally to chat\n"
            "- I can read/write files, run commands, browse the web\n"
            "- High-risk actions require your approval via buttons\n",
            parse_mode="Markdown",
        )

    async def _on_model(self, update, context) -> None:
        """Handle /model command."""
        if not self._is_user_allowed(update.effective_user.id):
            return

        user_key = str(update.effective_user.id)
        session = self.get_or_create_session(user_key)

        args = context.args
        if args:
            model_name = " ".join(args)
            session.metadata["model_override"] = model_name
            await update.message.reply_text(
                f"\u2705 Model switched to: `{model_name}`",
                parse_mode="Markdown",
            )
        else:
            current = session.metadata.get(
                "model_override", self.config.models.default
            )
            await update.message.reply_text(
                f"\U0001f916 Current model: `{current}`",
                parse_mode="Markdown",
            )

    async def _on_clear(self, update, context) -> None:
        """Handle /clear command."""
        if not self._is_user_allowed(update.effective_user.id):
            return

        user_key = str(update.effective_user.id)
        self.clear_session(user_key)
        await update.message.reply_text(
            "\U0001f5d1 Conversation cleared. Starting fresh!",
        )

    async def _on_trust(self, update, context) -> None:
        """Handle /trust command."""
        if not self._is_user_allowed(update.effective_user.id):
            return

        user_key = str(update.effective_user.id)
        session = self.get_or_create_session(user_key)

        args = context.args
        if args and args[0] in ("low", "medium", "high", "critical"):
            session.trust_level = args[0]
            await update.message.reply_text(
                f"\U0001f513 Trust level set to: *{args[0].upper()}*",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"\U0001f512 Current trust level: *{session.trust_level.upper()}*\n"
                f"Usage: /trust low|medium|high|critical",
                parse_mode="Markdown",
            )

    async def _on_message(self, update, context) -> None:
        """Handle regular text messages."""
        user = update.effective_user
        if not self._is_user_allowed(user.id):
            logger.debug("telegram_user_blocked", user_id=user.id)
            return

        user_key = str(user.id)
        session = self.get_or_create_session(user_key)
        chat_id = update.effective_chat.id

        # Register chat for approval callbacks
        self._approval_cb.register_chat(session.id, chat_id)

        user_text = update.message.text
        if not user_text:
            return

        logger.info(
            "telegram_message",
            user_id=user.id,
            username=user.username,
            text_len=len(user_text),
        )

        # Send "typing" indicator
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        try:
            # Get model override if set
            model = session.metadata.get("model_override")

            # Process through engine
            response = await self.engine.process_message(
                user_text, session, model=model
            )

            # Send response (split if needed)
            chunks = split_message(response, self.config.adapters.telegram.max_message_length)
            for chunk in chunks:
                await update.message.reply_text(chunk)

        except Exception as e:
            logger.error(
                "telegram_process_error",
                user_id=user.id,
                error=str(e),
            )
            await update.message.reply_text(
                f"\u274c Error processing message: {str(e)[:200]}"
            )

    async def _on_callback_query(self, update, context) -> None:
        """Handle inline keyboard button presses (approval responses)."""
        query = update.callback_query
        if not query or not query.data:
            return

        # Process the approval callback
        status_msg = await self._approval_cb.handle_callback(query.data)

        # Answer the callback query (removes loading indicator)
        await query.answer(text=status_msg or "Processed")

        # Edit the original message to show the result
        if status_msg:
            try:
                original_text = query.message.text or ""
                await query.edit_message_text(
                    text=f"{original_text}\n\n*Result:* {status_msg}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass  # Message might be too old to edit


def split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a message into chunks respecting Telegram's length limit.

    Splitting priority:
    1. By paragraph (double newline)
    2. By line (single newline)
    3. By space (word boundary)
    4. By character (last resort)
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


def _format_params_telegram(params: dict[str, Any]) -> str:
    """Format tool parameters for Telegram display."""
    if not params:
        return "`(none)`"
    lines = []
    for k, v in params.items():
        val_str = str(v)
        if len(val_str) > 80:
            val_str = val_str[:80] + "..."
        lines.append(f"  `{k}`: {val_str}")
    return "\n".join(lines)
