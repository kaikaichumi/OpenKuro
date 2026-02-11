"""Clipboard tools: read and write system clipboard.

On Windows, uses PowerShell Get-Clipboard / Set-Clipboard for reliability.
On macOS, uses pbcopy/pbpaste.
On Linux, uses xclip or xsel.
"""

from __future__ import annotations

import platform
import subprocess
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


def _read_clipboard_windows() -> str:
    """Read clipboard text on Windows using PowerShell."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PowerShell Get-Clipboard failed: {result.stderr}")
    return result.stdout.rstrip("\r\n")


def _write_clipboard_windows(text: str) -> None:
    """Write text to clipboard on Windows using PowerShell via stdin pipe."""
    # Pipe text via stdin to avoid argument parsing issues with spaces/special chars
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "$input | Set-Clipboard",
        ],
        input=text,
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PowerShell Set-Clipboard failed: {result.stderr}")


def _read_clipboard_unix() -> str:
    """Read clipboard text on Linux/macOS using subprocess."""
    system = platform.system()
    if system == "Darwin":
        cmd = ["pbpaste"]
    else:
        # Try xclip, then xsel
        try:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout
        except FileNotFoundError:
            cmd = ["xsel", "--clipboard", "--output"]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    return result.stdout


def _write_clipboard_unix(text: str) -> None:
    """Write text to clipboard on Linux/macOS using subprocess."""
    system = platform.system()
    if system == "Darwin":
        cmd = ["pbcopy"]
    else:
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text, text=True, timeout=5, check=True,
            )
            return
        except FileNotFoundError:
            cmd = ["xsel", "--clipboard", "--input"]

    subprocess.run(cmd, input=text, text=True, timeout=5, check=True)


class ClipboardReadTool(BaseTool):
    """Read the current system clipboard contents."""

    name = "clipboard_read"
    description = (
        "Read the current text content from the system clipboard. "
        "Returns the text that was last copied."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            system = platform.system()
            if system == "Windows":
                text = _read_clipboard_windows()
            else:
                text = _read_clipboard_unix()

            if not text:
                return ToolResult.ok("Clipboard is empty.", content="")

            # Truncate if very long
            max_len = context.max_output_size
            truncated = len(text) > max_len
            display_text = text[:max_len] if truncated else text

            msg = f"Clipboard content ({len(text)} chars):\n{display_text}"
            if truncated:
                msg += f"\n... (truncated, total {len(text)} chars)"

            return ToolResult.ok(msg, content=text[:max_len], length=len(text))

        except Exception as e:
            return ToolResult.fail(f"Failed to read clipboard: {e}")


class ClipboardWriteTool(BaseTool):
    """Write text to the system clipboard."""

    name = "clipboard_write"
    description = (
        "Copy text to the system clipboard. "
        "This will overwrite any existing clipboard content."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to copy to the clipboard",
            },
        },
        "required": ["text"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        text = params.get("text", "")
        if not text:
            return ToolResult.fail("Text is required")

        try:
            system = platform.system()
            if system == "Windows":
                _write_clipboard_windows(text)
            else:
                _write_clipboard_unix(text)

            preview = text[:100]
            if len(text) > 100:
                preview += "..."

            return ToolResult.ok(
                f"Copied to clipboard ({len(text)} chars): {preview}",
                length=len(text),
            )

        except Exception as e:
            return ToolResult.fail(f"Failed to write to clipboard: {e}")
