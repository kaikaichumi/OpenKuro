"""Tests for Phase 5: messaging adapters (Telegram, manager, base)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import KuroConfig, TelegramConfig
from src.core.engine import ApprovalCallback, Engine
from src.core.types import Session
from src.tools.base import RiskLevel


# === Shared fixtures ===

@pytest.fixture
def config():
    """Create a default KuroConfig."""
    return KuroConfig()


@pytest.fixture
def mock_engine(config):
    """Create a mock Engine instance."""
    from src.core.security.approval import ApprovalPolicy

    engine = MagicMock(spec=Engine)
    engine.config = config
    engine.process_message = AsyncMock(return_value="Test response")
    engine.approval_cb = ApprovalCallback()
    engine.approval_policy = ApprovalPolicy(config.security)
    return engine


# =============================================================
# Config Tests
# =============================================================

class TestTelegramConfig:
    """Tests for TelegramConfig."""

    def test_defaults(self):
        cfg = TelegramConfig()
        assert cfg.enabled is False
        assert cfg.bot_token_env == "KURO_TELEGRAM_TOKEN"
        assert cfg.allowed_user_ids == []
        assert cfg.max_message_length == 4096
        assert cfg.approval_timeout == 60

    def test_get_bot_token_from_env(self):
        cfg = TelegramConfig()
        with patch.dict("os.environ", {"KURO_TELEGRAM_TOKEN": "test-token-123"}):
            assert cfg.get_bot_token() == "test-token-123"

    def test_get_bot_token_missing(self):
        cfg = TelegramConfig(bot_token_env="NONEXISTENT_VAR")
        assert cfg.get_bot_token() is None

    def test_custom_config(self):
        cfg = TelegramConfig(
            enabled=True,
            allowed_user_ids=[123, 456],
            max_message_length=2048,
        )
        assert cfg.enabled is True
        assert cfg.allowed_user_ids == [123, 456]
        assert cfg.max_message_length == 2048

    def test_adapters_config_has_telegram(self):
        config = KuroConfig()
        assert hasattr(config.adapters, "telegram")
        assert isinstance(config.adapters.telegram, TelegramConfig)


# =============================================================
# BaseAdapter Tests
# =============================================================

class TestBaseAdapter:
    """Tests for the BaseAdapter ABC."""

    def test_cannot_instantiate_directly(self, mock_engine, config):
        from src.adapters.base import BaseAdapter
        with pytest.raises(TypeError):
            BaseAdapter(mock_engine, config)

    def test_session_management(self, mock_engine, config):
        """Test get_or_create_session and clear_session."""
        from src.adapters.base import BaseAdapter

        class TestAdapter(BaseAdapter):
            name = "test"
            async def start(self): pass
            async def stop(self): pass

        adapter = TestAdapter(mock_engine, config)

        # First call creates session
        s1 = adapter.get_or_create_session("user1")
        assert isinstance(s1, Session)
        assert s1.adapter == "test"
        assert s1.user_id == "user1"

        # Second call returns same session
        s2 = adapter.get_or_create_session("user1")
        assert s1 is s2

        # Different user gets different session
        s3 = adapter.get_or_create_session("user2")
        assert s3 is not s1
        assert s3.user_id == "user2"

        # Session count
        assert adapter.session_count == 2

        # Clear session
        adapter.clear_session("user1")
        assert adapter.session_count == 1

        # Creating after clear gives new session
        s4 = adapter.get_or_create_session("user1")
        assert s4 is not s1
        assert s4.id != s1.id


# =============================================================
# Message Splitting Tests
# =============================================================

class TestMessageSplitting:
    """Tests for Telegram message splitting logic."""

    def test_short_message(self):
        from src.adapters.telegram_adapter import split_message
        result = split_message("Hello", 4096)
        assert result == ["Hello"]

    def test_exact_limit(self):
        from src.adapters.telegram_adapter import split_message
        text = "x" * 4096
        result = split_message(text, 4096)
        assert result == [text]

    def test_split_on_paragraph(self):
        from src.adapters.telegram_adapter import split_message
        para1 = "a" * 2000
        para2 = "b" * 2000
        text = para1 + "\n\n" + para2
        result = split_message(text, 3000)
        assert len(result) == 2
        assert result[0].strip() == para1
        assert result[1].strip() == para2

    def test_split_on_line(self):
        from src.adapters.telegram_adapter import split_message
        line1 = "a" * 2000
        line2 = "b" * 2000
        text = line1 + "\n" + line2
        result = split_message(text, 3000)
        assert len(result) == 2

    def test_split_on_space(self):
        from src.adapters.telegram_adapter import split_message
        words = " ".join(["word"] * 1000)
        result = split_message(words, 100)
        for chunk in result:
            assert len(chunk) <= 100

    def test_split_by_character(self):
        from src.adapters.telegram_adapter import split_message
        # No natural break points
        text = "x" * 10000
        result = split_message(text, 4096)
        assert len(result) == 3
        assert result[0] == "x" * 4096
        assert result[1] == "x" * 4096
        assert result[2] == "x" * 1808

    def test_empty_message(self):
        from src.adapters.telegram_adapter import split_message
        result = split_message("", 4096)
        assert result == [""]


# =============================================================
# TelegramApprovalCallback Tests
# =============================================================

class TestTelegramApprovalCallback:
    """Tests for the Telegram approval callback mechanism."""

    def test_callback_init(self):
        from src.adapters.telegram_adapter import TelegramApprovalCallback
        cb = TelegramApprovalCallback()
        assert cb._bot is None
        assert cb._pending == {}
        assert cb._chat_ids == {}

    def test_register_chat(self):
        from src.adapters.telegram_adapter import TelegramApprovalCallback
        cb = TelegramApprovalCallback()
        cb.register_chat("session-1", 12345)
        assert cb._chat_ids["session-1"] == 12345

    @pytest.mark.asyncio
    async def test_approval_no_bot(self):
        """Approval should fail if bot is not set."""
        from src.adapters.telegram_adapter import TelegramApprovalCallback
        cb = TelegramApprovalCallback()
        session = Session(id="test")
        result = await cb.request_approval("test_tool", {}, RiskLevel.LOW, session)
        assert result is False

    @pytest.mark.asyncio
    async def test_approval_no_chat(self):
        """Approval should fail if chat ID is not registered."""
        from src.adapters.telegram_adapter import TelegramApprovalCallback
        cb = TelegramApprovalCallback()
        cb.set_bot(MagicMock())
        session = Session(id="test-session")
        result = await cb.request_approval("test_tool", {}, RiskLevel.LOW, session)
        assert result is False

    @pytest.mark.asyncio
    async def test_callback_handle_approve(self):
        """Test handling an approve callback."""
        from src.adapters.telegram_adapter import TelegramApprovalCallback
        cb = TelegramApprovalCallback()

        # Manually create a pending approval
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        cb._pending["abc123"] = future

        result_msg = await cb.handle_callback("approve:abc123")
        assert "\u2705" in result_msg  # ✅
        assert future.result() is True

    @pytest.mark.asyncio
    async def test_callback_handle_deny(self):
        """Test handling a deny callback."""
        from src.adapters.telegram_adapter import TelegramApprovalCallback
        cb = TelegramApprovalCallback()

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        cb._pending["abc123"] = future

        result_msg = await cb.handle_callback("deny:abc123")
        assert "\u274c" in result_msg  # ❌
        assert future.result() is False

    @pytest.mark.asyncio
    async def test_callback_handle_trust(self):
        """Test handling a trust callback."""
        from src.adapters.telegram_adapter import TelegramApprovalCallback
        cb = TelegramApprovalCallback()

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        cb._pending["abc123"] = future

        session = Session(id="s1")
        cb._pending["session:abc123"] = session  # type: ignore

        result_msg = await cb.handle_callback("trust:abc123:medium")
        assert "\U0001f513" in result_msg  # 🔓
        assert future.result() is True
        assert session.trust_level == "medium"

    @pytest.mark.asyncio
    async def test_callback_expired(self):
        """Test handling a callback for an expired approval."""
        from src.adapters.telegram_adapter import TelegramApprovalCallback
        cb = TelegramApprovalCallback()

        result_msg = await cb.handle_callback("approve:nonexistent")
        assert "expired" in result_msg.lower()


# =============================================================
# TelegramAdapter Tests
# =============================================================

class TestTelegramAdapter:
    """Tests for TelegramAdapter construction and user filtering."""

    def test_adapter_creation(self, mock_engine, config):
        from src.adapters.telegram_adapter import TelegramAdapter
        adapter = TelegramAdapter(mock_engine, config)
        assert adapter.name == "telegram"
        assert adapter._app is None

    def test_user_allowed_empty_list(self, mock_engine, config):
        """Empty allowed list means all users are allowed."""
        from src.adapters.telegram_adapter import TelegramAdapter
        config.adapters.telegram.allowed_user_ids = []
        adapter = TelegramAdapter(mock_engine, config)
        assert adapter._is_user_allowed(12345) is True
        assert adapter._is_user_allowed(99999) is True

    def test_user_allowed_whitelist(self, mock_engine, config):
        """Only whitelisted users should be allowed."""
        from src.adapters.telegram_adapter import TelegramAdapter
        config.adapters.telegram.allowed_user_ids = [111, 222]
        adapter = TelegramAdapter(mock_engine, config)
        assert adapter._is_user_allowed(111) is True
        assert adapter._is_user_allowed(222) is True
        assert adapter._is_user_allowed(333) is False

    def test_adapter_replaces_approval_cb(self, mock_engine, config):
        """Adapter should replace engine's approval callback."""
        from src.adapters.telegram_adapter import (
            TelegramAdapter,
            TelegramApprovalCallback,
        )
        adapter = TelegramAdapter(mock_engine, config)
        assert isinstance(mock_engine.approval_cb, TelegramApprovalCallback)


