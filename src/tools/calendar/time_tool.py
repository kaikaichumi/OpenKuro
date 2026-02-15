"""Time tool: get the current date and time."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class GetTimeTool(BaseTool):
    """Get the current date and time."""

    name = "get_time"
    description = (
        "Get the current date, time, day of week, and timezone. "
        "Use this when the user asks what time or date it is."
    )
    parameters = {
        "type": "object",
        "properties": {
            "timezone_offset": {
                "type": "number",
                "description": (
                    "UTC offset in hours (e.g., 8 for UTC+8, -5 for UTC-5). "
                    "Defaults to local system time if omitted."
                ),
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Return the current date and time."""
        offset = params.get("timezone_offset")

        if offset is not None:
            try:
                from datetime import timedelta

                tz = timezone(timedelta(hours=float(offset)))
                now = datetime.now(tz)
                tz_label = f"UTC{offset:+g}"
            except (ValueError, OverflowError):
                return ToolResult.fail(f"Invalid timezone offset: {offset}")
        else:
            now = datetime.now().astimezone()
            tz_label = now.strftime("%Z") or "local"

        return ToolResult.ok(
            f"Date: {now.strftime('%Y-%m-%d')}\n"
            f"Time: {now.strftime('%H:%M:%S')}\n"
            f"Day: {now.strftime('%A')}\n"
            f"Timezone: {tz_label}"
        )
