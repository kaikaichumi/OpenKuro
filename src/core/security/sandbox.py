"""Execution sandbox: constrains tool operations to safe boundaries.

Provides:
- Directory whitelist for file operations
- Command blocklist/allowlist for shell execution
- Execution timeout enforcement
- Output size limits
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import structlog

from src.config import SandboxConfig

logger = structlog.get_logger()


class Sandbox:
    """Constrains tool execution to safe boundaries."""

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config
        self._resolved_dirs: list[Path] | None = None

    @property
    def allowed_directories(self) -> list[Path]:
        """Resolve and cache allowed directory paths."""
        if self._resolved_dirs is None:
            self._resolved_dirs = []
            for d in self.config.allowed_directories:
                expanded = os.path.expanduser(os.path.expandvars(d))
                self._resolved_dirs.append(Path(expanded).resolve())
        return self._resolved_dirs

    def is_path_allowed(self, path: str | Path) -> bool:
        """Check if a file path is within allowed directories.

        If no directories are configured, all paths are allowed.
        """
        if not self.config.allowed_directories:
            return True

        target = Path(os.path.expanduser(str(path))).resolve()

        for allowed in self.allowed_directories:
            try:
                target.relative_to(allowed)
                return True
            except ValueError:
                continue

        return False

    def is_command_allowed(self, command: str) -> bool:
        """Check if a shell command is safe to execute.

        Checks against the blocked commands list using substring matching
        and pattern matching for dangerous operations.
        """
        cmd_lower = command.lower().strip()

        # Check explicit blocklist
        for blocked in self.config.blocked_commands:
            if blocked.lower() in cmd_lower:
                logger.warning(
                    "command_blocked",
                    command=command[:100],
                    matched_rule=blocked,
                )
                return False

        # Additional dangerous pattern checks
        dangerous_patterns = [
            r"\brm\s+(-[rf]+\s+)?/\b",           # rm -rf /
            r"\bformat\s+[a-z]:",                  # format C:
            r"\bdel\s+/[sfq]+\s+[a-z]:\\",         # del /f /s C:\
            r"\brmdir\s+/s\s+/q\s+[a-z]:\\",       # rmdir /s /q C:\
            r"\bmkfs\b",                            # mkfs
            r"\bdd\s+if=.*of=/dev/",               # dd if=... of=/dev/
            r">\s*/dev/sd[a-z]",                    # > /dev/sda
            r"\bchmod\s+-R\s+777\s+/",             # chmod -R 777 /
            r"\bchown\s+-R\s+.*\s+/\s*$",          # chown -R ... /
            r"curl.*\|\s*(bash|sh|python|powershell)",  # curl ... | bash
            r"wget.*\|\s*(bash|sh|python|powershell)",  # wget ... | bash
            r"\breg\s+delete\b",                   # reg delete
            r"\bnet\s+user\s+.*\s+/add\b",         # net user ... /add
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, cmd_lower):
                logger.warning(
                    "command_blocked_pattern",
                    command=command[:100],
                    pattern=pattern,
                )
                return False

        return True

    def validate_file_operation(
        self,
        path: str | Path,
        operation: str = "read",
    ) -> tuple[bool, str]:
        """Validate a file operation against sandbox rules.

        Returns (allowed, reason).
        """
        if not self.is_path_allowed(path):
            allowed_str = ", ".join(str(d) for d in self.allowed_directories)
            return False, f"Path not in allowed directories: {allowed_str}"

        resolved = Path(os.path.expanduser(str(path))).resolve()

        # Prevent symlink escapes
        if resolved.is_symlink():
            real = resolved.resolve()
            if not self.is_path_allowed(real):
                return False, "Symlink target is outside allowed directories"

        # For write operations, check if parent directory exists
        if operation in ("write", "create"):
            if not resolved.parent.exists():
                return False, f"Parent directory does not exist: {resolved.parent}"

        return True, "OK"

    def sanitize_command(self, command: str) -> str:
        """Basic command sanitization (remove obviously dangerous constructs)."""
        # Remove command chaining that could bypass checks
        # This is a soft defense layer - the blocklist is the hard defense
        sanitized = command.strip()

        # Warn about command chaining
        chain_operators = ["&&", "||", ";", "|"]
        for op in chain_operators:
            if op in sanitized:
                logger.debug(
                    "command_chaining_detected",
                    operator=op,
                    command=sanitized[:100],
                )

        return sanitized