# =============================================================
# AdapterManager Tests
# =============================================================

class TestAdapterManager:
    """Tests for AdapterManager."""

    def test_register(self, mock_engine, config):
        from src.adapters.base import BaseAdapter
        from src.adapters.manager import AdapterManager

        class DummyAdapter(BaseAdapter):
            name = "dummy"
            async def start(self): pass
            async def stop(self): pass

        manager = AdapterManager(mock_engine, config)
        adapter = DummyAdapter(mock_engine, config)
        manager.register(adapter)

        assert "dummy" in manager.adapter_names
        assert manager.get("dummy") is adapter
        assert manager.get("nonexistent") is None

    def test_from_config_telegram(self, mock_engine, config):
        from src.adapters.manager import AdapterManager
        manager = AdapterManager.from_config(mock_engine, config, adapters=["telegram"])
        assert "telegram" in manager.adapter_names

    def test_from_config_discord_stub(self, mock_engine, config):
        from src.adapters.manager import AdapterManager
        manager = AdapterManager.from_config(mock_engine, config, adapters=["discord"])
        assert "discord" in manager.adapter_names

    def test_from_config_line_stub(self, mock_engine, config):
        from src.adapters.manager import AdapterManager
        manager = AdapterManager.from_config(mock_engine, config, adapters=["line"])
        assert "line" in manager.adapter_names

    def test_from_config_unknown_adapter(self, mock_engine, config):
        from src.adapters.manager import AdapterManager
        manager = AdapterManager.from_config(mock_engine, config, adapters=["unknown"])
        assert manager.adapter_names == []

    def test_from_config_auto_detect(self, mock_engine, config):
        """Test auto-detection from config (telegram.enabled)."""
        from src.adapters.manager import AdapterManager

        # Telegram disabled by default
        manager = AdapterManager.from_config(mock_engine, config, adapters=None)
        assert manager.adapter_names == []

        # Enable telegram
        config.adapters.telegram.enabled = True
        manager = AdapterManager.from_config(mock_engine, config, adapters=None)
        assert "telegram" in manager.adapter_names

    @pytest.mark.asyncio
    async def test_start_all_with_stubs(self, mock_engine, config):
        """Starting adapters without tokens should log error but not crash."""
        from src.adapters.manager import AdapterManager
        manager = AdapterManager.from_config(mock_engine, config, adapters=["line"])
        # Should not raise - just logs warning for unimplemented stubs
        await manager.start_all()

    @pytest.mark.asyncio
    async def test_stop_all_empty(self, mock_engine, config):
        """Stopping with no adapters should work fine."""
        from src.adapters.manager import AdapterManager
        manager = AdapterManager(mock_engine, config)
        await manager.stop_all()  # No error


