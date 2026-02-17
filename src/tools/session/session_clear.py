"""Session clear tool: reset conversation history to free up context.

Useful when a local LLM's context window is nearly full.
Can be triggered from any interface (CLI, Web, Discord, Telegram)
by asking the AI in natural language, e.g. "幫我清除對話".
"""

from __future__ import annotations

from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class SessionClearTool(BaseTool):
    """Clear the current conversation history to free context window space."""

    name = "session_clear"
    description = (
        "Clear the current conversation history. Use this when the context "
        "is getting too long for the current model, or when the user asks to "
        "start a fresh conversation. After clearing, only the system prompt "
        "remains. The user's current request will still be answered."
    )
    parameters = {
        "type": "object",
        "properties": {
            "keep_last": {
                "type": "integer",
                "description": (
                    "Number of recent message pairs to keep (0 = clear all). "
                    "Default: 0. Use a small number to retain recent context."
                ),
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        session = context.session
        if session is None:
            return ToolResult.fail("No session available to clear")

        keep_last = params.get("keep_last", 0)
        old_count = len(session.messages)

        if keep_last > 0:
            # Keep system messages + last N pairs of user/assistant messages
            system_msgs = [m for m in session.messages if m.role.value == "system"]
            non_system = [m for m in session.messages if m.role.value != "system"]
            # Each "pair" is roughly 2 messages (user + assistant)
            keep_msgs = non_system[-(keep_last * 2):]
            session.messages = system_msgs + keep_msgs
        else:
            # Clear all non-system messages
            session.messages = [
                m for m in session.messages if m.role.value == "system"
            ]

        cleared = old_count - len(session.messages)
        return ToolResult.ok(
            f"Conversation cleared. Removed {cleared} messages. "
            f"Remaining: {len(session.messages)} (system prompts only)."
        )
