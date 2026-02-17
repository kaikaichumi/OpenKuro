"""Screenshot tool: capture screen using mss + Pillow."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from src.config import get_kuro_home
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class ScreenshotTool(BaseTool):
    """Capture a screenshot of the screen or a specific monitor."""

    name = "screenshot"
    description = (
        "Take a screenshot of the entire screen or a specific monitor. "
        "The screenshot is saved as a PNG file and the file path is returned. "
        "Use this to see what's currently on the user's screen."
    )
    parameters = {
        "type": "object",
        "properties": {
            "monitor": {
                "type": "integer",
                "description": (
                    "Monitor number to capture (0 = all monitors combined, "
                    "1 = primary monitor, 2 = secondary, etc.). Default: 1"
                ),
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        monitor = params.get("monitor", 1)

        try:
            import mss
            from PIL import Image

            # Ensure screenshots directory exists
            screenshots_dir = get_kuro_home() / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)

            # Generate filename with timestamp
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{timestamp}.png"
            filepath = screenshots_dir / filename

            with mss.mss() as sct:
                # Validate monitor number
                if monitor < 0 or monitor >= len(sct.monitors):
                    available = len(sct.monitors) - 1  # Exclude the "all" virtual monitor
                    return ToolResult.fail(
                        f"Invalid monitor number {monitor}. "
                        f"Available monitors: 0 (all), 1-{available}"
                    )

                # Capture the screenshot
                sct_img = sct.grab(sct.monitors[monitor])

                # Convert to PIL Image and save as PNG
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                img.save(str(filepath), "PNG", optimize=True)

                width, height = img.size
                file_size = filepath.stat().st_size

            return ToolResult.ok(
                f"Screenshot saved: {filepath}\n"
                f"Resolution: {width}x{height}\n"
                f"Size: {file_size / 1024:.1f} KB",
                image_path=str(filepath),
                path=str(filepath),
                width=width,
                height=height,
                file_size=file_size,
            )

        except ImportError as e:
            return ToolResult.fail(
                f"Screenshot dependencies not installed: {e}. "
                "Install with: pip install mss Pillow"
            )
        except Exception as e:
            return ToolResult.fail(f"Screenshot failed: {e}")
