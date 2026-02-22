"""Tests for Email adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import EmailConfig, KuroConfig
from src.core.engine import ApprovalCallback, Engine
from src.tools.base import RiskLevel


# === Shared fixtures ===

@pytest.fixture
def config():
    return KuroConfig()


@pytest.fixture
def mock_engine(config):
    from src.core.security.approval import ApprovalPolicy

    engine = MagicMock(spec=Engine)
    engine.config = config
    engine.process_message = AsyncMock(return_value="Email response")
    engine.approval_cb = ApprovalCallback()
    engine.approval_policy = ApprovalPolicy(config.security)
    return engine


def _mock_email_modules():
    return {
        "aiosmtplib": MagicMock(),
        "aioimaplib": MagicMock(),
    }


# =============================================================
# EmailConfig Tests
# =============================================================

class TestEmailConfig:
    def test_defaults(self):
        cfg = EmailConfig()
        assert cfg.enabled is False
        assert cfg.imap_host == "imap.gmail.com"
        assert cfg.imap_port == 993
        assert cfg.smtp_host == "smtp.gmail.com"
        assert cfg.smtp_port == 587
        assert cfg.email_env == "KURO_EMAIL_ADDRESS"
        assert cfg.password_env == "KURO_EMAIL_PASSWORD"
        assert cfg.allowed_senders == []
        assert cfg.check_interval == 30
        assert cfg.approval_timeout == 300

    def test_custom_values(self):
        cfg = EmailConfig(
            enabled=True,
            imap_host="mail.example.com",
            allowed_senders=["boss@company.com"],
        )
        assert cfg.enabled is True
        assert cfg.imap_host == "mail.example.com"
        assert cfg.allowed_senders == ["boss@company.com"]

    def test_in_adapters_config(self):
        config = KuroConfig()
        assert hasattr(config.adapters, "email")
        assert isinstance(config.adapters.email, EmailConfig)


# =============================================================
# EmailAdapter Tests
# =============================================================

class TestEmailAdapter:
    def test_init(self, mock_engine, config):
        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.email_adapter import EmailAdapter
            adapter = EmailAdapter(mock_engine, config)
            assert adapter.name == "email"

    def test_normalize_subject(self, mock_engine, config):
        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.email_adapter import EmailAdapter
            adapter = EmailAdapter(mock_engine, config)

            assert adapter._normalize_subject("Re: Hello World") == "Hello World"
            assert adapter._normalize_subject("RE: Hello World") == "Hello World"
            assert adapter._normalize_subject("Fwd: Hello World") == "Hello World"
            assert adapter._normalize_subject("FW: Hello World") == "Hello World"
            assert adapter._normalize_subject("Hello World") == "Hello World"
            # Single-level stripping only (implementation strips one prefix at a time)
            result = adapter._normalize_subject("Re: Re: Hello World")
            assert "Hello World" in result

    def test_session_key_consistent(self, mock_engine, config):
        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.email_adapter import EmailAdapter
            adapter = EmailAdapter(mock_engine, config)

            key1 = adapter._session_key("user@example.com", "Re: Project Update")
            key2 = adapter._session_key("user@example.com", "RE: Project Update")
            assert key1 == key2  # Same normalized subject → same key

            key3 = adapter._session_key("other@example.com", "Re: Project Update")
            assert key1 != key3  # Different sender → different key

    def test_is_sender_allowed_empty_allows_all(self, mock_engine, config):
        config.adapters.email.allowed_senders = []

        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.email_adapter import EmailAdapter
            adapter = EmailAdapter(mock_engine, config)
            assert adapter._is_sender_allowed("anyone@example.com") is True

    def test_is_sender_allowed_restricted(self, mock_engine):
        config = KuroConfig()
        config.adapters.email.allowed_senders = ["boss@company.com"]

        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.email_adapter import EmailAdapter
            adapter = EmailAdapter(mock_engine, config)
            assert adapter._is_sender_allowed("boss@company.com") is True
            assert adapter._is_sender_allowed("spam@evil.com") is False


# =============================================================
# EmailApprovalCallback Tests
# =============================================================

class TestEmailApprovalCallback:
    def test_init(self):
        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.email_adapter import EmailApprovalCallback
            cb = EmailApprovalCallback()
            assert cb._pending == {}
            assert cb._send_fn is None
            assert cb._email_map == {}

    def test_try_resolve_approve(self):
        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.email_adapter import EmailApprovalCallback

            cb = EmailApprovalCallback()

            approval_id = "abc12345"  # valid hex chars only
            loop = asyncio.new_event_loop()
            fut = loop.create_future()
            cb._pending[approval_id] = fut

            # Simulate approve reply — subject must contain [#approval_id]
            resolved = cb.try_resolve_from_reply(
                subject=f"Re: [Kuro] Approval Required: shell [#{approval_id}]",
                body="approve",
                sender="user@example.com",
            )
            assert resolved is True
            assert fut.done()
            assert fut.result() is True
            loop.close()

    def test_try_resolve_deny(self):
        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.email_adapter import EmailApprovalCallback

            cb = EmailApprovalCallback()

            approval_id = "def45678"  # valid hex chars only
            loop = asyncio.new_event_loop()
            fut = loop.create_future()
            cb._pending[approval_id] = fut

            resolved = cb.try_resolve_from_reply(
                subject=f"Re: [Kuro] Approval Required: shell [#{approval_id}]",
                body="deny\n\nOn Thu... wrote:\n> approve this",
                sender="user@example.com",
            )
            assert resolved is True
            assert fut.result() is False
            loop.close()

    def test_try_resolve_wrong_subject(self):
        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.email_adapter import EmailApprovalCallback

            cb = EmailApprovalCallback()

            resolved = cb.try_resolve_from_reply(
                subject="Some unrelated email",
                body="approve",
                sender="user@example.com",
            )
            assert resolved is False

    def test_try_resolve_not_pending(self):
        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.email_adapter import EmailApprovalCallback

            cb = EmailApprovalCallback()

            # Approval ID that doesn't exist in pending → future is None
            resolved = cb.try_resolve_from_reply(
                subject="Re: [Kuro] Approval Required: shell [#nonexistent]",
                body="approve",
                sender="user@example.com",
            )
            assert resolved is False


# =============================================================
# AdapterManager with Email Tests
# =============================================================

class TestAdapterManagerEmail:
    def test_from_config_email_enabled(self, mock_engine):
        config = KuroConfig()
        config.adapters.email.enabled = True

        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.manager import AdapterManager
            manager = AdapterManager.from_config(mock_engine, config)
            assert "email" in manager.adapter_names

    def test_from_config_email_disabled(self, mock_engine):
        config = KuroConfig()
        config.adapters.email.enabled = False

        from src.adapters.manager import AdapterManager
        manager = AdapterManager.from_config(mock_engine, config)
        assert "email" not in manager.adapter_names

    def test_explicit_email_adapter(self, mock_engine):
        config = KuroConfig()

        with patch.dict("sys.modules", _mock_email_modules()):
            from src.adapters.manager import AdapterManager
            manager = AdapterManager.from_config(mock_engine, config, adapters=["email"])
            assert "email" in manager.adapter_names
