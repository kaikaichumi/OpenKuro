"""File search tool: search for files using glob patterns."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class FileSearchTool(BaseTool):
    """Search for files matching a glob pattern."""

    name = "file_search"
    description = (
        "Search for files matching a glob pattern in a directory. "
        "Returns a list of matching file paths. "
        "Examples: '*.py', '**/*.txt', 'docs/*.md'"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match files (e.g., '*.py', '**/*.txt')",
            },
            "directory": {
                "type": "string",
                "description": "Directory to search in (default: current directory)",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 50)",
            },
        },
        "required": ["pattern"],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = params.get("pattern", "")
        directory = params.get("directory", ".")
        max_results = params.get("max_results", 50)

        if not pattern:
            return ToolResult.fail("Pattern is required")

        expanded = os.path.expanduser(directory)
        search_dir = Path(expanded).resolve()

        if not search_dir.exists():
            return ToolResult.fail(f"Directory not found: {directory}")

        if not search_dir.is_dir():
            return ToolResult.fail(f"Not a directory: {directory}")

        try:
            matches = []
            for match in search_dir.glob(pattern):
                matches.append(str(match))
                if len(matches) >= max_results:
                    break

            if not matches:
                return ToolResult.ok(
                    f"No files matching '{pattern}' in {search_dir}",
                    count=0,
                )

            result_text = "\n".join(matches)
            total_note = ""
            if len(matches) >= max_results:
                total_note = f"\n(showing first {max_results} results)"

            return ToolResult.ok(
                f"Found {len(matches)} files:{total_note}\n{result_text}",
                count=len(matches),
                files=matches,
            )
        except Exception as e:
            return ToolResult.fail(f"Search error: {e}")
