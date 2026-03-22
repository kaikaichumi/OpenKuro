"""Capability token issuer/validator for tool execution.

Phase 2 goals:
- Short-lived, single-use tokens.
- Bound to tool/session/adapter/model + argument digest.
- Include domain/path profile from arguments.
- Persist nonce replay cache across process restarts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.config import CapabilityTokenConfig, get_kuro_home

_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_PATH_KEY_HINTS = ("path", "file", "dir", "directory", "cwd", "workdir")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    raw = str(text or "").strip()
    if not raw:
        return b""
    pad_len = (4 - (len(raw) % 4)) % 4
    return base64.urlsafe_b64decode(raw + ("=" * pad_len))


def _canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _normalize_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    if len(text) > 320:
        text = text[:320]
    if os.name == "nt":
        text = text.lower()
    return text


class CapabilityTokenManager:
    """Issue and validate HMAC-signed capability tokens."""

    def __init__(self, config: CapabilityTokenConfig | None = None) -> None:
        self.config = config or CapabilityTokenConfig()
        self._secret = self._resolve_secret()
        self._used_nonces: dict[str, int] = {}
        self._lock = threading.Lock()
        self._nonce_cache_file = self._resolve_nonce_cache_file()
        self._nonce_cache_loaded = False

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", False))

    @property
    def _persist_enabled(self) -> bool:
        return bool(
            self.enabled
            and bool(getattr(self.config, "persist_nonce_cache", True))
            and self._nonce_cache_file is not None
        )

    def _resolve_secret(self) -> bytes:
        env_name = str(getattr(self.config, "secret_env", "KURO_CAPABILITY_TOKEN_SECRET") or "").strip()
        env_value = os.environ.get(env_name, "").strip() if env_name else ""
        if env_value:
            return hashlib.sha256(env_value.encode("utf-8")).digest()

        fallback = f"{Path.home()}|{os.name}|kuro-capability-token"
        return hashlib.sha256(fallback.encode("utf-8")).digest()

    def _resolve_nonce_cache_file(self) -> Path | None:
        raw = str(getattr(self.config, "nonce_cache_file", "") or "").strip()
        if raw:
            return Path(raw).expanduser()
        return get_kuro_home() / "security" / "capability_nonces.json"

    @staticmethod
    def _extract_urls(value: Any) -> list[str]:
        urls: list[str] = []
        if isinstance(value, str):
            for hit in _URL_RE.findall(value):
                urls.append(hit.strip().rstrip(".,);]>"))
            return urls
        if isinstance(value, dict):
            for item in value.values():
                urls.extend(CapabilityTokenManager._extract_urls(item))
            return urls
        if isinstance(value, list):
            for item in value:
                urls.extend(CapabilityTokenManager._extract_urls(item))
            return urls
        return urls

    @staticmethod
    def _extract_paths(value: Any) -> list[str]:
        out: list[str] = []
        if isinstance(value, dict):
            for key, raw in value.items():
                k = str(key or "").strip().lower()
                if isinstance(raw, str) and any(hint in k for hint in _PATH_KEY_HINTS):
                    candidate = raw.strip()
                    if candidate and not candidate.startswith(("http://", "https://")):
                        norm = _normalize_path(candidate)
                        if norm:
                            out.append(norm)
                out.extend(CapabilityTokenManager._extract_paths(raw))
        elif isinstance(value, list):
            for item in value:
                out.extend(CapabilityTokenManager._extract_paths(item))
        return out

    @staticmethod
    def _domain_profile(arguments: dict[str, Any]) -> list[str]:
        domains: set[str] = set()
        for url in CapabilityTokenManager._extract_urls(arguments):
            try:
                host = str(urlparse(url).hostname or "").strip().lower()
            except Exception:
                host = ""
            if host:
                domains.add(host)
        return sorted(domains)

    @staticmethod
    def _path_profile(arguments: dict[str, Any]) -> list[str]:
        paths = {_normalize_path(p) for p in CapabilityTokenManager._extract_paths(arguments)}
        paths.discard("")
        return sorted(paths)

    @staticmethod
    def _args_digest(arguments: dict[str, Any]) -> str:
        payload = _canonical_json(arguments or {})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _prune_nonces(self, now_ts: int) -> None:
        stale = [nonce for nonce, expiry in self._used_nonces.items() if int(expiry) <= now_ts]
        for nonce in stale:
            self._used_nonces.pop(nonce, None)

    def _trim_nonce_cache(self) -> None:
        max_entries = max(100, int(getattr(self.config, "max_nonce_cache_entries", 50_000) or 50_000))
        if len(self._used_nonces) <= max_entries:
            return
        ordered = sorted(self._used_nonces.items(), key=lambda kv: int(kv[1]), reverse=True)
        self._used_nonces = {nonce: int(expiry) for nonce, expiry in ordered[:max_entries]}

    def _load_nonce_cache_locked(self, *, force: bool = False) -> None:
        if not self._persist_enabled:
            return
        if self._nonce_cache_loaded and not force:
            return
        self._nonce_cache_loaded = True
        path = self._nonce_cache_file
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        source = raw.get("used_nonces", {})
        if not isinstance(source, dict):
            return

        now_ts = int(time.time())
        for nonce, expiry_raw in source.items():
            n = str(nonce or "").strip()
            if not n:
                continue
            try:
                expiry = int(expiry_raw)
            except Exception:
                continue
            if expiry <= now_ts:
                continue
            prev = int(self._used_nonces.get(n, 0) or 0)
            if expiry > prev:
                self._used_nonces[n] = expiry

        self._prune_nonces(now_ts)
        self._trim_nonce_cache()

    def _persist_nonce_cache_locked(self) -> None:
        if not self._persist_enabled:
            return
        path = self._nonce_cache_file
        if path is None:
            return

        now_ts = int(time.time())
        self._prune_nonces(now_ts)
        self._trim_nonce_cache()
        payload = {
            "version": 1,
            "updated_at": now_ts,
            "used_nonces": {k: int(v) for k, v in self._used_nonces.items()},
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            return

    def _consume_nonce(self, nonce: str, expiry_ts: int) -> bool:
        now_ts = int(time.time())
        with self._lock:
            if self._persist_enabled:
                self._load_nonce_cache_locked(force=True)
            self._prune_nonces(now_ts)

            existing = int(self._used_nonces.get(nonce, 0) or 0)
            if existing > now_ts:
                return False

            self._used_nonces[nonce] = int(expiry_ts)
            if self._persist_enabled:
                self._persist_nonce_cache_locked()
            return True

    def issue(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str,
        adapter: str,
        active_model: str,
    ) -> tuple[str, dict[str, Any]]:
        """Issue one short-lived token for one tool execution attempt."""
        now_ts = int(time.time())
        ttl = max(5, int(getattr(self.config, "ttl_seconds", 120) or 120))
        exp_ts = now_ts + ttl
        nonce = secrets.token_urlsafe(18)
        claims = {
            "v": 1,
            "iat": now_ts,
            "exp": exp_ts,
            "nonce": nonce,
            "tool": str(tool_name or "").strip(),
            "session_id": str(session_id or "").strip(),
            "adapter": str(adapter or "").strip().lower() if bool(getattr(self.config, "bind_adapter", True)) else "",
            "active_model": str(active_model or "").strip() if bool(getattr(self.config, "bind_active_model", True)) else "",
            "args_digest": self._args_digest(arguments),
            "domains": self._domain_profile(arguments),
            "paths": self._path_profile(arguments),
        }
        payload = _canonical_json(claims).encode("utf-8")
        sig = hmac.new(self._secret, payload, hashlib.sha256).digest()
        token = f"{_b64url_encode(payload)}.{_b64url_encode(sig)}"
        return token, claims

    def validate(
        self,
        *,
        token: str | None,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str,
        adapter: str,
        active_model: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Validate token and consume nonce when single-use mode is on."""
        raw = str(token or "").strip()
        if not raw:
            return False, "missing capability token", None
        if "." not in raw:
            return False, "invalid token format", None

        payload_part, sig_part = raw.split(".", 1)
        try:
            payload = _b64url_decode(payload_part)
            sig = _b64url_decode(sig_part)
        except Exception:
            return False, "invalid base64 token", None

        expected_sig = hmac.new(self._secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected_sig):
            return False, "signature mismatch", None

        try:
            claims = json.loads(payload.decode("utf-8"))
        except Exception:
            return False, "invalid token payload", None
        if not isinstance(claims, dict):
            return False, "invalid token payload type", None

        now_ts = int(time.time())
        skew = max(0, int(getattr(self.config, "max_clock_skew_seconds", 0) or 0))
        iat = int(claims.get("iat", 0) or 0)
        exp = int(claims.get("exp", 0) or 0)
        if iat - skew > now_ts:
            return False, "token issued in the future", claims
        if now_ts > exp + skew:
            return False, "token expired", claims

        if str(claims.get("tool", "")) != str(tool_name or ""):
            return False, "tool mismatch", claims
        if str(claims.get("session_id", "")) != str(session_id or ""):
            return False, "session mismatch", claims

        if bool(getattr(self.config, "bind_adapter", True)):
            expected_adapter = str(claims.get("adapter", "") or "").strip().lower()
            actual_adapter = str(adapter or "").strip().lower()
            if expected_adapter != actual_adapter:
                return False, "adapter mismatch", claims

        if bool(getattr(self.config, "bind_active_model", True)):
            expected_model = str(claims.get("active_model", "") or "").strip()
            actual_model = str(active_model or "").strip()
            if expected_model != actual_model:
                return False, "active model mismatch", claims

        args_digest = self._args_digest(arguments)
        if str(claims.get("args_digest", "")) != args_digest:
            return False, "arguments digest mismatch", claims

        expected_domains = {str(v).strip().lower() for v in (claims.get("domains") or []) if str(v).strip()}
        actual_domains = set(self._domain_profile(arguments))
        if not actual_domains.issubset(expected_domains):
            return False, "domain profile mismatch", claims

        expected_paths = {str(v).strip() for v in (claims.get("paths") or []) if str(v).strip()}
        actual_paths = set(self._path_profile(arguments))
        if not actual_paths.issubset(expected_paths):
            return False, "path profile mismatch", claims

        if bool(getattr(self.config, "enforce_single_use", True)):
            nonce = str(claims.get("nonce", "") or "").strip()
            if not nonce:
                return False, "missing nonce", claims
            if not self._consume_nonce(nonce, exp + skew):
                return False, "nonce already used", claims

        return True, "ok", claims

