"""OpenAI OAuth helper for Web UI login and per-browser token storage.

Implements Authorization Code + PKCE flow with in-memory token/session state.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp
import structlog
from fastapi import Request
from starlette.responses import Response

logger = structlog.get_logger()


@dataclass
class _PendingAuth:
    session_id: str
    code_verifier: str
    redirect_uri: str
    created_at: float


@dataclass
class _OAuthSession:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    token_type: str
    scope: str
    expires_at: float | None
    created_at: float
    userinfo: dict[str, Any]


class OpenAIOAuthManager:
    """Stateful OpenAI OAuth manager (server-side in-memory sessions)."""

    def __init__(self) -> None:
        self.client_id = os.environ.get("OPENAI_OAUTH_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("OPENAI_OAUTH_CLIENT_SECRET", "").strip()
        self.scope = os.environ.get(
            "OPENAI_OAUTH_SCOPE",
            "openid profile email offline_access",
        ).strip()
        self.audience = os.environ.get("OPENAI_OAUTH_AUDIENCE", "").strip()
        self.redirect_uri_env = os.environ.get("OPENAI_OAUTH_REDIRECT_URI", "").strip()
        self.redirect_path = os.environ.get(
            "OPENAI_OAUTH_REDIRECT_PATH",
            "/api/oauth/openai/callback",
        ).strip()
        self.auth_url = os.environ.get(
            "OPENAI_OAUTH_AUTH_URL",
            "https://auth.openai.com/authorize",
        ).strip()
        self.token_url = os.environ.get(
            "OPENAI_OAUTH_TOKEN_URL",
            "https://auth0.openai.com/oauth/token",
        ).strip()
        self.userinfo_url = os.environ.get(
            "OPENAI_OAUTH_USERINFO_URL",
            "https://auth0.openai.com/userinfo",
        ).strip()
        self.prompt = os.environ.get("OPENAI_OAUTH_PROMPT", "").strip()
        self.cookie_name = os.environ.get(
            "OPENAI_OAUTH_COOKIE_NAME",
            "kuro_openai_oauth_session",
        ).strip()
        self.cookie_secure = os.environ.get("OPENAI_OAUTH_COOKIE_SECURE", "").strip() == "1"

        self._pending: dict[str, _PendingAuth] = {}
        self._sessions: dict[str, _OAuthSession] = {}
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    def get_session_id_from_cookies(self, cookies: dict[str, str] | None) -> str | None:
        if not cookies:
            return None
        sid = (cookies.get(self.cookie_name) or "").strip()
        return sid or None

    def get_or_create_session_id(self, request: Request) -> str:
        sid = self.get_session_id_from_cookies(request.cookies)
        if sid:
            return sid
        return secrets.token_urlsafe(24)

    def attach_cookie(self, response: Response, session_id: str) -> None:
        response.set_cookie(
            key=self.cookie_name,
            value=session_id,
            httponly=True,
            secure=self.cookie_secure,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,  # 30 days
            path="/",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(self.cookie_name, path="/")

    def _redirect_uri(self, request: Request) -> str:
        if self.redirect_uri_env:
            return self.redirect_uri_env
        base = str(request.base_url).rstrip("/")
        if not self.redirect_path.startswith("/"):
            return f"{base}/{self.redirect_path}"
        return f"{base}{self.redirect_path}"

    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return verifier, challenge

    def _prune(self) -> None:
        now = time.time()
        # pending auth state expires quickly
        for state, pending in list(self._pending.items()):
            if now - pending.created_at > 600:
                self._pending.pop(state, None)
        # stale sessions without refresh token can be dropped after expiry grace period
        for sid, sess in list(self._sessions.items()):
            if sess.expires_at and now > sess.expires_at + 3600 and not sess.refresh_token:
                self._sessions.pop(sid, None)

    def build_login_url(self, request: Request, session_id: str) -> str:
        if not self.configured:
            raise RuntimeError("OPENAI_OAUTH_CLIENT_ID is not configured")

        self._prune()
        verifier, challenge = self._pkce_pair()
        state = secrets.token_urlsafe(24)
        redirect_uri = self._redirect_uri(request)

        self._pending[state] = _PendingAuth(
            session_id=session_id,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            created_at=time.time(),
        )

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if self.audience:
            params["audience"] = self.audience
        if self.prompt:
            params["prompt"] = self.prompt

        return f"{self.auth_url}?{urlencode(params)}"

    async def _refresh_access_token(self, session_id: str, session: _OAuthSession) -> str | None:
        if not session.refresh_token:
            return None

        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": session.refresh_token,
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        if self.audience:
            payload["audience"] = self.audience

        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(self.token_url, data=payload) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    logger.warning("openai_oauth_refresh_failed", status=resp.status, body=body)
                    return None

        now = time.time()
        access_token = str(body.get("access_token", "")).strip()
        if not access_token:
            return None

        expires_in = body.get("expires_in")
        expires_at = None
        if isinstance(expires_in, (int, float)):
            expires_at = now + float(expires_in)

        self._sessions[session_id] = _OAuthSession(
            access_token=access_token,
            refresh_token=str(body.get("refresh_token") or session.refresh_token or "").strip() or None,
            id_token=str(body.get("id_token") or session.id_token or "").strip() or None,
            token_type=str(body.get("token_type", "Bearer")).strip() or "Bearer",
            scope=str(body.get("scope", session.scope or "")).strip(),
            expires_at=expires_at,
            created_at=now,
            userinfo=dict(session.userinfo),
        )
        return access_token

    async def _fetch_userinfo(self, access_token: str) -> dict[str, Any]:
        if not self.userinfo_url:
            return {}
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.get(self.userinfo_url, headers=headers) as resp:
                    if resp.status != 200:
                        return {}
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        return data
        except Exception:
            return {}
        return {}

    async def exchange_code(
        self,
        *,
        request: Request,
        session_id: str,
        state: str,
        code: str,
    ) -> None:
        if not self.configured:
            raise RuntimeError("OPENAI OAuth is not configured")

        async with self._lock:
            self._prune()
            pending = self._pending.pop(state, None)
            if pending is None:
                raise ValueError("Invalid OAuth state")
            if pending.session_id != session_id:
                raise ValueError("OAuth session mismatch")
            if time.time() - pending.created_at > 600:
                raise ValueError("OAuth state expired")

            payload = {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "code": code,
                "redirect_uri": pending.redirect_uri or self._redirect_uri(request),
                "code_verifier": pending.code_verifier,
            }
            if self.client_secret:
                payload["client_secret"] = self.client_secret
            if self.audience:
                payload["audience"] = self.audience

            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.post(self.token_url, data=payload) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status != 200:
                        logger.warning(
                            "openai_oauth_token_exchange_failed",
                            status=resp.status,
                            body=body,
                        )
                        msg = body.get("error_description") or body.get("error") or "Token exchange failed"
                        raise RuntimeError(str(msg))

            now = time.time()
            access_token = str(body.get("access_token", "")).strip()
            if not access_token:
                raise RuntimeError("No access_token returned by OAuth provider")

            expires_in = body.get("expires_in")
            expires_at = None
            if isinstance(expires_in, (int, float)):
                expires_at = now + float(expires_in)

            userinfo = await self._fetch_userinfo(access_token)

            self._sessions[session_id] = _OAuthSession(
                access_token=access_token,
                refresh_token=str(body.get("refresh_token", "")).strip() or None,
                id_token=str(body.get("id_token", "")).strip() or None,
                token_type=str(body.get("token_type", "Bearer")).strip() or "Bearer",
                scope=str(body.get("scope", self.scope)).strip(),
                expires_at=expires_at,
                created_at=now,
                userinfo=userinfo,
            )

    def get_status(self, session_id: str | None) -> dict[str, Any]:
        self._prune()
        if not self.configured:
            return {
                "configured": False,
                "logged_in": False,
            }
        if not session_id:
            return {
                "configured": True,
                "logged_in": False,
            }
        sess = self._sessions.get(session_id)
        if not sess:
            return {
                "configured": True,
                "logged_in": False,
            }
        now = time.time()
        token_valid = bool(
            sess.access_token and (sess.expires_at is None or now < sess.expires_at)
        )
        can_refresh = bool(sess.refresh_token)
        expires_in = None
        if sess.expires_at:
            expires_in = int(max(0, sess.expires_at - now))
        email = str(sess.userinfo.get("email", "")).strip() or None
        return {
            "configured": True,
            "logged_in": token_valid or can_refresh,
            "scope": sess.scope,
            "expires_in": expires_in,
            "email": email,
        }

    async def get_access_token(self, session_id: str | None) -> str | None:
        if not self.configured or not session_id:
            return None

        async with self._lock:
            self._prune()
            sess = self._sessions.get(session_id)
            if not sess:
                return None

            now = time.time()
            if sess.expires_at and now >= (sess.expires_at - 30):
                refreshed = await self._refresh_access_token(session_id, sess)
                if not refreshed:
                    self._sessions.pop(session_id, None)
                return refreshed
            return sess.access_token

    async def logout(self, session_id: str | None) -> None:
        if not session_id:
            return
        async with self._lock:
            self._sessions.pop(session_id, None)
            self._prune()
