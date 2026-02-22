"""Tests for LINE adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import KuroConfig, LineConfig
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
    engine.process_message = AsyncMock(return_value="Test response")
    engine.approval_cb = ApprovalCallback()
    engine.approval_policy = ApprovalPolicy(config.security)
    return engine


# =============================================================
# LineConfig Tests
# =============================================================

class TestLineConfig:
    def test_defaults(self):
        cfg = LineConfig()
        assert cfg.enabled is False
        assert cfg.channel_secret_env == "KURO_LINE_CHANNEL_SECRET"
        assert cfg.channel_access_token_env == "KURO_LINE_ACCESS_TOKEN"
        assert cfg.webhook_port == 8443
        assert cfg.allowed_user_ids == []
        assert cfg.max_message_length == 5000
        assert cfg.approval_timeout == 60

    def test_custom_values(self):
        cfg = LineConfig(
            enabled=True,
            webhook_port=9000,
            allowed_user_ids=["U123"],
        )
        assert cfg.enabled is True
        assert cfg.webhook_port == 9000
        assert cfg.allowed_user_ids == ["U123"]

    def test_in_adapters_config(self):
        config = KuroConfig()
        assert hasattr(config.adapters, "line")
        assert isinstance(config.adapters.line, LineConfig)


# =============================================================
# LineApprovalCallback Tests
# =============================================================

class TestLineApprovalCallback:
    def test_init(self):
        from src.adapters.line_adapter import LineApprovalCallback
        cb = LineApprovalCallback()
        assert cb._pending == {}
        assert cb._api is None
        assert cb._user_map == {}

    @pytest.mark.asyncio
    async def test_request_approval_no_api_returns_false(self):
        from src.adapters.line_adapter import LineApprovalCallback
        from src.core.types import Session

        cb = LineApprovalCallback()
        # No API set → returns False
        result = await cb.request_approval(
            tool_name="shell",
            params={"command": "rm -rf /"},
            risk_level=RiskLevel.CRITICAL,
            session=Session(adapter="line"),
        )
        assert result is False

    def test_handle_postback_approve(self):
        from src.adapters.line_adapter import LineApprovalCallback

        cb = LineApprovalCallback()
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        approval_id = "aid001"
        cb._pending[approval_id] = fut
        # Postback data format: "action:approval_id"
        result = cb.handle_postback(f"approve:{approval_id}")
        assert fut.done()
        assert fut.result() is True
        assert "Approved" in result
        loop.close()

    def test_handle_postback_deny(self):
        from src.adapters.line_adapter import LineApprovalCallback

        cb = LineApprovalCallback()
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        approval_id = "aid002"
        cb._pending[approval_id] = fut
        result = cb.handle_postback(f"deny:{approval_id}")
        assert fut.done()
        assert fut.result() is False
        assert "Denied" in result
        loop.close()

    def test_handle_postback_expired(self):
        from src.adapters.line_adapter import LineApprovalCallback
        cb = LineApprovalCallback()
        result = cb.handle_postback("approve:nonexistent")
        assert result is not None  # Returns expiry message


# =============================================================
# LineAdapter Tests
# =============================================================

class TestLineAdapter:
    def _mock_modules(self):
        linebot_mock = MagicMock()
        linebot_mock.v3 = MagicMock()
        linebot_mock.v3.messaging = MagicMock()
        linebot_mock.v3.webhook = MagicMock()
        return {
            "linebot": linebot_mock,
            "linebot.v3": linebot_mock.v3,
            "linebot.v3.messaging": linebot_mock.v3.messaging,
            "linebot.v3.messaging.models": MagicMock(),
            "linebot.v3.webhook": linebot_mock.v3.webhook,
            "linebot.v3.webhook.models": MagicMock(),
            "linebot.v3.exceptions": MagicMock(),
            "aiohttp": MagicMock(),
        }

    def test_init(self, mock_engine, config):
        with patch.dict("sys.modules", self._mock_modules()):
            from src.adapters.line_adapter import LineAdapter
            adapter = LineAdapter(mock_engine, config)
            assert adapter.name == "line"

    def test_is_user_allowed_empty_allows_all(self, mock_engine):
        config = KuroConfig()
        config.adapters.line.allowed_user_ids = []

        with patch.dict("sys.modules", self._mock_modules()):
            from src.adapters.line_adapter import LineAdapter
            adapter = LineAdapter(mock_engine, config)
            assert adapter._is_user_allowed("UANYBODY") is True

    def test_is_user_allowed_restricted(self, mock_engine):
        config = KuroConfig()
        config.adapters.line.allowed_user_ids = ["UALLOWED"]

        with patch.dict("sys.modules", self._mock_modules()):
            from src.adapters.line_adapter import LineAdapter
            adapter = LineAdapter(mock_engine, config)
            assert adapter._is_user_allowed("UALLOWED") is True
            assert adapter._is_user_allowed("UOTHER") is False

    def test_split_message_via_utils(self, mock_engine, config):
        # LINE uses shared split_message with 5000 char limit
        from src.adapters.utils import split_message
        long_msg = "a" * 6000
        chunks = split_message(long_msg, max_len=5000)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 5000


# =============================================================
# AdapterManager with LINE Tests
# =============================================================

class TestAdapterManagerLine:
    def _mock_modules(self):
        return {
            "linebot": MagicMock(),
            "linebot.v3": MagicMock(),
            "linebot.v3.messaging": MagicMock(),
            "linebot.v3.messaging.models": MagicMock(),
            "linebot.v3.webhook": MagicMock(),
            "linebot.v3.webhook.models": MagicMock(),
            "linebot.v3.exceptions": MagicMock(),
            "aiohttp": MagicMock(),
        }

    def test_from_config_line_enabled(self, mock_engine):
        config = KuroConfig()
        config.adapters.line.enabled = True

        with patch.dict("sys.modules", self._mock_modules()):
            from src.adapters.manager import AdapterManager
            manager = AdapterManager.from_config(mock_engine, config)
            assert "line" in manager.adapter_names

    def test_from_config_line_disabled(self, mock_engine):
        config = KuroConfig()
        config.adapters.line.enabled = False

        from src.adapters.manager import AdapterManager
        manager = AdapterManager.from_config(mock_engine, config)
        assert "line" not in manager.adapter_names
