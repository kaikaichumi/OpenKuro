"""Computer Use tool: screenshot-driven desktop automation loop.

This tool kicks off the computer use workflow by taking an initial
screenshot and instructing the LLM to use mouse_action / keyboard_action
tools to complete the given task. The existing agent loop in engine.py
naturally supports multi-round tool calls, so no special loop is needed.

DPI scaling: coordinates are auto-converted to logical pixels so they
can be used directly with mouse_action, regardless of display scaling.
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
        "After calling this, use screenshot + analyze_image + mouse_action / keyboard_action "
        "tools to complete the task step by step. "
        "Works with vision models (raw image) and text-only models (auto OCR analysis). "
        "All coordinates are in logical pixels and can be used directly with mouse_action."
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
                phys_w, phys_h = img.size

            # Detect DPI scaling
            from src.tools.screen.dpi import get_dpi_scale
            scale = get_dpi_scale()
            logical_w = int(phys_w / scale)
            logical_h = int(phys_h / scale)

            dpi_note = ""
            if scale != 1.0:
                dpi_note = (
                    f"\nDPI scaling: {scale}x detected "
                    f"(physical: {phys_w}x{phys_h}, logical: {logical_w}x{logical_h})\n"
                    f"All coordinates from analyze_image are pre-converted to logical pixels.\n"
                )

            guide = (
                f"[Computer Use Mode]\n"
                f"Task: {task}\n"
                f"Screen: {logical_w}x{logical_h} (logical pixels){dpi_note}\n"
                f"## Workflow\n"
                f"1. Look at the screenshot (image or OCR analysis below)\n"
                f"2. Call analyze_image if you need precise click coordinates\n"
                f"3. Use mouse_action(action='click', x=..., y=...) to interact\n"
                f"4. Use keyboard_action to type text or press keys\n"
                f"5. Call screenshot again to verify the result\n"
                f"6. Repeat until the task is complete\n\n"
                f"## Coordinate Rules\n"
                f"- All coordinates are in LOGICAL pixels (use directly with mouse_action)\n"
                f"- analyze_image returns a 'Click Targets' section with exact coordinates\n"
                f"- Example: see '\"OK\" -> mouse_action(action=\"click\", x=500, y=300)'\n"
                f"  then call: mouse_action(action='click', x=500, y=300)\n"
                f"- For CJK/Unicode text input, keyboard_action(action='type') handles it\n\n"
                f"## Tips\n"
                f"- Always call screenshot AFTER each action to verify the result\n"
                f"- Wait 0.5-1s between actions for UI to respond\n"
                f"- If a click doesn't work, try analyze_image for more accurate coordinates\n"
                f"- Use screen_info to check current mouse position"
            )

            return ToolResult.ok(
                guide,
                image_path=str(filepath),
                task=task,
                width=logical_w,
                height=logical_h,
                physical_width=phys_w,
                physical_height=phys_h,
                dpi_scale=scale,
            )

        except ImportError as e:
            return ToolResult.fail(
                f"Screenshot dependencies not installed: {e}. "
                "Install with: pip install mss Pillow"
            )
        except Exception as e:
            return ToolResult.fail(f"Computer use initialization failed: {e}")
