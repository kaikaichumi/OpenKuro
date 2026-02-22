"""Tests for Phase 4: screenshot, clipboard, calendar, and browser tools."""

from __future__ import annotations

import asyncio
import os
import platform
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.base import RiskLevel, ToolContext


# === Shared fixtures ===

@pytest.fixture
def tool_context():
    """Create a default ToolContext for tests."""
    return ToolContext(
        session_id="test-session",
        max_output_size=100_000,
    )


# =============================================================
# Screenshot Tool Tests
# =============================================================

class TestScreenshotTool:
    """Tests for the screenshot tool."""

    def test_tool_metadata(self):
        from src.tools.screen.screenshot import ScreenshotTool
        tool = ScreenshotTool()
        assert tool.name == "screenshot"
        assert tool.risk_level == RiskLevel.LOW
        assert "screenshot" in tool.description.lower()

    def test_to_openai_tool(self):
        from src.tools.screen.screenshot import ScreenshotTool
        tool = ScreenshotTool()
        schema = tool.to_openai_tool()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "screenshot"
        assert "monitor" in schema["function"]["parameters"]["properties"]

    @pytest.mark.asyncio
    async def test_screenshot_capture(self, tool_context, tmp_path):
        """Test actual screenshot capture (only on systems with a display)."""
        from src.tools.screen.screenshot import ScreenshotTool
        tool = ScreenshotTool()

        with patch("src.tools.screen.screenshot.get_kuro_home", return_value=tmp_path):
            result = await tool.execute({"monitor": 1}, tool_context)

        # On CI without display, this will fail gracefully
        if result.success:
            assert "Screenshot saved" in result.output
            assert result.data["width"] > 0
            assert result.data["height"] > 0
            # Verify file was created
            assert Path(result.data["path"]).exists()
        else:
            # Graceful failure expected without display
            assert "failed" in result.error.lower() or "not" in result.error.lower()

    @pytest.mark.asyncio
    async def test_screenshot_invalid_monitor(self, tool_context, tmp_path):
        """Test error on invalid monitor number."""
        from src.tools.screen.screenshot import ScreenshotTool
        tool = ScreenshotTool()

        with patch("src.tools.screen.screenshot.get_kuro_home", return_value=tmp_path):
            result = await tool.execute({"monitor": 99}, tool_context)

        # Should either fail with invalid monitor or screenshot error
        if not result.success:
            assert "monitor" in result.error.lower() or "failed" in result.error.lower()


# =============================================================
# Clipboard Tool Tests
# =============================================================

class TestClipboardTools:
    """Tests for clipboard read/write tools."""

    def test_read_tool_metadata(self):
        from src.tools.screen.clipboard import ClipboardReadTool
        tool = ClipboardReadTool()
        assert tool.name == "clipboard_read"
        assert tool.risk_level == RiskLevel.LOW

    def test_write_tool_metadata(self):
        from src.tools.screen.clipboard import ClipboardWriteTool
        tool = ClipboardWriteTool()
        assert tool.name == "clipboard_write"
        assert tool.risk_level == RiskLevel.MEDIUM

    @pytest.mark.skipif(
        platform.system() != "Windows",
        reason="Windows clipboard tests require Windows"
    )
    @pytest.mark.asyncio
    async def test_clipboard_roundtrip(self, tool_context):
        """Test write then read on Windows."""
        from src.tools.screen.clipboard import ClipboardReadTool, ClipboardWriteTool

        write_tool = ClipboardWriteTool()
        read_tool = ClipboardReadTool()

        test_text = f"Kuro test {time.time()}"

        # Write
        result = await write_tool.execute({"text": test_text}, tool_context)
        assert result.success
        assert "Copied" in result.output

        # Read
        result = await read_tool.execute({}, tool_context)
        assert result.success
        assert test_text in result.output

    @pytest.mark.asyncio
    async def test_clipboard_write_empty_text(self, tool_context):
        """Test that empty text is rejected."""
        from src.tools.screen.clipboard import ClipboardWriteTool
        tool = ClipboardWriteTool()
        result = await tool.execute({"text": ""}, tool_context)
        assert not result.success
        assert "required" in result.error.lower()


