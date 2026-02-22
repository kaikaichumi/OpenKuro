"""Slack adapter: full implementation using slack-bolt with Socket Mode.

Features:
- Socket Mode (no public URL needed, similar to Telegram long-polling)
- Per-channel+user sessions with conversation history
- Block Kit interactive messages for tool approval
- Smart message splitting (4000 char limit)
- Slash commands: /kuro-help, /kuro-model, /kuro-clear, /kuro-trust
- User/channel whitelist (optional, empty = allow all)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import structlog

from src.adapters.base import BaseAdapter
from src.adapters.utils import split_message
from src.config import KuroConfig
from src.core.engine import ApprovalCallback, Engine
from src.core.types import Session
from src.tools.base import RiskLevel

logger = structlog.get_logger()

DEFAULT_APPROVAL_TIMEOUT = 60


def _format_params_slack(params: dict[str, Any]) -> str:
    """Format tool parameters for Slack display (truncated)."""
    if not params:
        return "_No parameters_"
    try:
        text = json.dumps(params, ensure_ascii=False, indent=2)
        if len(text) > 500:
            text = text[:500] + "\n... (truncated)"
        return f"```{text}```"
    except Exception:
        return str(params)[:300]


class SlackApprovalCallback(ApprovalCallback):
    """Approval callback using Slack Block Kit interactive messages."""

    def __init__(self, approval_policy=None) -> None:
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._app = None  # slack_bolt AsyncApp, set by SlackAdapter
        self._channel_map: dict[str, str] = {}  # session_id -> channel_id
        self._timeout: int = DEFAULT_APPROVAL_TIMEOUT
        self.approval_policy = approval_policy

    def set_app(self, app) -> None:
        self._app = app

    def register_channel(self, session_id: str, channel_id: str) -> None:
        self._channel_map[session_id] = channel_id

    async def request_approval(
        self,
        tool_name: str,
        params: dict[str, Any],
        risk_level: RiskLevel,
        session: Session,
    ) -> bool:
        if self._app is None:
            return False

        channel_id = self._channel_map.get(session.id)
        if channel_id is None:
            return False

        try:
            approval_id = str(uuid4())[:8]
            params_text = _format_params_slack(params)

            risk_emoji = {
                RiskLevel.LOW: "✅",
                RiskLevel.MEDIUM: "⚠️",
                RiskLevel.HIGH: "🔴",
                RiskLevel.CRITICAL: "☢️",
            }
            emoji = risk_emoji.get(risk_level, "❓")

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"⚡ *Approval Required*\n\n"
                            f"*Tool:* `{tool_name}`\n"
                            f"*Risk:* {emoji} {risk_level.value.upper()}"
                        ),
                    },
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Params:*\n{params_text}"},
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Allow"},
                            "style": "primary",
                            "action_id": "kuro_approve",
                            "value": approval_id,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Deny"},
                            "style": "danger",
                            "action_id": "kuro_deny",
                            "value": approval_id,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": f"Trust {risk_level.value}"},
                            "action_id": "kuro_trust",
                            "value": f"{approval_id}:{risk_level.value}",
                        },
                    ],
                },
            ]

            loop = asyncio.get_running_loop()
            future: asyncio.Future[bool] = loop.create_future()
            self._pending[approval_id] = future
            self._pending[f"session:{approval_id}"] = session  # type: ignore[assignment]

            await self._app.client.chat_postMessage(
                channel=channel_id,
                blocks=blocks,
                text=f"Approval required for `{tool_name}`",
            )

            try:
                result = await asyncio.wait_for(future, timeout=self._timeout)
                return result
            except asyncio.TimeoutError:
                await self._app.client.chat_postMessage(
                    channel=channel_id,
                    text=f"⏰ Approval timed out for `{tool_name}`. Action denied.",
                )
                return False
            finally:
                self._pending.pop(approval_id, None)
                self._pending.pop(f"session:{approval_id}", None)

        except Exception as e:
            logger.error("slack_approval_error", error=str(e))
            return False

    def handle_action(self, action: str, approval_id: str, extra: str | None = None) -> str | None:
        """Process a Block Kit button action. Returns status text or None."""
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return "This approval request has expired."

        if action == "approve":
            future.set_result(True)
            return "✅ Approved"
        elif action == "deny":
            future.set_result(False)
            return "❌ Denied"
        elif action == "trust":
            session = self._pending.get(f"session:{approval_id}")
            if session and self.approval_policy and extra:
                try:
                    from src.tools.base import RiskLevel as RL
                    level = RL(extra)
                    self.approval_policy.elevate_session_trust(session.id, level)
                except Exception:
                    pass
            future.set_result(True)
            return f"✅ Approved and trusted {extra or ''}"
        return None


class SlackAdapter(BaseAdapter):
    """Slack adapter using slack-bolt with Socket Mode."""

    name = "slack"

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        super().__init__(engine, config)
        self._app = None
        self._handler = None
        self._approval_cb = SlackApprovalCallback(
            approval_policy=engine.approval_policy
        )
        self.engine.approval_cb = self._approval_cb

    async def start(self) -> None:
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        bot_token = self.config.adapters.slack.get_bot_token()
        app_token = self.config.adapters.slack.get_app_token()

        if not bot_token:
            raise ValueError(
                f"Slack bot token not set. "
                f"Set {self.config.adapters.slack.bot_token_env} environment variable."
            )
        if not app_token:
            raise ValueError(
                f"Slack app token not set. "
                f"Set {self.config.adapters.slack.app_token_env} environment variable."
            )

        self._app = AsyncApp(token=bot_token)
        self._approval_cb.set_app(self._app)

        # --- Event Handlers ---

        @self._app.event("app_mention")
        async def handle_mention(event, say):
            await self._handle_event(event, say, strip_mention=True)

        @self._app.event("message")
        async def handle_dm(event, say):
            # Only handle DMs (channel_type = "im") to avoid double-processing mentions
            if event.get("channel_type") == "im":
                await self._handle_event(event, say)

        # --- Slash Commands ---

        @self._app.command("/kuro-help")
        async def cmd_help(ack, respond):
            await ack()
            await respond(
                "🤖 *Kuro Commands*\n"
                "• `/kuro-help` — Show this help\n"
                "• `/kuro-model <name>` — Switch AI model\n"
                "• `/kuro-stats` — Dashboard overview\n"
                "• `/kuro-costs` — Token usage & cost breakdown\n"
                "• `/kuro-security` — Security report\n"
                "• `/kuro-clear` — Clear conversation history\n"
                "• `/kuro-trust <low|medium|high>` — Set session trust level\n\n"
                "Mention me in a channel or DM me to chat!"
            )

        @self._app.command("/kuro-clear")
        async def cmd_clear(ack, respond, command):
            await ack()
            channel_id = command["channel_id"]
            user_id = command["user_id"]
            session_key = self._session_key(channel_id, user_id)
            self.clear_session(session_key)
            await respond("🗑 Conversation cleared. Starting fresh!")

        @self._app.command("/kuro-model")
        async def cmd_model(ack, respond, command):
            await ack()
            parts = command.get("text", "").strip().split()
            if not parts:
                session_key = self._session_key(command["channel_id"], command["user_id"])
                session = self.get_or_create_session(session_key)
                model = session.metadata.get("model_override", self.config.models.default)
                await respond(f"Current model: `{model}`")
            else:
                new_model = parts[0]
                session_key = self._session_key(command["channel_id"], command["user_id"])
                session = self.get_or_create_session(session_key)
                session.metadata["model_override"] = new_model
                await respond(f"✅ Model switched to `{new_model}`")

        @self._app.command("/kuro-trust")
        async def cmd_trust(ack, respond, command):
            await ack()
            level_str = command.get("text", "").strip().lower()
            if level_str not in ("low", "medium", "high"):
                await respond("Usage: `/kuro-trust <low|medium|high>`")
                return
            session_key = self._session_key(command["channel_id"], command["user_id"])
            session = self.get_or_create_session(session_key)
            session.trust_level = level_str
            await respond(f"✅ Trust level set to `{level_str}` for this session.")

        @self._app.command("/kuro-stats")
        async def cmd_stats(ack, respond):
            await ack()
            from src.adapters.dashboard_commands import handle_stats_command
            text = await handle_stats_command(max_chars=3900)
            await respond(f"```\n{text}\n```")

        @self._app.command("/kuro-costs")
        async def cmd_costs(ack, respond):
            await ack()
            from src.adapters.dashboard_commands import handle_costs_command
            text = await handle_costs_command(max_chars=3900)
            await respond(f"```\n{text}\n```")

        @self._app.command("/kuro-security")
        async def cmd_security(ack, respond):
            await ack()
            from src.adapters.dashboard_commands import handle_security_command
            text = await handle_security_command(max_chars=3900)
            await respond(f"```\n{text}\n```")

        # --- Block Kit Action Handlers ---

        @self._app.action("kuro_approve")
        async def handle_approve(ack, body, respond):
            await ack()
            approval_id = body["actions"][0]["value"]
            msg = self._approval_cb.handle_action("approve", approval_id)
            await respond(msg or "✅ Approved")

        @self._app.action("kuro_deny")
        async def handle_deny(ack, body, respond):
            await ack()
            approval_id = body["actions"][0]["value"]
            msg = self._approval_cb.handle_action("deny", approval_id)
            await respond(msg or "❌ Denied")

        @self._app.action("kuro_trust")
        async def handle_trust(ack, body, respond):
            await ack()
            value = body["actions"][0]["value"]
            parts = value.split(":", 1)
            approval_id = parts[0]
            extra = parts[1] if len(parts) > 1 else None
            msg = self._approval_cb.handle_action("trust", approval_id, extra)
            await respond(msg or "✅ Approved")

        # Start Socket Mode
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        await self._handler.start_async()
        logger.info("slack_started")

    async def stop(self) -> None:
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception as e:
                logger.warning("slack_stop_error", error=str(e))
        logger.info("slack_stopped")

    def _session_key(self, channel_id: str, user_id: str) -> str:
        return f"{channel_id}:{user_id}"

    def _is_user_allowed(self, user_id: str) -> bool:
        allowed = self.config.adapters.slack.allowed_user_ids
        if not allowed:
            return True
        return user_id in allowed

    def _is_channel_allowed(self, channel_id: str) -> bool:
        allowed = self.config.adapters.slack.allowed_channel_ids
        if not allowed:
            return True
        return channel_id in allowed

    async def _handle_event(self, event: dict, say, strip_mention: bool = False) -> None:
        """Process an incoming Slack message event."""
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        text = event.get("text", "").strip()

        if not user_id or not text:
            return

        if not self._is_user_allowed(user_id):
            logger.debug("slack_user_blocked", user_id=user_id)
            return

        if not self._is_channel_allowed(channel_id):
            logger.debug("slack_channel_blocked", channel_id=channel_id)
            return

        # Strip bot mention from text (e.g., "<@U123ABC> hello" -> "hello")
        if strip_mention:
            import re
            text = re.sub(r"<@\w+>\s*", "", text).strip()

        if not text:
            return

        session_key = self._session_key(channel_id, user_id)
        session = self.get_or_create_session(session_key)
        self._approval_cb.register_channel(session.id, channel_id)

        model = session.metadata.get("model_override")

        try:
            response = await self.engine.process_message(text, session, model=model)

            max_len = self.config.adapters.slack.max_message_length
            chunks = split_message(response, max_len)
            for chunk in chunks:
                await say(chunk)

        except Exception as e:
            logger.error("slack_process_error", user_id=user_id, error=str(e))
            await say(f"❌ Error: {str(e)[:200]}")

    async def send_notification(self, user_id: str, message: str) -> bool:
        """Send a proactive notification to a Slack channel or DM."""
        if self._app is None:
            return False
        try:
            max_len = self.config.adapters.slack.max_message_length
            chunks = split_message(message, max_len)
            for chunk in chunks:
                await self._app.client.chat_postMessage(
                    channel=user_id,
                    text=chunk,
                )
            logger.info("slack_notification_sent", channel=user_id)
            return True
        except Exception as e:
            logger.error("slack_notification_failed", error=str(e), user_id=user_id)
            return False
