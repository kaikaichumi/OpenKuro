"""Action log: lightweight operation history recorder.

Zero AI token consumption - pure Python hook that records all tool calls
to JSONL files with automatic daily rotation.

Modes:
- tools_only (default): Record all tool calls (params + result summary)
- full: Tool calls + user/assistant conversation turns
- mutations_only: Only record operations with side effects (write, execute, send)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
import structlog

from src.config import ActionLogConfig, get_kuro_home

logger = structlog.get_logger()

# Tools that are considered mutations (have side effects)
MUTATION_TOOLS = frozenset({
    "file_write", "shell_execute", "clipboard_write",
    "calendar_write", "send_message", "memory_store",
})

# Patterns to redact from log entries
SENSITIVE_PATTERNS = [
    "api_key", "api-key", "apikey",
    "password", "passwd", "secret",
    "token", "credential", "auth",
]


def _redact_sensitive(data: Any) -> Any:
    """Recursively redact sensitive values from a dict/list."""
    if isinstance(data, dict):
        return {
            k: "***REDACTED***" if any(p in k.lower() for p in SENSITIVE_PATTERNS) else _redact_sensitive(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_redact_sensitive(item) for item in data]
    return data


class ActionLogger:
    """Records operations to JSONL files with zero LLM token cost."""

    def __init__(self, config: ActionLogConfig | None = None) -> None:
        self.config = config or ActionLogConfig()
        self._log_dir = get_kuro_home() / "action_logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._current_file: str | None = None

    def _get_log_path(self) -> Path:
        """Get the log file path for today (daily rotation)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._log_dir / f"actions-{today}.jsonl"

    def _should_log_tool(self, tool_name: str) -> bool:
        """Check if this tool should be logged based on current mode."""
        if self.config.mode == "tools_only":
            return True
        if self.config.mode == "mutations_only":
            return tool_name in MUTATION_TOOLS
        if self.config.mode == "full":
            return True
        return True

    async def log_tool_call(
        self,
        session_id: str,
        tool_name: str,
        params: dict[str, Any],
        result_output: str = "",
        status: str = "ok",
        duration_ms: int = 0,
        error: str | None = None,
    ) -> None:
        """Log a tool call to the JSONL file.

        This is called as a hook around tool execution - no LLM involved.
        """
        if not self._should_log_tool(tool_name):
            return

        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sid": session_id,
            "type": "tool_call",
            "tool": tool_name,
            "params": _redact_sensitive(params),
            "status": status,
            "duration_ms": duration_ms,
        }

        if error:
            entry["error"] = error[:500]  # Truncate long errors

        if self.config.include_full_result:
            entry["result"] = result_output[:10000]  # Cap at 10KB
        else:
            entry["result_size"] = len(result_output.encode("utf-8", errors="replace"))

        await self._write_entry(entry)

    async def log_conversation(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> None:
        """Log a conversation turn (only in 'full' mode)."""
        if self.config.mode != "full":
            return

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sid": session_id,
            "type": "message",
            "role": role,
            "content_size": len(content.encode("utf-8", errors="replace")),
            "content_preview": content[:200],  # First 200 chars only
        }

        await self._write_entry(entry)

    async def log_complexity(
        self,
        session_id: str,
        complexity_data: dict[str, Any],
    ) -> None:
        """Log a task complexity estimation result."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sid": session_id,
            "type": "complexity",
            "score": complexity_data.get("score"),
            "tier": complexity_data.get("tier"),
            "model": complexity_data.get("suggested_model"),
            "method": complexity_data.get("estimation_method"),
            "decompose": complexity_data.get("needs_decomposition", False),
        }
        await self._write_entry(entry)

    async def _write_entry(self, entry: dict[str, Any]) -> None:
        """Append a JSON entry to the current log file."""
        log_path = self._get_log_path()

        # Check file size for rotation
        if log_path.exists():
            size_mb = log_path.stat().st_size / (1024 * 1024)
            if size_mb >= self.config.max_file_size_mb:
                # Add sequence number for intra-day rotation
                base = log_path.stem
                for i in range(1, 100):
                    alt_path = log_path.parent / f"{base}-{i}.jsonl"
                    if not alt_path.exists():
                        log_path = alt_path
                        break

        try:
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            async with aiofiles.open(log_path, "a", encoding="utf-8") as f:
                await f.write(line + "\n")
        except Exception as e:
            logger.error("action_log_write_failed", error=str(e))

    async def cleanup_old_logs(self) -> int:
        """Remove log files older than retention_days. Returns count of removed files."""
        if self.config.retention_days <= 0:
            return 0

        removed = 0
        now = datetime.now(timezone.utc)

        for log_file in self._log_dir.glob("actions-*.jsonl"):
            try:
                # Parse date from filename
                date_str = log_file.stem.replace("actions-", "").split("-")
                if len(date_str) >= 3:
                    file_date = datetime(
                        int(date_str[0]), int(date_str[1]), int(date_str[2]),
                        tzinfo=timezone.utc,
                    )
                    age_days = (now - file_date).days
                    if age_days > self.config.retention_days:
                        log_file.unlink()
                        removed += 1
            except (ValueError, IndexError):
                continue

        if removed:
            logger.info("action_log_cleanup", removed=removed)

        return removed
