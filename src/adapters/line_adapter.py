"""LINE Messaging API adapter using line-bot-sdk v3 with webhook.

Features:
- Webhook-based (needs public URL or ngrok tunnel)
- Per-user sessions with conversation history
- Quick Reply buttons for tool approval (Postback actions)
- Smart message splitting (5000 char limit)
- User whitelist (optional, empty = allow all)

Setup:
1. Create a LINE Messaging API channel at developers.line.biz
2. Set KURO_LINE_CHANNEL_SECRET and KURO_LINE_ACCESS_TOKEN env vars
3. Expose webhook via ngrok or cloudflare tunnel
4. Set webhook URL in LINE Developer Console: https://your-url/webhook
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


def _format_params_line(params: dict[str, Any]) -> str:
    """Format tool parameters for LINE display (truncated)."""
    if not params:
        return "No parameters"
    try:
        text = json.dumps(params, ensure_ascii=False, indent=2)
        if len(text) > 300:
            text = text[:300] + "\n... (truncated)"
        return text
    except Exception:
        return str(params)[:200]


class LineApprovalCallback(ApprovalCallback):
    """Approval callback using LINE Quick Reply buttons (Postback actions)."""

    def __init__(self, approval_policy=None) -> None:
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._api = None  # AsyncMessagingApi, set by LineAdapter
        self._user_map: dict[str, str] = {}  # session_id -> LINE user_id
        self._timeout: int = DEFAULT_APPROVAL_TIMEOUT
        self.approval_policy = approval_policy

    def set_api(self, api) -> None:
        self._api = api

    def register_user(self, session_id: str, user_id: str) -> None:
        self._user_map[session_id] = user_id

    async def request_approval(
        self,
        tool_name: str,
        params: dict[str, Any],
        risk_level: RiskLevel,
        session: Session,
    ) -> bool:
        if self._api is None:
            return False

        line_user_id = self._user_map.get(session.id)
        if line_user_id is None:
            return False

        try:
            from linebot.v3.messaging import (
                TextMessage,
                QuickReply,
                QuickReplyItem,
                PostbackAction,
                PushMessageRequest,
            )

            approval_id = str(uuid4())[:8]
            params_text = _format_params_line(params)

            risk_emoji = {
                RiskLevel.LOW: "✅",
                RiskLevel.MEDIUM: "⚠️",
                RiskLevel.HIGH: "🔴",
                RiskLevel.CRITICAL: "☢️",
            }
            emoji = risk_emoji.get(risk_level, "❓")

            text = (
                f"⚡ Approval Required\n\n"
                f"Tool: {tool_name}\n"
                f"Risk: {emoji} {risk_level.value.upper()}\n\n"
                f"Params:\n{params_text}"
            )

            quick_reply = QuickReply(items=[
                QuickReplyItem(
                    action=PostbackAction(
                        label="Allow",
                        data=f"approve:{approval_id}",
                        display_text="Allow",
                    )
                ),
                QuickReplyItem(
                    action=PostbackAction(
                        label="Deny",
                        data=f"deny:{approval_id}",
                        display_text="Deny",
                    )
                ),
                QuickReplyItem(
                    action=PostbackAction(
                        label=f"Trust {risk_level.value}",
                        data=f"trust:{approval_id}:{risk_level.value}",
                        display_text=f"Trust {risk_level.value}",
                    )
                ),
            ])

            loop = asyncio.get_running_loop()
            future: asyncio.Future[bool] = loop.create_future()
            self._pending[approval_id] = future
            self._pending[f"session:{approval_id}"] = session  # type: ignore[assignment]

            await self._api.push_message(
                PushMessageRequest(
                    to=line_user_id,
                    messages=[TextMessage(text=text, quick_reply=quick_reply)],
                )
            )

            try:
                result = await asyncio.wait_for(future, timeout=self._timeout)
                return result
            except asyncio.TimeoutError:
                await self._api.push_message(
                    PushMessageRequest(
                        to=line_user_id,
                        messages=[TextMessage(
                            text=f"⏰ Approval timed out for {tool_name}. Action denied."
                        )],
                    )
                )
                return False
            finally:
                self._pending.pop(approval_id, None)
                self._pending.pop(f"session:{approval_id}", None)

        except Exception as e:
            logger.error("line_approval_error", error=str(e))
            return False

    def handle_postback(self, data: str) -> str | None:
        """Process a postback event. Returns status text or None."""
        parts = data.split(":", 2)
        if len(parts) < 2:
            return None

        action = parts[0]
        approval_id = parts[1]
        extra = parts[2] if len(parts) > 2 else None

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


class LineAdapter(BaseAdapter):
    """LINE Messaging API adapter with webhook server."""

    name = "line"

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        super().__init__(engine, config)
        self._api = None
        self._parser = None
        self._runner = None
        self._site = None
        self._approval_cb = LineApprovalCallback(
            approval_policy=engine.approval_policy
        )
        self.engine.approval_cb = self._approval_cb

    async def start(self) -> None:
        from aiohttp import web
        from linebot.v3.messaging import AsyncMessagingApi, AsyncApiClient, Configuration
        from linebot.v3.webhook import WebhookParser

        secret = self.config.adapters.line.get_channel_secret()
        token = self.config.adapters.line.get_access_token()

        if not secret:
            raise ValueError(
                f"LINE channel secret not set. "
                f"Set {self.config.adapters.line.channel_secret_env} environment variable."
            )
        if not token:
            raise ValueError(
                f"LINE access token not set. "
                f"Set {self.config.adapters.line.channel_access_token_env} environment variable."
            )

        configuration = Configuration(access_token=token)
        api_client = AsyncApiClient(configuration)
        self._api = AsyncMessagingApi(api_client)
        self._parser = WebhookParser(secret)
        self._approval_cb.set_api(self._api)

        # Set up aiohttp webhook server
        app = web.Application()
        app.router.add_post("/webhook", self._handle_webhook)
        app.router.add_get("/health", lambda req: web.Response(text="OK"))

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        port = self.config.adapters.line.webhook_port
        self._site = web.TCPSite(self._runner, "0.0.0.0", port)
        await self._site.start()

        logger.info("line_started", port=port, webhook_path="/webhook")
        print(f"LINE webhook listening on port {port}. Set webhook URL in LINE Developer Console.")

    async def stop(self) -> None:
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception as e:
                logger.warning("line_stop_error", error=str(e))
        logger.info("line_stopped")

    def _is_user_allowed(self, user_id: str) -> bool:
        allowed = self.config.adapters.line.allowed_user_ids
        if not allowed:
            return True
        return user_id in allowed

    async def _handle_webhook(self, request) -> Any:
        """Parse and dispatch incoming LINE webhook events."""
        from aiohttp import web
        from linebot.v3.exceptions import InvalidSignatureError
        from linebot.v3.webhooks import (
            MessageEvent,
            PostbackEvent,
            TextMessageContent,
        )

        signature = request.headers.get("X-Line-Signature", "")
        body = await request.read()

        try:
            events = self._parser.parse(body.decode("utf-8"), signature)
        except InvalidSignatureError:
            logger.warning("line_invalid_signature")
            return web.Response(status=400, text="Invalid signature")
        except Exception as e:
            logger.error("line_parse_error", error=str(e))
            return web.Response(status=400, text="Parse error")

        for event in events:
            if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
                asyncio.create_task(self._process_message(event))
            elif isinstance(event, PostbackEvent):
                asyncio.create_task(self._process_postback(event))

        return web.Response(text="OK")

    async def _try_quick_command(self, text: str) -> str | None:
        """Check if text is a quick command. Returns response or None."""
        cmd = text.strip().lower()
        max_chars = self.config.adapters.line.max_message_length - 100
        if cmd in ("#stats", "/stats"):
            from src.adapters.dashboard_commands import handle_stats_command
            return await handle_stats_command(max_chars)
        if cmd in ("#costs", "/costs"):
            from src.adapters.dashboard_commands import handle_costs_command
            return await handle_costs_command(max_chars)
        if cmd in ("#security", "/security"):
            from src.adapters.dashboard_commands import handle_security_command
            return await handle_security_command(max_chars)
        if cmd in ("#help", "/help"):
            return (
                "\U0001f3d4 Kuro AI Assistant\n\n"
                "Commands:\n"
                "/stats - Dashboard overview\n"
                "/costs - Token & cost report\n"
                "/security - Security report\n"
                "/help - Show this help\n\n"
                "Or just type naturally to chat!"
            )
        return None

    async def _process_message(self, event) -> None:
        """Handle a LINE text message."""
        from linebot.v3.messaging import (
            ReplyMessageRequest,
            TextMessage,
        )

        user_id = event.source.user_id
        text = event.message.text.strip()
        reply_token = event.reply_token

        if not self._is_user_allowed(user_id):
            logger.debug("line_user_blocked", user_id=user_id)
            return

        # Quick commands (bypass LLM)
        cmd_response = await self._try_quick_command(text)
        if cmd_response is not None:
            from linebot.v3.messaging import ReplyMessageRequest, TextMessage

            max_len = self.config.adapters.line.max_message_length
            chunks = split_message(cmd_response, max_len)
            await self._api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=chunks[0])],
                )
            )
            for chunk in chunks[1:]:
                from linebot.v3.messaging import PushMessageRequest

                await self._api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=chunk)],
                    )
                )
            return

        session = self.get_or_create_session(user_id)
        self._approval_cb.register_user(session.id, user_id)

        model = session.metadata.get("model_override")

        try:
            response = await self.engine.process_message(text, session, model=model)

            max_len = self.config.adapters.line.max_message_length
            chunks = split_message(response, max_len)

            # Reply with first chunk, push the rest
            await self._api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=chunks[0])],
                )
            )
            for chunk in chunks[1:]:
                from linebot.v3.messaging import PushMessageRequest
                await self._api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=chunk)],
                    )
                )

        except Exception as e:
            logger.error("line_process_error", user_id=user_id, error=str(e))
            try:
                from linebot.v3.messaging import ReplyMessageRequest, TextMessage
                await self._api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(text=f"❌ Error: {str(e)[:200]}")],
                    )
                )
            except Exception:
                pass

    async def _process_postback(self, event) -> None:
        """Handle a LINE postback event (button press)."""
        user_id = event.source.user_id
        data = event.postback.data

        result = self._approval_cb.handle_postback(data)
        if result:
            try:
                from linebot.v3.messaging import PushMessageRequest, TextMessage
                await self._api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=result)],
                    )
                )
            except Exception as e:
                logger.error("line_postback_reply_error", error=str(e))

    async def send_notification(self, user_id: str, message: str) -> bool:
        """Send a proactive notification to a LINE user."""
        if self._api is None:
            return False
        try:
            from linebot.v3.messaging import PushMessageRequest, TextMessage

            max_len = self.config.adapters.line.max_message_length
            chunks = split_message(message, max_len)
            for chunk in chunks:
                await self._api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=chunk)],
                    )
                )
            logger.info("line_notification_sent", user_id=user_id)
            return True
        except Exception as e:
            logger.error("line_notification_failed", error=str(e), user_id=user_id)
            return False
