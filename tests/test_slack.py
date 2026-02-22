"""Tests for Slack adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import KuroConfig, SlackConfig
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
    engine.model = MagicMock()
    engine.model.default_model = "anthropic/claude-sonnet-4-20250514"
    return engine


# =============================================================
# SlackConfig Tests
# =============================================================

class TestSlackConfig:
    def test_defaults(self):
        cfg = SlackConfig()
        assert cfg.enabled is False
        assert cfg.bot_token_env == "KURO_SLACK_BOT_TOKEN"
        assert cfg.app_token_env == "KURO_SLACK_APP_TOKEN"
        assert cfg.allowed_user_ids == []
        assert cfg.allowed_channel_ids == []
        assert cfg.max_message_length == 4000
        assert cfg.approval_timeout == 60

    def test_custom_values(self):
        cfg = SlackConfig(
            enabled=True,
            allowed_user_ids=["U123", "U456"],
            max_message_length=2000,
        )
        assert cfg.enabled is True
        assert cfg.allowed_user_ids == ["U123", "U456"]
        assert cfg.max_message_length == 2000

    def test_in_adapters_config(self):
        config = KuroConfig()
        assert hasattr(config.adapters, "slack")
        assert isinstance(config.adapters.slack, SlackConfig)


# =============================================================
# SlackApprovalCallback Tests
# =============================================================

class TestSlackApprovalCallback:
    def test_init(self):
        from src.adapters.slack_adapter import SlackApprovalCallback
        cb = SlackApprovalCallback()
        assert cb._pending == {}
        assert cb._app is None
        assert cb._channel_map == {}

    @pytest.mark.asyncio
    async def test_request_approval_no_app_returns_false(self):
        from src.adapters.slack_adapter import SlackApprovalCallback
        from src.core.types import Session

        cb = SlackApprovalCallback()
        # No app set → should return False immediately
        result = await cb.request_approval(
            tool_name="shell",
            params={"command": "ls"},
            risk_level=RiskLevel.HIGH,
            session=Session(adapter="slack"),
        )
        assert result is False

    def test_handle_action_approve(self):
        from src.adapters.slack_adapter import SlackApprovalCallback

        cb = SlackApprovalCallback()
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        cb._pending["aid1"] = fut

        result = cb.handle_action("approve", "aid1")
        assert fut.done()
        assert fut.result() is True
        assert "Approved" in result
        loop.close()

    def test_handle_action_deny(self):
        from src.adapters.slack_adapter import SlackApprovalCallback

        cb = SlackApprovalCallback()
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        cb._pending["aid2"] = fut

        result = cb.handle_action("deny", "aid2")
        assert fut.done()
        assert fut.result() is False
        assert "Denied" in result
        loop.close()

    def test_handle_action_unknown_id(self):
        from src.adapters.slack_adapter import SlackApprovalCallback
        cb = SlackApprovalCallback()
        # Expired approval returns a message
        result = cb.handle_action("approve", "nonexistent")
        assert result is not None


# =============================================================
# SlackAdapter Tests
# =============================================================

class TestSlackAdapter:
    def test_init(self, mock_engine, config):
        with patch.dict("sys.modules", {
            "slack_bolt": MagicMock(),
            "slack_bolt.async_app": MagicMock(),
            "slack_bolt.adapter.socket_mode.async_handler": MagicMock(),
            "slack_sdk": MagicMock(),
        }):
            from src.adapters.slack_adapter import SlackAdapter
            adapter = SlackAdapter(mock_engine, config)
            assert adapter.name == "slack"

    def test_is_user_allowed(self, mock_engine):
        config = KuroConfig()
        config.adapters.slack.allowed_user_ids = ["U111", "U222"]

        with patch.dict("sys.modules", {
            "slack_bolt": MagicMock(),
            "slack_bolt.async_app": MagicMock(),
            "slack_bolt.adapter.socket_mode.async_handler": MagicMock(),
            "slack_sdk": MagicMock(),
        }):
            from src.adapters.slack_adapter import SlackAdapter
            adapter = SlackAdapter(mock_engine, config)
            assert adapter._is_user_allowed("U111") is True
            assert adapter._is_user_allowed("U333") is False

    def test_is_user_allowed_empty_list_allows_all(self, mock_engine):
        config = KuroConfig()
        config.adapters.slack.allowed_user_ids = []

        with patch.dict("sys.modules", {
            "slack_bolt": MagicMock(),
            "slack_bolt.async_app": MagicMock(),
            "slack_bolt.adapter.socket_mode.async_handler": MagicMock(),
            "slack_sdk": MagicMock(),
        }):
            from src.adapters.slack_adapter import SlackAdapter
            adapter = SlackAdapter(mock_engine, config)
            assert adapter._is_user_allowed("UANYBODY") is True

    def test_session_key(self, mock_engine):
        config = KuroConfig()
        with patch.dict("sys.modules", {
            "slack_bolt": MagicMock(),
            "slack_bolt.async_app": MagicMock(),
            "slack_bolt.adapter.socket_mode.async_handler": MagicMock(),
            "slack_sdk": MagicMock(),
        }):
            from src.adapters.slack_adapter import SlackAdapter
            adapter = SlackAdapter(mock_engine, config)
            key = adapter._session_key("C123", "U456")
            assert key == "C123:U456"


# =============================================================
# split_message Tests (shared utility)
# =============================================================

class TestSplitMessage:
    def test_short_message_no_split(self):
        from src.adapters.utils import split_message
        chunks = split_message("Hello world", max_len=4000)
        assert chunks == ["Hello world"]

    def test_long_message_splits(self):
        from src.adapters.utils import split_message
        text = "x" * 10000
        chunks = split_message(text, max_len=4000)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 4000

    def test_split_preserves_content(self):
        from src.adapters.utils import split_message
        text = "\n\n".join([f"Paragraph {i}" for i in range(100)])
        chunks = split_message(text, max_len=500)
        combined = "".join(chunks)
        # All content preserved (joining may omit separators but content is there)
        for i in range(100):
            assert f"Paragraph {i}" in combined

    def test_empty_string(self):
        from src.adapters.utils import split_message
        chunks = split_message("", max_len=4000)
        assert chunks == [""]

    def test_code_block_not_split_mid(self):
        from src.adapters.utils import split_message
        # Code block that fits in max_len should stay together
        code = "```python\nprint('hello')\n```"
        chunks = split_message(code, max_len=4000)
        assert len(chunks) == 1
        assert "```python" in chunks[0]


# =============================================================
# AdapterManager with Slack Tests
# =============================================================

class TestAdapterManagerSlack:
    def test_from_config_slack_enabled(self, mock_engine):
        config = KuroConfig()
        config.adapters.slack.enabled = True

        with patch.dict("sys.modules", {
            "slack_bolt": MagicMock(),
            "slack_bolt.async_app": MagicMock(),
            "slack_bolt.adapter.socket_mode.async_handler": MagicMock(),
            "slack_sdk": MagicMock(),
        }):
            from src.adapters.manager import AdapterManager
            manager = AdapterManager.from_config(mock_engine, config)
            assert "slack" in manager.adapter_names

    def test_from_config_slack_disabled(self, mock_engine):
        config = KuroConfig()
        config.adapters.slack.enabled = False

        from src.adapters.manager import AdapterManager
        manager = AdapterManager.from_config(mock_engine, config)
        assert "slack" not in manager.adapter_names

    def test_explicit_slack_adapter(self, mock_engine):
        config = KuroConfig()

        with patch.dict("sys.modules", {
            "slack_bolt": MagicMock(),
            "slack_bolt.async_app": MagicMock(),
            "slack_bolt.adapter.socket_mode.async_handler": MagicMock(),
            "slack_sdk": MagicMock(),
        }):
            from src.adapters.manager import AdapterManager
            manager = AdapterManager.from_config(mock_engine, config, adapters=["slack"])
            assert "slack" in manager.adapter_names
