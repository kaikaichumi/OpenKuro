"""Helpers for guarding dependency installation flows."""

from __future__ import annotations

import re
from typing import Any

_INSTALL_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(^|[\s;&|])(?:sudo\s+)?(?:apt(?:-get)?|yum|dnf|zypper|brew|choco|winget)\s+install(?:\s|$)",
        re.IGNORECASE,
    ),
    re.compile(r"(^|[\s;&|])(?:sudo\s+)?pacman\s+-S(?:\s|$)", re.IGNORECASE),
    re.compile(r"(^|[\s;&|])(?:pip(?:3)?|uv\s+pip)\s+install(?:\s|$)", re.IGNORECASE),
    re.compile(
        r"(^|[\s;&|])python(?:3(?:\.\d+)?)?\s+-m\s+pip\s+install(?:\s|$)",
        re.IGNORECASE,
    ),
    re.compile(r"(^|[\s;&|])poetry\s+(?:add|install)(?:\s|$)", re.IGNORECASE),
    re.compile(r"(^|[\s;&|])npm\s+install(?:\s|$)", re.IGNORECASE),
    re.compile(r"(^|[\s;&|])pnpm\s+add(?:\s|$)", re.IGNORECASE),
    re.compile(r"(^|[\s;&|])yarn\s+add(?:\s|$)", re.IGNORECASE),
    re.compile(r"(^|[\s;&|])playwright\s+install(?:\s|$)", re.IGNORECASE),
)

_DEPENDENCY_HINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"install with:", re.IGNORECASE),
    re.compile(r"\bdependency\b.*\bmissing\b", re.IGNORECASE),
    re.compile(r"\bnot installed\b", re.IGNORECASE),
    re.compile(r"\bmissing dependency\b", re.IGNORECASE),
    re.compile(r"\bno module named\b", re.IGNORECASE),
    re.compile(r"\byou must install\b", re.IGNORECASE),
)


def is_install_command(command: str | None) -> bool:
    """Return True when command text appears to install dependencies/packages."""
    raw = str(command or "").strip()
    if not raw:
        return False
    return any(p.search(raw) for p in _INSTALL_COMMAND_PATTERNS)


def is_fix_mode_session(session: Any | None) -> bool:
    """Return True when current session is marked as running in !fix mode."""
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("_kuro_fix_mode", False))


def is_dependency_error_text(text: str | None) -> bool:
    """Return True when text indicates missing installable dependencies."""
    raw = str(text or "").strip()
    if not raw:
        return False
    return any(p.search(raw) for p in _DEPENDENCY_HINT_PATTERNS)
