"""Browser tools: web navigation and interaction via Playwright.

Provides a lazy-loaded global BrowserManager that shares a single
browser/page instance. Tools: web_navigate, web_get_text, web_click,
web_type, web_screenshot.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import structlog

from src.config import get_kuro_home
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

logger = structlog.get_logger()


class BrowserManager:
    """Global browser manager using Playwright (lazy-loaded).

    Maintains a single browser + page instance. The browser is launched
    on the first tool call and reused until explicitly closed.
    """

    _instance: BrowserManager | None = None

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._page = None
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> BrowserManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def ensure_page(self):
        """Ensure browser and page are initialized. Returns the page."""
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page

            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=False,  # Visible browser for user interaction
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
                context = await self._browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                self._page = await context.new_page()

                logger.info("browser_started", headless=False)
                return self._page

            except Exception as e:
                logger.error("browser_start_failed", error=str(e))
                raise

    async def close(self) -> None:
        """Close the browser and cleanup resources."""
        async with self._lock:
            if self._page and not self._page.is_closed():
                await self._page.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()

            self._page = None
            self._browser = None
            self._playwright = None
            logger.info("browser_closed")

    @property
    def is_active(self) -> bool:
        return self._page is not None and not self._page.is_closed()


def _truncate_text(text: str, max_len: int = 50_000) -> str:
    """Truncate text to a maximum length with a marker."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n\n... (truncated, total {len(text)} chars)"