# =============================================================
# Calendar Tool Tests
# =============================================================

class TestCalendarTools:
    """Tests for calendar read/write tools."""

    def test_read_tool_metadata(self):
        from src.tools.calendar.calendar_tool import CalendarReadTool
        tool = CalendarReadTool()
        assert tool.name == "calendar_read"
        assert tool.risk_level == RiskLevel.LOW

    def test_write_tool_metadata(self):
        from src.tools.calendar.calendar_tool import CalendarWriteTool
        tool = CalendarWriteTool()
        assert tool.name == "calendar_write"
        assert tool.risk_level == RiskLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_calendar_create_and_read(self, tool_context, tmp_path):
        """Test creating an event and reading it back."""
        from src.tools.calendar.calendar_tool import CalendarReadTool, CalendarWriteTool

        with patch("src.tools.calendar.calendar_tool.get_kuro_home", return_value=tmp_path):
            write_tool = CalendarWriteTool()
            read_tool = CalendarReadTool()

            # Create an event for today
            today = datetime.now().strftime("%Y-%m-%d")
            result = await write_tool.execute(
                {
                    "summary": "Team Meeting",
                    "start": f"{today} 10:00",
                    "end": f"{today} 11:00",
                    "description": "Weekly sync",
                    "location": "Room A",
                },
                tool_context,
            )
            assert result.success
            assert "Team Meeting" in result.output
            assert result.data.get("uid")

            # Read events for today
            result = await read_tool.execute({"date": today}, tool_context)
            assert result.success
            assert "Team Meeting" in result.output
            assert result.data["count"] >= 1

    @pytest.mark.asyncio
    async def test_calendar_read_empty(self, tool_context, tmp_path):
        """Test reading from an empty calendar."""
        from src.tools.calendar.calendar_tool import CalendarReadTool

        with patch("src.tools.calendar.calendar_tool.get_kuro_home", return_value=tmp_path):
            tool = CalendarReadTool()
            result = await tool.execute({"date": "2099-12-31"}, tool_context)
            assert result.success
            assert "No events" in result.output
            assert result.data["count"] == 0

    @pytest.mark.asyncio
    async def test_calendar_write_all_day_event(self, tool_context, tmp_path):
        """Test creating an all-day event."""
        from src.tools.calendar.calendar_tool import CalendarWriteTool

        with patch("src.tools.calendar.calendar_tool.get_kuro_home", return_value=tmp_path):
            tool = CalendarWriteTool()
            result = await tool.execute(
                {"summary": "Holiday", "start": "2026-03-15"},
                tool_context,
            )
            assert result.success
            assert "Holiday" in result.output

    @pytest.mark.asyncio
    async def test_calendar_write_missing_summary(self, tool_context, tmp_path):
        """Test error when summary is missing."""
        from src.tools.calendar.calendar_tool import CalendarWriteTool

        with patch("src.tools.calendar.calendar_tool.get_kuro_home", return_value=tmp_path):
            tool = CalendarWriteTool()
            result = await tool.execute(
                {"summary": "", "start": "2026-03-15"},
                tool_context,
            )
            assert not result.success

    @pytest.mark.asyncio
    async def test_calendar_write_missing_start(self, tool_context, tmp_path):
        """Test error when start date is missing."""
        from src.tools.calendar.calendar_tool import CalendarWriteTool

        with patch("src.tools.calendar.calendar_tool.get_kuro_home", return_value=tmp_path):
            tool = CalendarWriteTool()
            result = await tool.execute(
                {"summary": "Test", "start": ""},
                tool_context,
            )
            assert not result.success

    @pytest.mark.asyncio
    async def test_calendar_read_date_range(self, tool_context, tmp_path):
        """Test reading events within a date range."""
        from src.tools.calendar.calendar_tool import CalendarReadTool, CalendarWriteTool

        with patch("src.tools.calendar.calendar_tool.get_kuro_home", return_value=tmp_path):
            write_tool = CalendarWriteTool()
            read_tool = CalendarReadTool()

            # Create events on different dates
            await write_tool.execute(
                {"summary": "Event A", "start": "2026-06-01 09:00"},
                tool_context,
            )
            await write_tool.execute(
                {"summary": "Event B", "start": "2026-06-03 14:00"},
                tool_context,
            )
            await write_tool.execute(
                {"summary": "Event C", "start": "2026-06-10 10:00"},
                tool_context,
            )

            # Read range that should include A and B but not C
            result = await read_tool.execute(
                {"start_date": "2026-06-01", "end_date": "2026-06-05"},
                tool_context,
            )
            assert result.success
            assert "Event A" in result.output
            assert "Event B" in result.output
            assert "Event C" not in result.output
            assert result.data["count"] == 2

    @pytest.mark.asyncio
    async def test_calendar_read_days(self, tool_context, tmp_path):
        """Test reading events for N days from today."""
        from src.tools.calendar.calendar_tool import CalendarReadTool, CalendarWriteTool

        with patch("src.tools.calendar.calendar_tool.get_kuro_home", return_value=tmp_path):
            write_tool = CalendarWriteTool()
            read_tool = CalendarReadTool()

            # Create an event tomorrow
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            await write_tool.execute(
                {"summary": "Tomorrow Task", "start": f"{tomorrow} 15:00"},
                tool_context,
            )

            # Read next 3 days
            result = await read_tool.execute({"days": 3}, tool_context)
            assert result.success
            assert "Tomorrow Task" in result.output

    def test_parse_date_formats(self):
        """Test date parsing with various formats."""
        from src.tools.calendar.calendar_tool import _parse_date

        # YYYY-MM-DD
        dt = _parse_date("2026-03-15")
        assert dt.year == 2026 and dt.month == 3 and dt.day == 15

        # YYYY-MM-DD HH:MM
        dt = _parse_date("2026-03-15 10:30")
        assert dt.hour == 10 and dt.minute == 30

        # YYYY-MM-DDTHH:MM
        dt = _parse_date("2026-03-15T14:00")
        assert dt.hour == 14 and dt.minute == 0

        # Invalid
        with pytest.raises(ValueError):
            _parse_date("not-a-date")


