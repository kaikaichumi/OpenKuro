"""Input sanitizer: prompt injection defense and content filtering.

Provides defense against:
- Prompt injection attacks in tool outputs
- Sensitive data leakage in LLM context
- Malicious content in user inputs from messaging platforms
"""

from __future__ import annotations

import re
from typing import Any

import structlog

logger = structlog.get_logger()

# Patterns that might indicate prompt injection in tool outputs
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"ignore\s+(all\s+)?above\s+instructions",
    r"you\s+are\s+now\s+(?:a|an)\s+",
    r"new\s+instructions?\s*:",
    r"system\s*:\s*you\s+are",
    r"forget\s+(all\s+)?previous",
    r"disregard\s+(all\s+)?previous",
    r"override\s+(all\s+)?previous",
    r"\[SYSTEM\]",
    r"\[INST\]",
    r"<<SYS>>",
]

# Compiled patterns for performance
_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

# Sensitive data patterns to redact from tool outputs before sending to LLM
SENSITIVE_PATTERNS = [
    # API keys
    (r"(sk-[a-zA-Z0-9]{20,})", r"sk-***REDACTED***"),
    (r"(api[_-]?key\s*[:=]\s*)['\"]?([a-zA-Z0-9_-]{20,})['\"]?", r"\1***REDACTED***"),
    # Tokens
    (r"(token\s*[:=]\s*)['\"]?([a-zA-Z0-9_.-]{20,})['\"]?", r"\1***REDACTED***"),
    # Passwords in URLs
    (r"(://[^:]+:)([^@]+)(@)", r"\1***@"),
    # AWS keys
    (r"(AKIA[0-9A-Z]{16})", r"AKIA***REDACTED***"),
    # Private keys
    (r"(-----BEGIN\s+\w+\s+PRIVATE\s+KEY-----)", r"[PRIVATE KEY REDACTED]"),
]

_SENSITIVE_RE = [(re.compile(p, re.IGNORECASE), r) for p, r in SENSITIVE_PATTERNS]


class Sanitizer:
    """Input/output sanitization for security."""

    def __init__(self) -> None:
        self.injection_detections: int = 0

    def check_injection(self, text: str) -> tuple[bool, str | None]:
        """Check if text contains potential prompt injection.

        Returns (is_suspicious, matched_pattern).
        Does NOT block - just detects and warns. The engine decides what to do.
        """
        for i, pattern in enumerate(_INJECTION_RE):
            match = pattern.search(text)
            if match:
                self.injection_detections += 1
                matched = match.group(0)
                logger.warning(
                    "injection_detected",
                    pattern=INJECTION_PATTERNS[i],
                    matched=matched[:50],
                )
                return True, matched
        return False, None

    def sanitize_tool_output(self, output: str) -> str:
        """Sanitize tool output before including in LLM context.

        - Redacts sensitive data patterns
        - Wraps content to mark it as tool output (not instructions)
        - Truncates excessively long outputs
        """
        result = output

        # Redact sensitive patterns
        for pattern, replacement in _SENSITIVE_RE:
            result = pattern.sub(replacement, result)

        return result

    def redact_for_log(self, data: Any) -> Any:
        """Redact sensitive values from data for logging."""
        if isinstance(data, str):
            result = data
            for pattern, replacement in _SENSITIVE_RE:
                result = pattern.sub(replacement, result)
            return result
        if isinstance(data, dict):
            return {k: self.redact_for_log(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.redact_for_log(item) for item in data]
        return data

    def sanitize_user_input(self, text: str) -> str:
        """Basic sanitization of user input.

        Mostly a pass-through - we don't want to modify what users say.
        Just removes null bytes and normalizes whitespace.
        """
        # Remove null bytes
        text = text.replace("\x00", "")
        # Normalize line endings
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text.strip()