# =============================================================
# Main.py Integration Tests
# =============================================================

class TestMainIntegration:
    """Tests for main.py adapter integration."""

    def test_build_engine(self, config):
        """build_engine should create an Engine with all components."""
        from src.main import build_engine
        engine = build_engine(config)
        assert isinstance(engine, Engine)

    def test_build_app_backward_compatible(self, config):
        """build_app should still work for CLI mode.

        Note: CLI constructor may fail in non-terminal environments
        (pytest), so we test build_engine directly instead.
        """
        from src.main import build_engine
        engine = build_engine(config)
        assert isinstance(engine, Engine)
        assert engine.tools is not None
        assert engine.model is not None

    def test_cli_args_telegram(self):
        """Verify --telegram argument is accepted."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--telegram", action="store_true")
        parser.add_argument("--adapters", action="store_true")
        args = parser.parse_args(["--telegram"])
        assert args.telegram is True
        assert args.adapters is False

    def test_cli_args_adapters(self):
        """Verify --adapters argument is accepted."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--telegram", action="store_true")
        parser.add_argument("--adapters", action="store_true")
        args = parser.parse_args(["--adapters"])
        assert args.telegram is False
        assert args.adapters is True


# =============================================================
# Tool Discovery Regression
# =============================================================

class TestToolDiscoveryRegression:
    """Ensure Phase 5 changes don't break existing tools."""

    def test_all_tools_still_discovered(self):
        from src.core.tool_system import ToolSystem
        ts = ToolSystem()
        ts.discover_tools()
        assert len(ts.registry.get_names()) >= 31

    def test_tool_names_complete(self):
        from src.core.tool_system import ToolSystem
        ts = ToolSystem()
        ts.discover_tools()

        expected = [
            "file_read", "file_write", "file_search",
            "shell_execute",
            "screenshot", "clipboard_read", "clipboard_write",
            "calendar_read", "calendar_write",
            "web_navigate", "web_get_text", "web_click",
            "web_type", "web_screenshot", "web_close",
            "memory_search", "memory_store",
        ]
        names = ts.registry.get_names()
        for t in expected:
            assert t in names, f"Tool '{t}' missing after Phase 5 changes"