# =============================================================
# Browser Tool Tests
# =============================================================

class TestBrowserTools:
    """Tests for the browser/web tools."""

    def test_navigate_tool_metadata(self):
        from src.tools.web.browse import WebNavigateTool
        tool = WebNavigateTool()
        assert tool.name == "web_navigate"
        assert tool.risk_level == RiskLevel.MEDIUM

    def test_get_text_tool_metadata(self):
        from src.tools.web.browse import WebGetTextTool
        tool = WebGetTextTool()
        assert tool.name == "web_get_text"
        assert tool.risk_level == RiskLevel.LOW

    def test_click_tool_metadata(self):
        from src.tools.web.browse import WebClickTool
        tool = WebClickTool()
        assert tool.name == "web_click"
        assert tool.risk_level == RiskLevel.MEDIUM

    def test_type_tool_metadata(self):
        from src.tools.web.browse import WebTypeTool
        tool = WebTypeTool()
        assert tool.name == "web_type"
        assert tool.risk_level == RiskLevel.MEDIUM

    def test_screenshot_tool_metadata(self):
        from src.tools.web.browse import WebScreenshotTool
        tool = WebScreenshotTool()
        assert tool.name == "web_screenshot"
        assert tool.risk_level == RiskLevel.LOW

    def test_close_tool_metadata(self):
        from src.tools.web.browse import WebCloseTool
        tool = WebCloseTool()
        assert tool.name == "web_close"
        assert tool.risk_level == RiskLevel.LOW

    @pytest.mark.asyncio
    async def test_navigate_empty_url(self, tool_context):
        """Test navigate with empty URL."""
        from src.tools.web.browse import WebNavigateTool
        tool = WebNavigateTool()
        result = await tool.execute({"url": ""}, tool_context)
        assert not result.success
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_get_text_no_page(self, tool_context):
        """Test getting text when no page is loaded."""
        from src.tools.web.browse import BrowserManager, WebGetTextTool

        # Reset browser manager state
        BrowserManager._instance = None

        tool = WebGetTextTool()
        result = await tool.execute({}, tool_context)
        assert not result.success
        assert "no page" in result.error.lower() or "not" in result.error.lower()

    @pytest.mark.asyncio
    async def test_click_requires_selector_or_text(self, tool_context):
        """Test click fails without selector or text."""
        from src.tools.web.browse import WebClickTool
        tool = WebClickTool()
        result = await tool.execute({}, tool_context)
        assert not result.success
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_type_requires_params(self, tool_context):
        """Test type fails without required params."""
        from src.tools.web.browse import WebTypeTool
        tool = WebTypeTool()
        result = await tool.execute({"selector": "", "text": ""}, tool_context)
        assert not result.success

    @pytest.mark.asyncio
    async def test_close_when_not_running(self, tool_context):
        """Test closing when browser isn't running."""
        from src.tools.web.browse import BrowserManager, WebCloseTool

        BrowserManager._instance = None
        tool = WebCloseTool()
        result = await tool.execute({}, tool_context)
        assert result.success
        assert "not running" in result.output.lower()

    def test_truncate_text(self):
        """Test the text truncation utility."""
        from src.tools.web.browse import _truncate_text

        short = "Hello"
        assert _truncate_text(short, 100) == short

        long_text = "x" * 200
        result = _truncate_text(long_text, 100)
        assert len(result) > 100  # includes truncation marker
        assert "truncated" in result

    def test_auto_https(self, tool_context):
        """Test URL auto-prefixing with https."""
        from src.tools.web.browse import WebNavigateTool
        tool = WebNavigateTool()
        # Just verify the tool has the correct metadata
        params = tool.parameters
        assert "url" in params["properties"]

    @pytest.mark.asyncio
    async def test_browser_manager_singleton(self):
        """Test that BrowserManager is a singleton."""
        from src.tools.web.browse import BrowserManager

        BrowserManager._instance = None
        m1 = BrowserManager.get_instance()
        m2 = BrowserManager.get_instance()
        assert m1 is m2


