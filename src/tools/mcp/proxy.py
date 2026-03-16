"""Dynamic proxy tool for MCP-exposed remote tools."""

from __future__ import annotations

from typing import Any

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class MCPProxyTool(BaseTool):
    """Wrap one MCP remote tool as a local Kuro tool."""

    _auto_discover = False

    def __init__(
        self,
        *,
        local_name: str,
        description: str,
        parameters: dict[str, Any],
        risk_level: RiskLevel,
        bridge_manager: Any,
    ) -> None:
        self.name = local_name
        self.description = description
        self.parameters = parameters
        self.risk_level = risk_level
        self._bridge = bridge_manager

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        return await self._bridge.execute_tool(self.name, params, context)

