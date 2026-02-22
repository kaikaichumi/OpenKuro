"""Email adapter: IMAP receive (IDLE) + SMTP send.

Features:
- IMAP IDLE for real-time new email detection (like long-polling)
- Subject-based session tracking (same subject = same conversation)
- Plain text + HTML email formatting for responses
- Approval via email reply keywords ("approve" / "deny")
- User whitelist via allowed_senders (empty = allow all)
- SMTP send for notifications and approval requests

Setup:
1. Set KURO_EMAIL_ADDRESS and KURO_EMAIL_PASSWORD env vars
2. For Gmail: enable App Passwords (not regular password)
3. Configure imap_host/smtp_host in config.yaml if not using Gmail
"""

from __future__ import annotations

import asyncio
import email as email_lib
import hashlib
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
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

DEFAULT_APPROVAL_TIMEOUT = 300  # 5 minutes for email


class EmailApprovalCallback(ApprovalCallback):
    """Approval via email reply. User replies with 'approve' or 'deny'."""

    def __init__(self, approval_policy=None) -> None:
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._send_fn = None  # async send_email function, set by EmailAdapter
        self._email_map: dict[str, str] = {}  # session_id -> sender email
        self._timeout: int = DEFAULT_APPROVAL_TIMEOUT
        self.approval_policy = approval_policy

    def set_send_fn(self, fn) -> None:
        self._send_fn = fn

    def register_sender(self, session_id: str, email: str) -> None:
        self._email_map[session_id] = email

    async def request_approval(
        self,
        tool_name: str,
        params: dict[str, Any],
        risk_level: RiskLevel,
        session: Session,
    ) -> bool:
        if self._send_fn is None:
            return False

        sender_email = self._email_map.get(session.id)
        if not sender_email:
            return False

        approval_id = str(uuid4())[:8]

        risk_emoji = {
            RiskLevel.LOW: "✅",
            RiskLevel.MEDIUM: "⚠️",
            RiskLevel.HIGH: "🔴",
            RiskLevel.CRITICAL: "☢️",
        }
        emoji = risk_emoji.get(risk_level, "❓")

        import json as _json
        try:
            params_text = _json.dumps(params, ensure_ascii=False, indent=2)
        except Exception:
            params_text = str(params)

        subject = f"[Kuro] Approval Required: {tool_name} [#{approval_id}]"
        body = (
            f"Kuro needs your approval to proceed.\n\n"
            f"Tool: {tool_name}\n"
            f"Risk: {emoji} {risk_level.value.upper()}\n\n"
            f"Parameters:\n{params_text}\n\n"
            f"--- Reply Instructions ---\n"
            f"Reply to this email with ONE of:\n"
            f"  approve   — Allow the action\n"
            f"  deny      — Block the action\n\n"
            f"Approval ID: #{approval_id}\n"
            f"This request expires in {self._timeout // 60} minutes."
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[approval_id] = future
        self._pending[f"session:{approval_id}"] = session  # type: ignore[assignment]

        try:
            await self._send_fn(to=sender_email, subject=subject, body=body)
        except Exception as e:
            logger.error("email_approval_send_failed", error=str(e))
            self._pending.pop(approval_id, None)
            self._pending.pop(f"session:{approval_id}", None)
            return False

        try:
            result = await asyncio.wait_for(future, timeout=self._timeout)
            return result
        except asyncio.TimeoutError:
            try:
                await self._send_fn(
                    to=sender_email,
                    subject=f"[Kuro] Approval Expired: {tool_name}",
                    body=f"The approval request for `{tool_name}` (#{approval_id}) has expired. Action denied.",
                )
            except Exception:
                pass
            return False
        finally:
            self._pending.pop(approval_id, None)
            self._pending.pop(f"session:{approval_id}", None)

    def try_resolve_from_reply(self, subject: str, body: str, sender: str) -> bool:
        """Check if an email body is an approval reply. Returns True if resolved."""
        # Extract approval_id from subject "[#abc123]"
        match = re.search(r"\[#([a-f0-9]+)\]", subject, re.IGNORECASE)
        if not match:
            return False

        approval_id = match.group(1)
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return False

        # Parse the first non-empty, non-quote line of the body
        body_lower = body.lower()
        # Strip quoted lines (lines starting with >)
        clean_lines = [
            line.strip()
            for line in body_lower.splitlines()
            if line.strip() and not line.strip().startswith(">")
        ]
        decision = clean_lines[0] if clean_lines else ""

        if "approve" in decision:
            session = self._pending.get(f"session:{approval_id}")
            if session and self.approval_policy:
                pass  # Could elevate trust here
            future.set_result(True)
            return True
        elif "deny" in decision or "reject" in decision or "no" == decision:
            future.set_result(False)
            return True

        return False


class EmailAdapter(BaseAdapter):
    """Email adapter: IMAP IDLE receive + SMTP send."""

    name = "email"

    def __init__(self, engine: Engine, config: KuroConfig) -> None:
        super().__init__(engine, config)
        self._imap = None
        self._running = False
        self._idle_task: asyncio.Task | None = None
        self._approval_cb = EmailApprovalCallback(
            approval_policy=engine.approval_policy
        )
        self._approval_cb.set_send_fn(self._send_email)
        self.engine.approval_cb = self._approval_cb
        self._seen_uids: set[str] = set()

    async def start(self) -> None:
        import aioimaplib

        email_addr = self.config.adapters.email.get_email()
        password = self.config.adapters.email.get_password()

        if not email_addr:
            raise ValueError(
                f"Email address not set. "
                f"Set {self.config.adapters.email.email_env} environment variable."
            )
        if not password:
            raise ValueError(
                f"Email password not set. "
                f"Set {self.config.adapters.email.password_env} environment variable."
            )

        self._imap = aioimaplib.IMAP4_SSL(
            host=self.config.adapters.email.imap_host,
            port=self.config.adapters.email.imap_port,
        )
        await self._imap.wait_hello_from_server()
        await self._imap.login(email_addr, password)
        await self._imap.select("INBOX")

        # Mark existing emails as seen so we don't process old ones on restart
        await self._mark_existing_as_seen()

        self._running = True
        self._idle_task = asyncio.create_task(self._idle_loop())

        logger.info(
            "email_started",
            email=email_addr,
            imap_host=self.config.adapters.email.imap_host,
        )
        print(f"Email adapter started: monitoring {email_addr}")

    async def stop(self) -> None:
        self._running = False
        if self._idle_task:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
        if self._imap:
            try:
                await self._imap.logout()
            except Exception:
                pass
        logger.info("email_stopped")

    async def _mark_existing_as_seen(self) -> None:
        """Mark all current UNSEEN messages as already processed to avoid processing old emails."""
        try:
            _, data = await self._imap.search("UNSEEN")
            if data and data[0]:
                uid_list = data[0].decode().split()
                self._seen_uids.update(uid_list)
        except Exception as e:
            logger.debug("email_mark_seen_error", error=str(e))

    async def _idle_loop(self) -> None:
        """Main loop: use IMAP IDLE to wait for new emails efficiently."""
        import aioimaplib

        idle_timeout = min(self.config.adapters.email.check_interval * 29, 840)  # max 14 min

        while self._running:
            try:
                # Start IDLE
                await self._imap.idle_start(timeout=idle_timeout)

                # Wait for server push or timeout
                try:
                    await asyncio.wait_for(
                        self._imap.wait_server_push(),
                        timeout=idle_timeout + 5,
                    )
                except asyncio.TimeoutError:
                    pass  # Normal: re-enter IDLE after timeout

                # Stop IDLE and process any new mail
                self._imap.idle_done()
                await asyncio.sleep(0.5)  # Small delay for server to settle
                await self._process_new_emails()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("email_idle_error", error=str(e))
                # Reconnect after error
                await asyncio.sleep(10)
                try:
                    await self._reconnect()
                except Exception as reconnect_err:
                    logger.error("email_reconnect_failed", error=str(reconnect_err))
                    await asyncio.sleep(30)

    async def _reconnect(self) -> None:
        """Reconnect IMAP after an error."""
        import aioimaplib

        try:
            if self._imap:
                await self._imap.logout()
        except Exception:
            pass

        email_addr = self.config.adapters.email.get_email()
        password = self.config.adapters.email.get_password()

        self._imap = aioimaplib.IMAP4_SSL(
            host=self.config.adapters.email.imap_host,
            port=self.config.adapters.email.imap_port,
        )
        await self._imap.wait_hello_from_server()
        await self._imap.login(email_addr, password)
        await self._imap.select("INBOX")
        logger.info("email_reconnected")

    async def _process_new_emails(self) -> None:
        """Fetch and process UNSEEN emails."""
        try:
            _, data = await self._imap.search("UNSEEN")
            if not data or not data[0]:
                return

            uid_list = data[0].decode().split()

            for uid in uid_list:
                if uid in self._seen_uids:
                    continue
                self._seen_uids.add(uid)

                try:
                    _, msg_data = await self._imap.fetch(uid, "(RFC822)")
                    if not msg_data or not msg_data[1]:
                        continue

                    raw = msg_data[1]
                    if isinstance(raw, (list, tuple)):
                        raw = raw[0] if raw else b""

                    msg = email_lib.message_from_bytes(raw if isinstance(raw, bytes) else raw.encode())
                    await self._handle_email(msg, uid)
                except Exception as e:
                    logger.error("email_fetch_error", uid=uid, error=str(e))

        except Exception as e:
            logger.error("email_process_error", error=str(e))

    async def _handle_email(self, msg, uid: str) -> None:
        """Handle a single email message."""
        sender = self._parse_sender(msg.get("From", ""))
        subject = self._decode_header(msg.get("Subject", ""))
        body = self._extract_body(msg)

        if not sender:
            return

        if not self._is_sender_allowed(sender):
            logger.debug("email_sender_blocked", sender=sender)
            return

        logger.info("email_received", sender=sender, subject=subject)

        # Check if this is an approval reply
        if self._approval_cb.try_resolve_from_reply(subject, body, sender):
            logger.info("email_approval_resolved", sender=sender)
            return

        # Quick dashboard commands via subject keywords
        cmd_response = await self._try_email_command(subject)
        if cmd_response is not None:
            reply_subject = f"Re: {subject}"
            await self._send_email(
                to=sender,
                subject=reply_subject,
                body=cmd_response,
                html=self._markdown_to_html(cmd_response),
            )
            return

        # Regular message — find or create session by normalized subject
        session_key = self._session_key(sender, subject)
        session = self.get_or_create_session(session_key)
        self._approval_cb.register_sender(session.id, sender)

        model = session.metadata.get("model_override")

        try:
            response = await self.engine.process_message(body, session, model=model)

            # Send response via email
            reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
            await self._send_email(
                to=sender,
                subject=reply_subject,
                body=response,
                html=self._markdown_to_html(response),
            )

        except Exception as e:
            logger.error("email_process_error", sender=sender, error=str(e))
            try:
                await self._send_email(
                    to=sender,
                    subject=f"Re: {subject}",
                    body=f"Error processing your request: {str(e)[:300]}",
                )
            except Exception:
                pass

    async def _send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html: str | None = None,
    ) -> None:
        """Send an email via SMTP."""
        import aiosmtplib

        from_addr = self.config.adapters.email.get_email()
        password = self.config.adapters.email.get_password()

        if html:
            msg = MIMEMultipart("alternative")
            msg["From"] = from_addr
            msg["To"] = to
            msg["Subject"] = subject
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid()
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")
            msg["From"] = from_addr
            msg["To"] = to
            msg["Subject"] = subject
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid()

        await aiosmtplib.send(
            msg,
            hostname=self.config.adapters.email.smtp_host,
            port=self.config.adapters.email.smtp_port,
            username=from_addr,
            password=password,
            start_tls=True,
        )

    async def _try_email_command(self, subject: str) -> str | None:
        """Check if email subject contains a quick command keyword."""
        normalized = self._normalize_subject(subject).strip().lower()
        max_chars = self.config.adapters.email.max_message_length - 100
        if normalized in ("[stats]", "stats"):
            from src.adapters.dashboard_commands import handle_stats_command
            return await handle_stats_command(max_chars)
        if normalized in ("[costs]", "costs"):
            from src.adapters.dashboard_commands import handle_costs_command
            return await handle_costs_command(max_chars)
        if normalized in ("[security]", "security"):
            from src.adapters.dashboard_commands import handle_security_command
            return await handle_security_command(max_chars)
        return None

    def _session_key(self, sender: str, subject: str) -> str:
        """Generate session key from sender + normalized subject."""
        normalized = self._normalize_subject(subject)
        # Hash to keep key short and filesystem-safe
        key_data = f"{sender}:{normalized}".lower()
        return hashlib.md5(key_data.encode()).hexdigest()[:16]

    def _normalize_subject(self, subject: str) -> str:
        """Strip Re:/Fwd: prefixes for session continuity."""
        return re.sub(
            r"^(Re|Fwd|FW|RE|Fw)\s*:\s*",
            "",
            subject,
            flags=re.IGNORECASE,
        ).strip()

    def _is_sender_allowed(self, sender: str) -> bool:
        allowed = self.config.adapters.email.allowed_senders
        if not allowed:
            return True
        sender_lower = sender.lower()
        return any(a.lower() == sender_lower for a in allowed)

    def _parse_sender(self, from_header: str) -> str:
        """Extract email address from From header."""
        match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", from_header)
        return match.group(0) if match else ""

    def _decode_header(self, header: str) -> str:
        """Decode email header (handles encoded words)."""
        from email.header import decode_header as _decode
        parts = []
        for part, charset in _decode(header):
            if isinstance(part, bytes):
                parts.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(str(part))
        return " ".join(parts)

    def _extract_body(self, msg) -> str:
        """Extract plain text body from email message."""
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain" and not part.get("Content-Disposition"):
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode(charset, errors="replace")
                        break
        else:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(charset, errors="replace")

        return body.strip()

    def _markdown_to_html(self, text: str) -> str:
        """Convert basic markdown to HTML for email."""
        import html as html_lib
        escaped = html_lib.escape(text)
        # Bold
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        # Italic
        escaped = re.sub(r"\*(.+?)\*", r"<em>\1</em>", escaped)
        # Code
        escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
        # Line breaks
        escaped = escaped.replace("\n", "<br>\n")
        return f"<html><body style='font-family:sans-serif'>{escaped}</body></html>"

    async def send_notification(self, user_id: str, message: str) -> bool:
        """Send a notification email. user_id is the recipient email address."""
        try:
            await self._send_email(
                to=user_id,
                subject="[Kuro] Notification",
                body=message,
                html=self._markdown_to_html(message),
            )
            logger.info("email_notification_sent", to=user_id)
            return True
        except Exception as e:
            logger.error("email_notification_failed", error=str(e), to=user_id)
            return False