class WebNavigateTool(BaseTool):
    """Navigate the browser to a URL."""

    name = "web_navigate"
    description = (
        "Open a URL in the browser. Returns the page title and a text "
        "summary of the page content. Use this to visit websites."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to (e.g., https://example.com)",
            },
            "wait_for": {
                "type": "string",
                "description": (
                    "Wait condition: 'load' (default), 'domcontentloaded', "
                    "'networkidle'"
                ),
            },
        },
        "required": ["url"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        url = params.get("url", "")
        wait_for = params.get("wait_for", "load")

        if not url:
            return ToolResult.fail("URL is required")

        # Auto-add https:// if no protocol
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            manager = BrowserManager.get_instance()
            page = await manager.ensure_page()

            await page.goto(url, wait_until=wait_for, timeout=30000)
            title = await page.title()

            # Get text content (structured)
            text = await page.inner_text("body")
            text = _truncate_text(text.strip(), context.max_output_size)

            return ToolResult.ok(
                f"Page loaded: {title}\nURL: {page.url}\n\n{text}",
                title=title,
                url=page.url,
            )

        except ImportError:
            return ToolResult.fail(
                "Playwright not installed. Install with: pip install playwright && playwright install chromium"
            )
        except Exception as e:
            return ToolResult.fail(f"Navigation failed: {e}")


class WebGetTextTool(BaseTool):
    """Get the full text content of the current page."""

    name = "web_get_text"
    description = (
        "Get the full text content of the currently loaded web page. "
        "Use this to read the content of a page after navigating to it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": (
                    "Optional CSS selector to get text from a specific element "
                    "(e.g., 'article', 'main', '#content'). Default: entire body."
                ),
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        selector = params.get("selector", "body")

        try:
            manager = BrowserManager.get_instance()
            if not manager.is_active:
                return ToolResult.fail("No page is currently loaded. Use web_navigate first.")

            page = await manager.ensure_page()
            title = await page.title()

            # Try to get text from selector
            try:
                element = await page.query_selector(selector)
                if element:
                    text = await element.inner_text()
                else:
                    text = await page.inner_text("body")
                    selector = "body (fallback)"
            except Exception:
                text = await page.inner_text("body")
                selector = "body (fallback)"

            text = _truncate_text(text.strip(), context.max_output_size)

            return ToolResult.ok(
                f"Page: {title}\nSelector: {selector}\n\n{text}",
                title=title,
                url=page.url,
            )

        except Exception as e:
            return ToolResult.fail(f"Failed to get page text: {e}")


class WebClickTool(BaseTool):
    """Click on an element in the current page."""

    name = "web_click"
    description = (
        "Click on an element in the current web page. You can target by "
        "CSS selector or by visible text content. Use this to interact "
        "with links, buttons, and other clickable elements."
    )
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": (
                    "CSS selector of the element to click "
                    "(e.g., 'button.submit', '#login-btn', 'a[href=\"/about\"]')"
                ),
            },
            "text": {
                "type": "string",
                "description": (
                    "Visible text of the element to click. "
                    "Will find the first matching element with this text."
                ),
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        selector = params.get("selector")
        text = params.get("text")

        if not selector and not text:
            return ToolResult.fail("Either 'selector' or 'text' is required")

        try:
            manager = BrowserManager.get_instance()
            if not manager.is_active:
                return ToolResult.fail("No page is currently loaded. Use web_navigate first.")

            page = await manager.ensure_page()

            if text:
                # Find by text content
                element = page.get_by_text(text, exact=False).first
                await element.click(timeout=10000)
                clicked_desc = f"text='{text}'"
            else:
                await page.click(selector, timeout=10000)
                clicked_desc = f"selector='{selector}'"

            # Wait a moment for navigation/interaction
            await page.wait_for_load_state("domcontentloaded", timeout=10000)

            new_title = await page.title()
            new_url = page.url

            return ToolResult.ok(
                f"Clicked: {clicked_desc}\n"
                f"Current page: {new_title}\n"
                f"URL: {new_url}",
                title=new_title,
                url=new_url,
            )

        except Exception as e:
            return ToolResult.fail(f"Click failed: {e}")


class WebTypeTool(BaseTool):
    """Type text into an input field on the current page."""

    name = "web_type"
    description = (
        "Type text into an input field on the current web page. "
        "Can target by CSS selector. Use this to fill forms, search boxes, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": (
                    "CSS selector of the input element "
                    "(e.g., 'input[name=\"search\"]', '#email', 'textarea')"
                ),
            },
            "text": {
                "type": "string",
                "description": "The text to type into the input field",
            },
            "clear_first": {
                "type": "boolean",
                "description": "Clear the input before typing (default: true)",
            },
            "press_enter": {
                "type": "boolean",
                "description": "Press Enter after typing (default: false)",
            },
        },
        "required": ["selector", "text"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        selector = params.get("selector", "")
        text = params.get("text", "")
        clear_first = params.get("clear_first", True)
        press_enter = params.get("press_enter", False)

        if not selector:
            return ToolResult.fail("Selector is required")
        if not text:
            return ToolResult.fail("Text is required")

        try:
            manager = BrowserManager.get_instance()
            if not manager.is_active:
                return ToolResult.fail("No page is currently loaded. Use web_navigate first.")

            page = await manager.ensure_page()

            if clear_first:
                await page.fill(selector, text, timeout=10000)
            else:
                await page.type(selector, text, timeout=10000)

            result_msg = f"Typed into '{selector}': {text[:100]}"
            if len(text) > 100:
                result_msg += "..."

            if press_enter:
                await page.press(selector, "Enter")
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                result_msg += " (+ Enter)"

            new_title = await page.title()

            return ToolResult.ok(
                f"{result_msg}\n"
                f"Current page: {new_title}\n"
                f"URL: {page.url}",
                title=new_title,
                url=page.url,
            )

        except Exception as e:
            return ToolResult.fail(f"Type failed: {e}")


class WebScreenshotTool(BaseTool):
    """Take a screenshot of the current browser page."""

    name = "web_screenshot"
    description = (
        "Capture a screenshot of the current web page as displayed in "
        "the browser. The screenshot is saved as a PNG file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "full_page": {
                "type": "boolean",
                "description": "Capture the full scrollable page (default: false, viewport only)",
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        full_page = params.get("full_page", False)

        try:
            manager = BrowserManager.get_instance()
            if not manager.is_active:
                return ToolResult.fail("No page is currently loaded. Use web_navigate first.")

            page = await manager.ensure_page()

            # Save screenshot
            screenshots_dir = get_kuro_home() / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"web_{timestamp}.png"
            filepath = screenshots_dir / filename

            await page.screenshot(
                path=str(filepath),
                full_page=full_page,
            )

            title = await page.title()
            file_size = filepath.stat().st_size

            return ToolResult.ok(
                f"Web screenshot saved: {filepath}\n"
                f"Page: {title}\n"
                f"URL: {page.url}\n"
                f"Size: {file_size / 1024:.1f} KB\n"
                f"Full page: {full_page}",
                path=str(filepath),
                title=title,
                url=page.url,
                file_size=file_size,
            )

        except Exception as e:
            return ToolResult.fail(f"Web screenshot failed: {e}")


class WebCloseTool(BaseTool):
    """Close the browser."""

    name = "web_close"
    description = (
        "Close the browser and free resources. "
        "Use this when done browsing."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            manager = BrowserManager.get_instance()
            if not manager.is_active:
                return ToolResult.ok("Browser is not running.")

            await manager.close()
            return ToolResult.ok("Browser closed.")

        except Exception as e:
            return ToolResult.fail(f"Failed to close browser: {e}")
