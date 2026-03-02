"""SharedWorkspace: team-wide key-value state for inter-agent data sharing.

All team members can read and write to this workspace. Changes are
tracked with history for debugging and audit purposes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger()


class SharedWorkspace:
    """Thread-safe shared key-value store for agent team collaboration.

    Supports:
    - Atomic read/write with asyncio.Lock
    - Full history tracking (who wrote what, when)
    - Summary generation for LLM context injection
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._history: list[dict[str, Any]] = []

    async def write(self, key: str, value: Any, writer_role: str) -> None:
        """Write a value to the workspace.

        Args:
            key: The data key.
            value: The value to store (any serializable type).
            writer_role: Name of the role writing the data.
        """
        async with self._lock:
            self._data[key] = value
            self._history.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "role": writer_role,
                "key": key,
                "action": "write",
                "value_preview": str(value)[:200],
            })
            logger.debug(
                "workspace_write",
                role=writer_role,
                key=key,
                value_len=len(str(value)),
            )

    async def read(self, key: str) -> Any:
        """Read a value from the workspace.

        Returns None if key doesn't exist.
        """
        return self._data.get(key)

    async def read_all(self) -> dict[str, Any]:
        """Read all workspace data."""
        return dict(self._data)

    async def delete(self, key: str, writer_role: str) -> bool:
        """Delete a key from the workspace. Returns True if key existed."""
        async with self._lock:
            if key in self._data:
                del self._data[key]
                self._history.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "role": writer_role,
                    "key": key,
                    "action": "delete",
                })
                return True
            return False

    async def keys(self) -> list[str]:
        """List all keys in the workspace."""
        return list(self._data.keys())

    async def get_summary(self, max_chars: int = 3000) -> str:
        """Generate a human-readable summary of workspace contents.

        Used for injecting workspace state into agent context prompts.

        Args:
            max_chars: Maximum total characters for the summary.
        """
        if not self._data:
            return "[Shared Workspace: empty]"

        lines = ["[Shared Workspace]"]
        total_chars = len(lines[0])

        for key, value in self._data.items():
            value_str = str(value)
            if len(value_str) > 500:
                value_str = value_str[:500] + "..."
            line = f"  {key}: {value_str}"
            if total_chars + len(line) > max_chars:
                lines.append(f"  ... ({len(self._data) - len(lines) + 1} more keys)")
                break
            lines.append(line)
            total_chars += len(line)

        return "\n".join(lines)

    @property
    def history(self) -> list[dict[str, Any]]:
        """Get the full history of workspace operations."""
        return list(self._history)

    @property
    def size(self) -> int:
        """Number of keys in the workspace."""
        return len(self._data)
