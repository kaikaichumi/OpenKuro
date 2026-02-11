"""Memory store tool: save facts and information to long-term memory."""

from __future__ import annotations

from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

# Shared with search.py
from src.tools.memory_tools.search import _memory_manager, set_memory_manager


class MemoryStoreTool(BaseTool):
    """Store a fact or piece of information in long-term memory."""

    name = "memory_store"
    description = (
        "Store a fact, preference, or piece of information in long-term memory. "
        "This will be remembered across sessions and can be retrieved later. "
        "Use this when the user tells you something important to remember."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact or information to remember",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for categorization (e.g., ['preference', 'work'])",
            },
            "save_to_file": {
                "type": "boolean",
                "description": "Also save to MEMORY.md for human viewing (default: false)",
            },
        },
        "required": ["content"],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        from src.tools.memory_tools.search import _memory_manager

        if _memory_manager is None:
            return ToolResult.fail("Memory system not initialized")

        content = params.get("content", "")
        tags = params.get("tags", [])
        save_to_file = params.get("save_to_file", False)

        if not content:
            return ToolResult.fail("Content is required")

        try:
            memory_id = await _memory_manager.store_fact(
                content=content,
                tags=tags,
                also_write_md=save_to_file,
                md_section="Facts",
            )

            result_msg = f"Stored in memory (ID: {memory_id[:8]}...)"
            if save_to_file:
                result_msg += " and written to MEMORY.md"

            return ToolResult.ok(result_msg, memory_id=memory_id)
        except Exception as e:
            return ToolResult.fail(f"Memory store error: {e}")
