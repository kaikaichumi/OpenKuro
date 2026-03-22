"""Ephemeral provider-secret broker (Phase 3).

Goals:
- Keep long-lived provider secrets out of general runtime context.
- Issue short-lived one-time leases for each model request.
- Support lease revocation and provider secret rotation.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any


_PLACEHOLDER_SECRETS = {
    "",
    "none",
    "null",
    "not-needed",
    "not_needed",
    "dummy",
}


@dataclass(frozen=True)
class SecretLease:
    """One ephemeral lease id for one provider secret retrieval."""

    lease_id: str
    provider: str
    issued_at: int
    expires_at: int
    generation: int
    reason: str


@dataclass
class _LeaseRecord:
    provider: str
    expires_at: int
    generation: int
    reason: str
    issued_at: int


class SecretBroker:
    """Issue and validate short-lived one-time leases for provider API keys."""

    def __init__(self, config: Any, providers: dict[str, Any] | None = None) -> None:
        self.config = config
        self._providers = providers or {}
        self._leases: dict[str, _LeaseRecord] = {}
        self._generation: dict[str, int] = {}
        self._rotated_overrides: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", False))

    @property
    def export_provider_env(self) -> bool:
        return bool(getattr(self.config, "export_provider_env", False))

    def _lease_ttl(self, ttl_seconds: int | None = None) -> int:
        base = int(getattr(self.config, "lease_ttl_seconds", 120) or 120)
        max_ttl = int(getattr(self.config, "max_lease_ttl_seconds", 900) or 900)
        requested = int(ttl_seconds if ttl_seconds is not None else base)
        return max(5, min(max_ttl, requested))

    @staticmethod
    def _normalize_secret(raw: Any) -> str | None:
        secret = str(raw or "").strip()
        if not secret:
            return None
        if secret.lower() in _PLACEHOLDER_SECRETS:
            return None
        return secret

    def _resolve_provider_secret(self, provider: str) -> str | None:
        key = str(provider or "").strip().lower()
        if not key:
            return None
        override = self._normalize_secret(self._rotated_overrides.get(key))
        if override:
            return override

        cfg = self._providers.get(key)
        if cfg is None:
            return None
        getter = getattr(cfg, "get_api_key", None)
        try:
            raw = getter() if callable(getter) else getattr(cfg, "api_key", None)
        except Exception:
            raw = None
        return self._normalize_secret(raw)

    def _known_provider_keys(self) -> list[str]:
        return sorted(
            str(name or "").strip().lower()
            for name in self._providers.keys()
            if str(name or "").strip()
        )

    def _provider_exists(self, provider: str) -> bool:
        key = str(provider or "").strip().lower()
        if not key:
            return False
        return key in self._providers

    def _generation_of(self, provider: str) -> int:
        key = str(provider or "").strip().lower()
        return int(self._generation.get(key, 0) or 0)

    def _bump_generation_locked(self, provider: str) -> int:
        key = str(provider or "").strip().lower()
        next_gen = self._generation_of(key) + 1
        self._generation[key] = next_gen
        return next_gen

    def _prune_locked(self) -> None:
        now_ts = int(time.time())
        expired = [k for k, v in self._leases.items() if int(v.expires_at) <= now_ts]
        for lease_id in expired:
            self._leases.pop(lease_id, None)

        max_active = max(100, int(getattr(self.config, "max_active_leases", 10_000) or 10_000))
        if len(self._leases) <= max_active:
            return
        ordered = sorted(
            self._leases.items(),
            key=lambda kv: int(kv[1].expires_at),
            reverse=True,
        )
        self._leases = dict(ordered[:max_active])

    def issue_lease(
        self,
        provider: str,
        *,
        ttl_seconds: int | None = None,
        reason: str = "model_request",
    ) -> SecretLease | None:
        """Issue a short-lived lease id for one provider key retrieval."""
        key = str(provider or "").strip().lower()
        if not self.enabled or not key:
            return None
        if not self._provider_exists(key):
            return None
        if not self._resolve_provider_secret(key):
            return None

        now_ts = int(time.time())
        ttl = self._lease_ttl(ttl_seconds)
        expires_at = now_ts + ttl
        lease_id = secrets.token_urlsafe(18)
        with self._lock:
            self._prune_locked()
            generation = self._generation_of(key)
            self._leases[lease_id] = _LeaseRecord(
                provider=key,
                expires_at=expires_at,
                generation=generation,
                reason=str(reason or "model_request"),
                issued_at=now_ts,
            )
        return SecretLease(
            lease_id=lease_id,
            provider=key,
            issued_at=now_ts,
            expires_at=expires_at,
            generation=generation,
            reason=str(reason or "model_request"),
        )

    def consume_lease(self, lease_id: str) -> str | None:
        """Resolve and consume one lease id (single-use)."""
        token = str(lease_id or "").strip()
        if not token:
            return None
        now_ts = int(time.time())
        with self._lock:
            rec = self._leases.pop(token, None)
            if rec is None:
                return None
            if int(rec.expires_at) <= now_ts:
                return None
            if int(rec.generation) != self._generation_of(rec.provider):
                return None
        return self._resolve_provider_secret(rec.provider)

    def acquire_secret(
        self,
        provider: str,
        *,
        ttl_seconds: int | None = None,
        reason: str = "model_request",
    ) -> str | None:
        """Issue + consume one lease in one call (single-use secret fetch)."""
        lease = self.issue_lease(provider, ttl_seconds=ttl_seconds, reason=reason)
        if lease is None:
            return None
        return self.consume_lease(lease.lease_id)

    def revoke_lease(self, lease_id: str) -> bool:
        token = str(lease_id or "").strip()
        if not token:
            return False
        with self._lock:
            return self._leases.pop(token, None) is not None

    def revoke_provider(self, provider: str) -> dict[str, Any]:
        """Revoke all active leases for one provider immediately."""
        key = str(provider or "").strip().lower()
        if not key:
            return {"status": "error", "reason": "missing_provider"}
        if not self._provider_exists(key):
            return {"status": "error", "reason": "unknown_provider", "provider": key}
        with self._lock:
            self._bump_generation_locked(key)
            removed = [k for k, rec in self._leases.items() if rec.provider == key]
            for lease_id in removed:
                self._leases.pop(lease_id, None)
        return {"status": "ok", "provider": key, "revoked_leases": len(removed)}

    def rotate_provider(self, provider: str, new_secret: str | None) -> dict[str, Any]:
        """Rotate provider secret source and invalidate old leases."""
        key = str(provider or "").strip().lower()
        if not key:
            return {"status": "error", "reason": "missing_provider"}
        if not self._provider_exists(key):
            return {"status": "error", "reason": "unknown_provider", "provider": key}

        normalized = self._normalize_secret(new_secret)
        with self._lock:
            if normalized:
                self._rotated_overrides[key] = normalized
                mode = "override"
            else:
                self._rotated_overrides.pop(key, None)
                mode = "fallback_source"

            revoked = 0
            if bool(getattr(self.config, "revoke_on_rotate", True)):
                self._bump_generation_locked(key)
                to_remove = [k for k, rec in self._leases.items() if rec.provider == key]
                for lease_id in to_remove:
                    self._leases.pop(lease_id, None)
                revoked = len(to_remove)
        return {
            "status": "ok",
            "provider": key,
            "mode": mode,
            "revoked_leases": revoked,
        }

    def status(self) -> dict[str, Any]:
        """Return broker runtime status for diagnostics."""
        with self._lock:
            self._prune_locked()
            active = len(self._leases)
            generations = {k: int(v) for k, v in self._generation.items()}
            overrides = sorted(self._rotated_overrides.keys())
            known_providers = self._known_provider_keys()
        return {
            "enabled": self.enabled,
            "lease_ttl_seconds": self._lease_ttl(),
            "max_lease_ttl_seconds": int(
                getattr(self.config, "max_lease_ttl_seconds", self._lease_ttl())
                or self._lease_ttl()
            ),
            "max_active_leases": int(getattr(self.config, "max_active_leases", 10_000) or 10_000),
            "export_provider_env": self.export_provider_env,
            "revoke_on_rotate": bool(getattr(self.config, "revoke_on_rotate", True)),
            "active_leases": active,
            "known_providers": known_providers,
            "provider_generations": generations,
            "rotated_override_providers": overrides,
        }
