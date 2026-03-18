"""ChatGPT/Codex OAuth helper for Web UI login and subscription tokens.

Uses the same public OAuth flow as Codex CLI:
- authorize: https://auth.openai.com/oauth/authorize
- token:     https://auth.openai.com/oauth/token
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
from aiohttp import web as aiohttp_web
import structlog
from fastapi import Request
from starlette.responses import Response

logger = structlog.get_logger()

# Matches Codex CLI's public OAuth client id (see openai/codex source).
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_SCOPE_DEFAULT = "openid profile email offline_access"


@dataclass
class _PendingAuth:
    session_id: str
    code_verifier: str
    redirect_uri: str
    return_uri: str
    created_at: float


@dataclass
class _OAuthSession:
    access_token: str
    refresh_token: str
    id_token: str | None
    scope: str
    expires_at: float | None
    created_at: float
    account_id: str | None
    plan_type: str | None
    email: str | None


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_auth_claims(token: str) -> tuple[str | None, str | None, str | None]:
    payload = _decode_jwt_payload(token)
    auth_claim = payload.get("https://api.openai.com/auth")
    profile_claim = payload.get("https://api.openai.com/profile")

    account_id: str | None = None
    plan_type: str | None = None
    email: str | None = None

    if isinstance(auth_claim, dict):
        account_id = str(auth_claim.get("chatgpt_account_id") or "").strip() or None
        plan_type = str(auth_claim.get("chatgpt_plan_type") or "").strip() or None
        email = str(auth_claim.get("email") or "").strip() or None
        if not email:
            email = str(auth_claim.get("user_email") or "").strip() or None

    if not email and isinstance(profile_claim, dict):
        email = str(profile_claim.get("email") or "").strip() or None
    if not email:
        email = str(payload.get("email") or "").strip() or None

    return account_id, plan_type, email


class OpenAIOAuthManager:
    """OpenAI OAuth manager for ChatGPT Codex subscription auth."""

    def __init__(self) -> None:
        self.client_id = os.environ.get("OPENAI_CODEX_OAUTH_CLIENT_ID", _CODEX_CLIENT_ID).strip()
        self.scope = os.environ.get(
            "OPENAI_CODEX_OAUTH_SCOPE",
            _CODEX_SCOPE_DEFAULT,
        ).strip()
        self.redirect_uri_env = os.environ.get("OPENAI_CODEX_OAUTH_REDIRECT_URI", "").strip()
        self.redirect_path = os.environ.get(
            "OPENAI_CODEX_OAUTH_REDIRECT_PATH",
            "/auth/callback",
        ).strip()
        self.force_localhost_redirect = (
            os.environ.get("OPENAI_CODEX_OAUTH_FORCE_LOCALHOST", "1")
            .strip()
            .lower()
            not in {"0", "false", "no", "off"}
        )
        self.auth_url = os.environ.get(
            "OPENAI_CODEX_OAUTH_AUTH_URL",
            "https://auth.openai.com/oauth/authorize",
        ).strip()
        self.token_url = os.environ.get(
            "OPENAI_CODEX_OAUTH_TOKEN_URL",
            "https://auth.openai.com/oauth/token",
        ).strip()
        self.originator = os.environ.get(
            "OPENAI_CODEX_OAUTH_ORIGINATOR",
            "codex_cli_rs",
        ).strip()
        self.cookie_name = os.environ.get(
            "OPENAI_OAUTH_COOKIE_NAME",
            "kuro_openai_oauth_session",
        ).strip()
        self.cookie_secure = os.environ.get("OPENAI_OAUTH_COOKIE_SECURE", "").strip() == "1"
        self.bridge_enabled = (
            os.environ.get("OPENAI_CODEX_OAUTH_LOCAL_BRIDGE", "1")
            .strip()
            .lower()
            not in {"0", "false", "no", "off"}
        )
        self.bridge_host = os.environ.get("OPENAI_CODEX_OAUTH_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
        bridge_port_raw = os.environ.get("OPENAI_CODEX_OAUTH_BRIDGE_PORT", "1455").strip()
        try:
            self.bridge_port = int(bridge_port_raw)
        except Exception:
            self.bridge_port = 1455
        self.bridge_path = os.environ.get("OPENAI_CODEX_OAUTH_BRIDGE_PATH", "/auth/callback").strip() or "/auth/callback"
        if not self.bridge_path.startswith("/"):
            self.bridge_path = f"/{self.bridge_path}"

        self._pending: dict[str, _PendingAuth] = {}
        self._sessions: dict[str, _OAuthSession] = {}
        self._session_store_path = self._oauth_session_store_path()
        self._load_sessions_from_disk()
        self._lock = asyncio.Lock()
        self._bridge_runner: aiohttp_web.AppRunner | None = None
        self._bridge_site: aiohttp_web.BaseSite | None = None
        self._bridge_started: bool = False
        self._bridge_failed: bool = False

    @property
    def configured(self) -> bool:
        # Subscription flow uses a fixed public client id; no local secret is needed.
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
            max_age=60 * 60 * 24 * 30,
            path="/",
        )

    def _redirect_uri(self, request: Request) -> str:
        if self.redirect_uri_env:
            return self.redirect_uri_env

        path = self.redirect_path if self.redirect_path.startswith("/") else f"/{self.redirect_path}"
        url = request.url
        scheme = (url.scheme or "http").strip().lower()
        host = (url.hostname or "").strip().lower()
        port = url.port

        if not host:
            host = "localhost"
        if host in {"127.0.0.1", "::1"}:
            host = "localhost"
        elif self.force_localhost_redirect:
            # Public Codex OAuth client is localhost-oriented; force this by default.
            host = "localhost"

        # Codex login server uses plain http localhost callback.
        if host == "localhost":
            scheme = "http"

        if port is None:
            port = 443 if scheme == "https" else 80
        default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        netloc = host if default_port else f"{host}:{port}"
        return f"{scheme}://{netloc}{path}"

    def _bridge_redirect_uri(self) -> str:
        host = "localhost"
        port = self.bridge_port
        netloc = host if port == 80 else f"{host}:{port}"
        return f"http://{netloc}{self.bridge_path}"

    async def _bridge_callback(self, request: aiohttp_web.Request) -> aiohttp_web.StreamResponse:
        state = str(request.query.get("state", "")).strip()
        target = ""
        if state:
            pending = self._pending.get(state)
            if pending and pending.return_uri:
                target = pending.return_uri
        if target:
            qs = request.query_string
            sep = "&" if "?" in target else "?"
            location = f"{target}{sep}{qs}" if qs else target
            raise aiohttp_web.HTTPFound(location=location)
        return aiohttp_web.Response(
            text="OAuth callback received. Return to the app and retry sign-in.",
            content_type="text/plain",
        )

    async def ensure_local_bridge(self) -> bool:
        if not self.bridge_enabled:
            return False
        if self._bridge_started:
            return True
        if self._bridge_failed:
            return False
        async with self._lock:
            if self._bridge_started:
                return True
            if self._bridge_failed:
                return False
            app = aiohttp_web.Application()
            app.router.add_get(self.bridge_path, self._bridge_callback)
            runner = aiohttp_web.AppRunner(app, access_log=None)
            await runner.setup()
            site = aiohttp_web.TCPSite(runner, host=self.bridge_host, port=self.bridge_port)
            try:
                await site.start()
            except Exception as e:
                self._bridge_failed = True
                with contextlib.suppress(Exception):
                    await runner.cleanup()
                logger.warning(
                    "openai_oauth_bridge_start_failed",
                    host=self.bridge_host,
                    port=self.bridge_port,
                    path=self.bridge_path,
                    error=str(e),
                )
                return False

            self._bridge_runner = runner
            self._bridge_site = site
            self._bridge_started = True
            logger.info(
                "openai_oauth_bridge_started",
                host=self.bridge_host,
                port=self.bridge_port,
                path=self.bridge_path,
            )
            return True

    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return verifier, challenge

    @staticmethod
    def _codex_home() -> Path:
        home = os.environ.get("CODEX_HOME", "")
        if home.strip():
            return Path(home).expanduser()
        return Path.home() / ".codex"

    def _oauth_session_store_path(self) -> Path:
        raw = os.environ.get("OPENAI_OAUTH_SESSION_FILE", "").strip()
        if raw:
            return Path(raw).expanduser()
        return self._codex_home() / "openai_oauth_sessions.json"

    @staticmethod
    def _session_to_dict(sess: _OAuthSession) -> dict[str, Any]:
        return {
            "access_token": sess.access_token,
            "refresh_token": sess.refresh_token,
            "id_token": sess.id_token,
            "scope": sess.scope,
            "expires_at": sess.expires_at,
            "created_at": sess.created_at,
            "account_id": sess.account_id,
            "plan_type": sess.plan_type,
            "email": sess.email,
        }

    @staticmethod
    def _session_from_dict(data: dict[str, Any]) -> _OAuthSession | None:
        if not isinstance(data, dict):
            return None

        access_token = str(data.get("access_token", "")).strip()
        refresh_token = str(data.get("refresh_token", "")).strip()
        if not access_token or not refresh_token:
            return None

        id_token_raw = data.get("id_token")
        id_token = str(id_token_raw).strip() if isinstance(id_token_raw, str) else None
        scope = str(data.get("scope", _CODEX_SCOPE_DEFAULT)).strip() or _CODEX_SCOPE_DEFAULT

        expires_at_raw = data.get("expires_at")
        expires_at: float | None = None
        if isinstance(expires_at_raw, (int, float)):
            expires_at = float(expires_at_raw)

        created_at_raw = data.get("created_at")
        if isinstance(created_at_raw, (int, float)):
            created_at = float(created_at_raw)
        else:
            created_at = time.time()

        account_raw = data.get("account_id")
        plan_raw = data.get("plan_type")
        email_raw = data.get("email")
        account_id = str(account_raw).strip() if isinstance(account_raw, str) else ""
        plan_type = str(plan_raw).strip() if isinstance(plan_raw, str) else ""
        email = str(email_raw).strip() if isinstance(email_raw, str) else ""
        account_id = account_id or None
        plan_type = plan_type or None
        email = email or None

        return _OAuthSession(
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            scope=scope,
            expires_at=expires_at,
            created_at=created_at,
            account_id=account_id,
            plan_type=plan_type,
            email=email,
        )

    def _load_sessions_from_disk(self) -> None:
        path = self._session_store_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            sessions_raw = raw.get("sessions", raw)
            if not isinstance(sessions_raw, dict):
                return

            loaded = 0
            for sid_raw, payload in sessions_raw.items():
                sid = str(sid_raw).strip()
                if not sid:
                    continue
                sess = self._session_from_dict(payload) if isinstance(payload, dict) else None
                if sess:
                    self._sessions[sid] = sess
                    loaded += 1

            if loaded > 0:
                logger.info(
                    "openai_oauth_sessions_loaded",
                    count=loaded,
                    path=str(path),
                )

            if self._prune():
                self._persist_sessions_to_disk()
        except Exception as e:
            logger.warning(
                "openai_oauth_sessions_load_failed",
                path=str(path),
                error=str(e),
            )

    def _persist_sessions_to_disk(self) -> None:
        path = self._session_store_path
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "sessions": {
                    sid: self._session_to_dict(sess)
                    for sid, sess in self._sessions.items()
                },
            }
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
            if os.name != "nt":
                with contextlib.suppress(Exception):
                    path.chmod(0o600)
        except Exception as e:
            with contextlib.suppress(Exception):
                tmp_path.unlink(missing_ok=True)
            logger.warning(
                "openai_oauth_sessions_persist_failed",
                path=str(path),
                error=str(e),
            )

    def _prune(self) -> bool:
        changed = False
        now = time.time()
        for state, pending in list(self._pending.items()):
            if now - pending.created_at > 600:
                self._pending.pop(state, None)
                changed = True
        for sid, sess in list(self._sessions.items()):
            if sess.expires_at and now > sess.expires_at + 3600 and not sess.refresh_token:
                self._sessions.pop(sid, None)
                changed = True
        return changed

    def build_login_url(self, request: Request, session_id: str) -> str:
        if not self.configured:
            raise RuntimeError("OpenAI Codex OAuth is not configured")

        self._prune()
        verifier, challenge = self._pkce_pair()
        state = secrets.token_urlsafe(24)
        return_uri = self._redirect_uri(request)
        redirect_uri = self._bridge_redirect_uri() if self._bridge_started else return_uri

        self._pending[state] = _PendingAuth(
            session_id=session_id,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            return_uri=return_uri,
            created_at=time.time(),
        )

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            # Follow codex CLI/openclaw flow params.
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": self.originator or "codex_cli_rs",
        }
        logger.info(
            "openai_oauth_authorize_url_built",
            redirect_uri=redirect_uri,
            return_uri=return_uri,
            scope=self.scope,
            bridge=self._bridge_started,
        )
        return f"{self.auth_url}?{urlencode(params)}"

    async def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(self.token_url, data=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    logger.warning(
                        "openai_codex_oauth_token_request_failed",
                        status=resp.status,
                        body=body,
                    )
                    msg = body.get("error_description") or body.get("error") or "Token request failed"
                    raise RuntimeError(str(msg))
                if not isinstance(body, dict):
                    raise RuntimeError("Invalid token response")
                return body

    def _upsert_session(self, session_id: str, body: dict[str, Any]) -> _OAuthSession:
        now = time.time()
        access_token = str(body.get("access_token", "")).strip()
        refresh_token = str(body.get("refresh_token", "")).strip()
        id_token = str(body.get("id_token", "")).strip() or None
        scope = str(body.get("scope", self.scope)).strip()
        expires_in = body.get("expires_in")
        expires_at = None
        if isinstance(expires_in, (int, float)):
            expires_at = now + float(expires_in)

        if not access_token or not refresh_token:
            raise RuntimeError("OAuth token response missing access_token/refresh_token")

        account_id, plan_type, email = _extract_auth_claims(id_token or access_token)
        sess = _OAuthSession(
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            scope=scope,
            expires_at=expires_at,
            created_at=now,
            account_id=account_id,
            plan_type=plan_type,
            email=email,
        )
        self._sessions[session_id] = sess
        self._persist_sessions_to_disk()
        return sess

    async def exchange_code(
        self,
        *,
        request: Request,
        session_id: str,
        state: str,
        code: str,
    ) -> None:
        if not self.configured:
            raise RuntimeError("OpenAI Codex OAuth is not configured")

        async with self._lock:
            self._prune()
            pending = self._pending.pop(state, None)
            if pending is None:
                raise ValueError("Invalid OAuth state")
            if pending.session_id != session_id:
                raise ValueError("OAuth session mismatch")
            if time.time() - pending.created_at > 600:
                raise ValueError("OAuth state expired")

            body = await self._token_request({
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "code": code,
                "code_verifier": pending.code_verifier,
                "redirect_uri": pending.redirect_uri or self._redirect_uri(request),
            })
            self._upsert_session(session_id, body)

    def _load_from_codex_auth_file(self) -> _OAuthSession | None:
        auth_file = self._codex_home() / "auth.json"
        if not auth_file.exists():
            return None
        try:
            raw = json.loads(auth_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            tokens = raw.get("tokens")
            if not isinstance(tokens, dict):
                return None
            access_token = str(tokens.get("access_token", "")).strip()
            refresh_token = str(tokens.get("refresh_token", "")).strip()
            if not access_token or not refresh_token:
                return None
            id_token_raw = tokens.get("id_token")
            id_token = str(id_token_raw).strip() if isinstance(id_token_raw, str) else None
            account_id, plan_type, email = _extract_auth_claims(id_token or access_token)
            return _OAuthSession(
                access_token=access_token,
                refresh_token=refresh_token,
                id_token=id_token,
                scope=self.scope,
                expires_at=None,
                created_at=time.time(),
                account_id=account_id,
                plan_type=plan_type,
                email=email,
            )
        except Exception:
            return None

    def _session(
        self,
        session_id: str | None,
        *,
        allow_file_fallback: bool = True,
    ) -> _OAuthSession | None:
        self._prune()
        if session_id:
            sess = self._sessions.get(session_id)
            if sess:
                return sess
        if allow_file_fallback:
            return self._load_from_codex_auth_file()
        return None

    def get_status(self, session_id: str | None) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False, "logged_in": False}

        # Web UI status must represent THIS browser session only.
        sess = self._session(session_id, allow_file_fallback=False)
        if not sess:
            return {"configured": True, "logged_in": False}

        now = time.time()
        token_valid = bool(sess.access_token and (sess.expires_at is None or now < sess.expires_at))
        can_refresh = bool(sess.refresh_token)
        expires_in = None
        if sess.expires_at:
            expires_in = int(max(0, sess.expires_at - now))
        return {
            "configured": True,
            "logged_in": token_valid or can_refresh,
            "scope": sess.scope,
            "expires_in": expires_in,
            "email": sess.email,
            "account_id": sess.account_id,
            "plan_type": sess.plan_type,
        }

    async def _refresh(self, session_id: str, sess: _OAuthSession) -> _OAuthSession | None:
        if not sess.refresh_token:
            return None
        try:
            body = await self._token_request({
                "grant_type": "refresh_token",
                "refresh_token": sess.refresh_token,
                "client_id": self.client_id,
            })
            return self._upsert_session(session_id, body)
        except Exception as e:
            logger.warning("openai_codex_oauth_refresh_failed", error=str(e))
            return None

    async def get_access_token(self, session_id: str | None) -> str | None:
        if not self.configured:
            return None

        async with self._lock:
            # Web UI auth context should not silently fall back to ~/.codex/auth.json.
            sess = self._session(session_id, allow_file_fallback=False)
            if not sess:
                return None

            now = time.time()
            if sess.expires_at and now >= (sess.expires_at - 30):
                refreshed = await self._refresh(session_id, sess)
                if not refreshed:
                    self._sessions.pop(session_id, None)
                    self._persist_sessions_to_disk()
                    return None
                return refreshed.access_token
            return sess.access_token

    async def get_auth_context(self, session_id: str | None) -> dict[str, str] | None:
        token = await self.get_access_token(session_id)
        if not token:
            return None
        sess = self._session(session_id)
        if not sess:
            account_id, plan_type, email = _extract_auth_claims(token)
            return {
                "access_token": token,
                "account_id": account_id or "",
                "plan_type": plan_type or "",
                "email": email or "",
            }
        return {
            "access_token": token,
            "account_id": sess.account_id or "",
            "plan_type": sess.plan_type or "",
            "email": sess.email or "",
        }

    async def logout(self, session_id: str | None) -> None:
        if not session_id:
            return
        async with self._lock:
            removed = self._sessions.pop(session_id, None) is not None
            pruned = self._prune()
            if removed or pruned:
                self._persist_sessions_to_disk()
