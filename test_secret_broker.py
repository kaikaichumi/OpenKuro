"""Tests for Secret Broker runtime (Phase 3)."""

from __future__ import annotations

from types import SimpleNamespace

from src.config import SecretBrokerConfig
from src.core.security.secret_broker import SecretBroker


def _provider_cfg(secret: str):
    return SimpleNamespace(
        api_key=secret,
        get_api_key=lambda: secret,
    )


def test_secret_broker_issue_consume_single_use() -> None:
    broker = SecretBroker(
        SecretBrokerConfig(enabled=True, lease_ttl_seconds=30),
        {"openai": _provider_cfg("sk-test")},
    )

    lease = broker.issue_lease("openai")
    assert lease is not None
    assert lease.provider == "openai"

    secret = broker.consume_lease(lease.lease_id)
    assert secret == "sk-test"
    assert broker.consume_lease(lease.lease_id) is None


def test_secret_broker_revoke_provider_invalidates_pending_leases() -> None:
    broker = SecretBroker(
        SecretBrokerConfig(enabled=True, lease_ttl_seconds=120),
        {"openai": _provider_cfg("sk-base")},
    )
    lease = broker.issue_lease("openai")
    assert lease is not None

    result = broker.revoke_provider("openai")
    assert result.get("status") == "ok"
    assert int(result.get("revoked_leases", 0)) >= 1
    assert broker.consume_lease(lease.lease_id) is None


def test_secret_broker_rotate_override_and_fallback_source() -> None:
    broker = SecretBroker(
        SecretBrokerConfig(enabled=True, lease_ttl_seconds=120, revoke_on_rotate=True),
        {"openai": _provider_cfg("sk-base")},
    )

    lease = broker.issue_lease("openai")
    assert lease is not None
    rotate_override = broker.rotate_provider("openai", "sk-override")
    assert rotate_override.get("status") == "ok"
    assert rotate_override.get("mode") == "override"
    assert broker.consume_lease(lease.lease_id) is None
    assert broker.acquire_secret("openai") == "sk-override"

    rotate_clear = broker.rotate_provider("openai", "")
    assert rotate_clear.get("status") == "ok"
    assert rotate_clear.get("mode") == "fallback_source"
    assert broker.acquire_secret("openai") == "sk-base"


def test_secret_broker_unknown_provider_rejected() -> None:
    broker = SecretBroker(
        SecretBrokerConfig(enabled=True),
        {"openai": _provider_cfg("sk-test")},
    )

    assert broker.issue_lease("gemini") is None
    assert broker.revoke_provider("gemini").get("reason") == "unknown_provider"
    assert broker.rotate_provider("gemini", "abc").get("reason") == "unknown_provider"


def test_secret_broker_status_includes_known_providers() -> None:
    broker = SecretBroker(
        SecretBrokerConfig(enabled=True),
        {
            "openai": _provider_cfg("sk-openai"),
            "gemini": _provider_cfg("gm-test"),
        },
    )
    status = broker.status()
    assert status.get("enabled") is True
    assert sorted(status.get("known_providers") or []) == ["gemini", "openai"]
