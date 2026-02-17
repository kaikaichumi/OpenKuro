"""Computer Use tool: screenshot-driven desktop automation loop.

This tool kicks off the computer use workflow by taking an initial
screenshot and instructing the LLM to use mouse_action / keyboard_action
tools to complete the given task. The existing agent loop in engine.py
naturally supports multi-round tool calls, so no special loop is needed.
"""

from __future__ import annotations

import time
from typing import Any

from src.config import get_kuro_home
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class ComputerUseTool(BaseTool):
    """Start a computer-use session: AI sees the screen and operates mouse/keyboard."""

    name = "computer_use"
    description = (
        "Start a computer-use session to control the desktop. "
        "Takes an initial screenshot and returns it with the task description. "
        "After calling this, use screenshot + mouse_action / keyboard_action "
        "tools to complete the task step by step. "
        "Requires a vision-capable model (e.g. Claude Sonnet/Opus, GPT-4o)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Description of the task to complete on the desktop",
            },
        },
        "required": ["task"],
    }
    risk_level = RiskLevel.HIGH

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        task = params.get("task", "")
        if not task:
            return ToolResult.fail("Task description is required")

        try:
            import mss
            from PIL import Image

            # Take initial screenshot
            screenshots_dir = get_kuro_home() / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"computer_use_{timestamp}.png"
            filepath = screenshots_dir / filename

            with mss.mss() as sct:
                sct_img = sct.grab(sct.monitors[1])
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                img.save(str(filepath), "PNG", optimize=True)
                width, height = img.size

            guide = (
                f"[Computer Use Mode]\n"
                f"Task: {task}\n"
                f"Screen resolution: {width}x{height}\n\n"
                f"Above is the current screen. To complete the task:\n"
                f"1. Analyze the screenshot to understand what's on screen\n"
                f"2. Use mouse_action to click, drag, or scroll\n"
                f"3. Use keyboard_action to type text or press keys\n"
                f"4. Call screenshot after each action to verify the result\n"
                f"5. Repeat until the task is complete\n\n"
                f"Tips:\n"
                f"- Use screen_info to get exact screen dimensions and mouse position\n"
                f"- Coordinates are in pixels from top-left (0,0)\n"
                f"- Wait for UI to respond after each action before taking the next screenshot"
            )

            return ToolResult.ok(
                guide,
                image_path=str(filepath),
                task=task,
                width=width,
                height=height,
            )

        except ImportError as e:
            return ToolResult.fail(
                f"Screenshot dependencies not installed: {e}. "
                "Install with: pip install mss Pillow"
            )
        except Exception as e:
            return ToolResult.fail(f"Computer use initialization failed: {e}")
