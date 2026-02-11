"""File write tool: create or overwrite files within sandbox boundaries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class FileWriteTool(BaseTool):
    """Write content to a file (create or overwrite)."""

    name = "file_write"
    description = (
        "Write text content to a file. Creates the file if it doesn't exist, "
        "or overwrites it if it does. Use 'append' mode to add to existing content."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The file path to write to",
            },
            "content": {
                "type": "string",
                "description": "The text content to write",
            },
            "append": {
                "type": "boolean",
                "description": "If true, append to existing file instead of overwriting (default: false)",
            },
        },
        "required": ["path", "content"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        path = params.get("path", "")
        content = params.get("content", "")
        append = params.get("append", False)

        if not path:
            return ToolResult.fail("Path is required")

        expanded = os.path.expanduser(path)
        resolved = Path(expanded).resolve()

        # Ensure parent directory exists
        if not resolved.parent.exists():
            try:
                resolved.parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return ToolResult.fail(f"Cannot create directory: {e}")

        try:
            mode = "a" if append else "w"
            with open(resolved, mode, encoding="utf-8") as f:
                f.write(content)

            action = "appended to" if append else "written to"
            size = resolved.stat().st_size
            return ToolResult.ok(
                f"Successfully {action} {resolved} ({size} bytes)",
                path=str(resolved),
                size=size,
            )
        except Exception as e:
            return ToolResult.fail(f"Error writing file: {e}")
