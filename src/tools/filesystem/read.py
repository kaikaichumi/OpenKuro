"""File read tool: read file contents within sandbox boundaries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class FileReadTool(BaseTool):
    """Read the contents of a file."""

    name = "file_read"
    description = (
        "Read the contents of a file at a given path. "
        "Returns the text content of the file. "
        "Use this when you need to examine file contents."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The file path to read",
            },
            "max_lines": {
                "type": "integer",
                "description": "Maximum number of lines to read (default: all)",
            },
        },
        "required": ["path"],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        path = params.get("path", "")
        max_lines = params.get("max_lines")

        if not path:
            return ToolResult.fail("Path is required")

        expanded = os.path.expanduser(path)
        resolved = Path(expanded).resolve()

        if not resolved.exists():
            return ToolResult.fail(f"File not found: {path}")

        if not resolved.is_file():
            return ToolResult.fail(f"Not a file: {path}")

        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")

            if max_lines and max_lines > 0:
                lines = content.splitlines(keepends=True)
                content = "".join(lines[:max_lines])
                if len(lines) > max_lines:
                    content += f"\n... ({len(lines) - max_lines} more lines)"

            # Respect output size limit
            if len(content) > context.max_output_size:
                content = content[: context.max_output_size] + "\n... (truncated)"

            return ToolResult.ok(
                content,
                path=str(resolved),
                size=resolved.stat().st_size,
            )
        except Exception as e:
            return ToolResult.fail(f"Error reading file: {e}")
