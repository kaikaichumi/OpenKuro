"""Desktop GUI automation tools: mouse, keyboard, screen info.

Uses pyautogui for cross-platform mouse/keyboard control.
Safety: pyautogui FAILSAFE is enabled (move mouse to top-left corner to abort).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

# Minimum interval between actions to prevent runaway automation
_MIN_ACTION_INTERVAL = 0.2
_last_action_time: float = 0


def _setup_pyautogui():
    """Import and configure pyautogui with safety settings."""
    import pyautogui

    pyautogui.FAILSAFE = True  # Move mouse to top-left to abort
    pyautogui.PAUSE = 0.1  # Small pause between actions
    return pyautogui


async def _rate_limit() -> None:
    """Enforce minimum interval between desktop actions."""
    global _last_action_time
    elapsed = time.monotonic() - _last_action_time
    if elapsed < _MIN_ACTION_INTERVAL:
        await asyncio.sleep(_MIN_ACTION_INTERVAL - elapsed)
    _last_action_time = time.monotonic()


def _validate_coordinates(x: int, y: int, pyautogui) -> str | None:
    """Validate that coordinates are within screen bounds. Returns error or None."""
    screen_w, screen_h = pyautogui.size()
    if x < 0 or x >= screen_w or y < 0 or y >= screen_h:
        return (
            f"Coordinates ({x}, {y}) out of screen bounds "
            f"(0-{screen_w - 1}, 0-{screen_h - 1})"
        )
    return None


class MouseActionTool(BaseTool):
    """Control the mouse: move, click, double-click, right-click, drag, scroll."""

    name = "mouse_action"
    description = (
        "Control the mouse cursor on the desktop. Actions: "
        "click (left-click at position), double_click, right_click, "
        "move (move cursor without clicking), drag (drag from x,y to end_x,end_y), "
        "scroll (scroll wheel at position). "
        "Use screenshot first to see the current screen and determine coordinates."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["click", "double_click", "right_click", "move", "drag", "scroll"],
                "description": "Mouse action type",
            },
            "x": {
                "type": "integer",
                "description": "Target X coordinate",
            },
            "y": {
                "type": "integer",
                "description": "Target Y coordinate",
            },
            "end_x": {
                "type": "integer",
                "description": "Drag end X coordinate (only for drag action)",
            },
            "end_y": {
                "type": "integer",
                "description": "Drag end Y coordinate (only for drag action)",
            },
            "scroll_amount": {
                "type": "integer",
                "description": "Scroll amount (positive=up, negative=down, only for scroll action)",
            },
        },
        "required": ["action", "x", "y"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        action = params.get("action", "")
        x = params.get("x", 0)
        y = params.get("y", 0)

        try:
            pyautogui = _setup_pyautogui()
        except ImportError:
            return ToolResult.fail(
                "pyautogui not installed. Install with: pip install pyautogui"
            )

        # Validate coordinates
        err = _validate_coordinates(x, y, pyautogui)
        if err:
            return ToolResult.fail(err)

        await _rate_limit()

        try:
            if action == "click":
                pyautogui.click(x, y)
                return ToolResult.ok(f"Clicked at ({x}, {y})")

            elif action == "double_click":
                pyautogui.doubleClick(x, y)
                return ToolResult.ok(f"Double-clicked at ({x}, {y})")

            elif action == "right_click":
                pyautogui.rightClick(x, y)
                return ToolResult.ok(f"Right-clicked at ({x}, {y})")

            elif action == "move":
                pyautogui.moveTo(x, y)
                return ToolResult.ok(f"Moved cursor to ({x}, {y})")

            elif action == "drag":
                end_x = params.get("end_x")
                end_y = params.get("end_y")
                if end_x is None or end_y is None:
                    return ToolResult.fail("drag action requires end_x and end_y")
                err = _validate_coordinates(end_x, end_y, pyautogui)
                if err:
                    return ToolResult.fail(f"Drag end point: {err}")
                pyautogui.moveTo(x, y)
                pyautogui.drag(end_x - x, end_y - y, duration=0.5)
                return ToolResult.ok(f"Dragged from ({x}, {y}) to ({end_x}, {end_y})")

            elif action == "scroll":
                scroll_amount = params.get("scroll_amount", 0)
                if scroll_amount == 0:
                    return ToolResult.fail("scroll action requires non-zero scroll_amount")
                pyautogui.moveTo(x, y)
                pyautogui.scroll(scroll_amount)
                direction = "up" if scroll_amount > 0 else "down"
                return ToolResult.ok(
                    f"Scrolled {direction} by {abs(scroll_amount)} at ({x}, {y})"
                )

            else:
                return ToolResult.fail(
                    f"Unknown action: {action}. "
                    "Use: click, double_click, right_click, move, drag, scroll"
                )

        except Exception as e:
            return ToolResult.fail(f"Mouse action failed: {e}")


class KeyboardActionTool(BaseTool):
    """Control the keyboard: type text, press keys, hotkey combos."""

    name = "keyboard_action"
    description = (
        "Control the keyboard on the desktop. Actions: "
        "type (type a string of text), "
        "press (press a single key like enter, tab, escape, backspace, delete, up, down, left, right, f1-f12), "
        "hotkey (press a key combination like ctrl+c, alt+tab, ctrl+shift+s). "
        "Use screenshot first to see the current screen and ensure the right window is focused."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["type", "press", "hotkey"],
                "description": "type=type text, press=press a single key, hotkey=key combination",
            },
            "text": {
                "type": "string",
                "description": "Text to type (only for type action)",
            },
            "key": {
                "type": "string",
                "description": "Key name (only for press action, e.g. enter, tab, escape)",
            },
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key combination (only for hotkey action, e.g. ['ctrl', 'c'])",
            },
        },
        "required": ["action"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        action = params.get("action", "")

        try:
            pyautogui = _setup_pyautogui()
        except ImportError:
            return ToolResult.fail(
                "pyautogui not installed. Install with: pip install pyautogui"
            )

        await _rate_limit()

        try:
            if action == "type":
                text = params.get("text", "")
                if not text:
                    return ToolResult.fail("type action requires text")
                pyautogui.write(text, interval=0.02)
                preview = text[:100] + ("..." if len(text) > 100 else "")
                return ToolResult.ok(f"Typed: {preview}")

            elif action == "press":
                key = params.get("key", "")
                if not key:
                    return ToolResult.fail("press action requires key")
                pyautogui.press(key)
                return ToolResult.ok(f"Pressed key: {key}")

            elif action == "hotkey":
                keys = params.get("keys", [])
                if not keys:
                    return ToolResult.fail("hotkey action requires keys array")
                pyautogui.hotkey(*keys)
                return ToolResult.ok(f"Hotkey: {' + '.join(keys)}")

            else:
                return ToolResult.fail(
                    f"Unknown action: {action}. Use: type, press, hotkey"
                )

        except Exception as e:
            return ToolResult.fail(f"Keyboard action failed: {e}")


class ScreenInfoTool(BaseTool):
    """Get screen information: resolution, mouse position."""

    name = "screen_info"
    description = (
        "Get current screen information including resolution and mouse cursor position. "
        "Useful before performing mouse or keyboard actions."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            pyautogui = _setup_pyautogui()
        except ImportError:
            return ToolResult.fail(
                "pyautogui not installed. Install with: pip install pyautogui"
            )

        try:
            screen_w, screen_h = pyautogui.size()
            mouse_x, mouse_y = pyautogui.position()

            return ToolResult.ok(
                f"Screen resolution: {screen_w}x{screen_h}\n"
                f"Mouse position: ({mouse_x}, {mouse_y})",
                screen_width=screen_w,
                screen_height=screen_h,
                mouse_x=mouse_x,
                mouse_y=mouse_y,
            )

        except Exception as e:
            return ToolResult.fail(f"Failed to get screen info: {e}")
