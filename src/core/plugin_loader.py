"""Plugin loader: discovers and loads external Python tools from ~/.kuro/plugins/.

Scans the plugins directory for .py files containing BaseTool subclasses,
loads them via importlib, and registers them into the ToolRegistry.

Plugin files are user-managed — placing a .py file in the plugins directory
is an explicit trust decision. Loaded tools still go through all security
layers (approval, sandbox, audit).
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
from types import ModuleType

from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class PluginLoader:
    """Loads external Python tools from the plugins directory."""

    def __init__(self, config: "PluginsConfig | None" = None) -> None:  # noqa: F821
        self._config = config
        self._loaded: dict[str, list[str]] = {}  # filename -> [tool_names]

    def load_plugins(self, registry: "ToolRegistry") -> int:  # noqa: F821
        """Scan plugins directory and register discovered tools.

        Returns the number of tools loaded.
        """
        if self._config is None:
            return 0

        plugins_dir = Path(self._config.plugins_dir).expanduser()
        if not plugins_dir.is_dir():
            return 0

        total = 0
        for py_file in sorted(plugins_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue

            module = self._load_module_from_file(py_file)
            if module is None:
                continue

            tools_found: list[str] = []
            for attr_name in dir(module):
                attr = getattr(module, attr_name, None)
                if (
                    attr is not None
                    and isinstance(attr, type)
                    and issubclass(attr, BaseTool)
                    and attr is not BaseTool
                    and hasattr(attr, "name")
                ):
                    try:
                        tool_instance = attr()
                        registry.register(tool_instance)
                        tools_found.append(tool_instance.name)
                        total += 1
                        logger.info(
                            "Loaded plugin tool '%s' from %s",
                            tool_instance.name,
                            py_file.name,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to instantiate tool %s from %s: %s",
                            attr_name,
                            py_file.name,
                            e,
                        )

            if tools_found:
                self._loaded[py_file.name] = tools_found

        return total

    def _load_module_from_file(self, filepath: Path) -> ModuleType | None:
        """Safely load a Python module from a file path."""
        module_name = f"kuro_plugin_{filepath.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception as e:
            logger.warning("Failed to load plugin %s: %s", filepath.name, e)
            return None

    def list_loaded(self) -> dict[str, list[str]]:
        """Return mapping of plugin filenames to their tool names."""
        return dict(self._loaded)

    @property
    def plugin_count(self) -> int:
        return len(self._loaded)

    @property
    def tool_count(self) -> int:
        return sum(len(tools) for tools in self._loaded.values())