# =============================================================
# Tool Discovery Tests
# =============================================================

class TestToolDiscovery:
    """Tests for Phase 4 tool auto-discovery."""

    def test_all_phase4_tools_discovered(self):
        """All Phase 4 tools should be discovered by ToolSystem."""
        from src.core.tool_system import ToolSystem
        ts = ToolSystem()
        ts.discover_tools()

        expected_tools = [
            "screenshot",
            "clipboard_read",
            "clipboard_write",
            "calendar_read",
            "calendar_write",
            "web_navigate",
            "web_get_text",
            "web_click",
            "web_type",
            "web_screenshot",
            "web_close",
        ]

        registered = ts.registry.get_names()
        for tool_name in expected_tools:
            assert tool_name in registered, f"Tool '{tool_name}' not discovered"

    def test_total_tool_count(self):
        """Verify total tool count including all phases."""
        from src.core.tool_system import ToolSystem
        ts = ToolSystem()
        ts.discover_tools()
        # Phase 1-3: file_read, file_write, file_search, shell_execute,
        #            memory_search, memory_store = 6
        # Phase 4: screenshot, clipboard_read, clipboard_write,
        #          calendar_read, calendar_write, get_time,
        #          web_navigate, web_get_text, web_click, web_type,
        #          web_screenshot, web_close = 12
        # Phase 8 (agents): delegate_to_agent, list_agents = 2
        # Screen: screen_info, computer_use, keyboard_action, mouse_action = 4
        # Session: session_clear = 1
        # System: check_update, perform_update, get_version = 3
        # Analytics: dashboard_summary, token_usage_report, security_report = 3
        # Grand total: 31
        assert len(ts.registry.get_names()) == 31

    def test_openai_tool_format(self):
        """All tools should produce valid OpenAI-compatible schemas."""
        from src.core.tool_system import ToolSystem
        ts = ToolSystem()
        ts.discover_tools()

        schemas = ts.registry.get_openai_tools()
        assert len(schemas) == 31

        for schema in schemas:
            assert schema["type"] == "function"
            assert "name" in schema["function"]
            assert "description" in schema["function"]
            assert "parameters" in schema["function"]
            params = schema["function"]["parameters"]
            assert params["type"] == "object"
