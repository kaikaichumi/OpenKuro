"""Memory search tool: search long-term memories via semantic similarity."""

from __future__ import annotations

from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

# Will be set by main.py during initialization
_memory_manager = None


def set_memory_manager(manager: Any) -> None:
    """Set the global memory manager instance (called during app init)."""
    global _memory_manager
    _memory_manager = manager


class MemorySearchTool(BaseTool):
    """Search your long-term memories for relevant facts and information."""

    name = "memory_search"
    description = (
        "Search your long-term memory for relevant facts, preferences, "
        "and previously stored information. Uses semantic similarity search. "
        "Use this when the user asks about something you might have been told before."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to find relevant memories",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default: 5)",
            },
        },
        "required": ["query"],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if _memory_manager is None:
            return ToolResult.fail("Memory system not initialized")

        query = params.get("query", "")
        top_k = params.get("top_k", 5)

        if not query:
            return ToolResult.fail("Query is required")

        try:
            results = await _memory_manager.search_memories(query, top_k=top_k)

            if not results:
                return ToolResult.ok("No relevant memories found.", count=0)

            lines = []
            for i, mem in enumerate(results, 1):
                distance = mem.get("distance", 0)
                relevance = f"({1 - distance:.0%} relevant)" if distance else ""
                lines.append(f"{i}. {mem['content']} {relevance}")
                if mem.get("metadata"):
                    tags = mem["metadata"].get("tags", "")
                    if tags:
                        lines.append(f"   Tags: {tags}")

            return ToolResult.ok(
                "\n".join(lines),
                count=len(results),
            )
        except Exception as e:
            return ToolResult.fail(f"Memory search error: {e}")
