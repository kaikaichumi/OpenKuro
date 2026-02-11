"""Tool system: registry, discovery, and execution pipeline.

Tools are auto-discovered from the src/tools/ directory.
Each tool module should export a class that inherits from BaseTool.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

import structlog

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

logger = structlog.get_logger()


class ToolRegistry:
    """Central registry for all available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool instance."""
        if tool.name in self._tools:
            logger.warning("tool_duplicate", name=tool.name)
        self._tools[tool.name] = tool
        logger.info("tool_registered", name=tool.name, risk=tool.risk_level.value)

    def get(self, name: str) -> BaseTool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_all(self) -> list[BaseTool]:
        """Get all registered tools."""
        return list(self._tools.values())

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """Get all tools in OpenAI-compatible format for LLM function calling."""
        return [t.to_openai_tool() for t in self._tools.values()]

    def get_names(self) -> list[str]:
        """Get all registered tool names."""
        return list(self._tools.keys())


class ToolSystem:
    """Manages tool discovery, registration, and execution."""

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()

    def discover_tools(self, package_name: str = "src.tools") -> None:
        """Auto-discover and register tools from the tools package.

        Looks for classes that inherit from BaseTool in each submodule.
        """
        try:
            package = importlib.import_module(package_name)
        except ImportError as e:
            logger.error("tool_discovery_failed", package=package_name, error=str(e))
            return

        if not hasattr(package, "__path__"):
            return

        for importer, modname, ispkg in pkgutil.walk_packages(
            package.__path__, prefix=f"{package_name}."
        ):
            try:
                module = importlib.import_module(modname)
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseTool)
                        and attr is not BaseTool
                        and hasattr(attr, "name")
                    ):
                        try:
                            tool_instance = attr()
                            self.registry.register(tool_instance)
                        except Exception as e:
                            logger.warning(
                                "tool_init_failed",
                                tool=attr_name,
                                error=str(e),
                            )
            except Exception as e:
                logger.debug("tool_module_skip", module=modname, error=str(e))

    async def execute(
        self,
        tool_name: str,
        params: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute a tool by name with given parameters.

        Returns ToolResult.denied() if the tool is not found.
        """
        tool = self.registry.get(tool_name)
        if tool is None:
            return ToolResult.fail(f"Unknown tool: {tool_name}")

        try:
            logger.info(
                "tool_execute",
                tool=tool_name,
                risk=tool.risk_level.value,
            )
            result = await tool.execute(params, context)
            return result
        except Exception as e:
            logger.error("tool_error", tool=tool_name, error=str(e))
            return ToolResult.fail(f"Tool execution error: {e}")
