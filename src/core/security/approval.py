"""Action approval system: risk-based human-in-the-loop control.

Determines whether a tool call needs human approval based on:
1. Tool's risk level vs. auto-approve threshold
2. Per-tool override rules
3. Session trust escalation (user can grant higher trust mid-session)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.config import SecurityConfig
from src.tools.base import RiskLevel

logger = structlog.get_logger()

# Map string risk levels to enum
RISK_MAP: dict[str, RiskLevel] = {
    "low": RiskLevel.LOW,
    "medium": RiskLevel.MEDIUM,
    "high": RiskLevel.HIGH,
    "critical": RiskLevel.CRITICAL,
}


@dataclass
class ApprovalDecision:
    """Result of an approval check."""

    approved: bool
    reason: str
    method: str  # "auto", "session_trust", "user_approved", "user_denied", "policy_denied"


@dataclass
class SessionTrust:
    """Tracks trust level escalation within a session."""

    level: RiskLevel = RiskLevel.LOW
    granted_at: float = 0.0
    timeout_seconds: int = 1800  # 30 minutes default

    @property
    def is_expired(self) -> bool:
        if self.granted_at == 0.0:
            return True
        return (time.monotonic() - self.granted_at) > self.timeout_seconds

    def elevate(self, level: RiskLevel, timeout_seconds: int | None = None) -> None:
        """Elevate session trust to a higher level."""
        self.level = level
        self.granted_at = time.monotonic()
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds

    def current_level(self) -> RiskLevel:
        """Get current effective trust level (accounting for timeout)."""
        if self.is_expired:
            self.level = RiskLevel.LOW
            self.granted_at = 0.0
        return self.level


class ApprovalPolicy:
    """Determines whether a tool call requires human approval."""

    def __init__(self, config: SecurityConfig) -> None:
        self.config = config
        self._session_trusts: dict[str, SessionTrust] = {}

    def get_session_trust(self, session_id: str) -> SessionTrust:
        """Get or create trust state for a session."""
        if session_id not in self._session_trusts:
            timeout = self.config.trust_timeout_minutes * 60
            self._session_trusts[session_id] = SessionTrust(timeout_seconds=timeout)
        return self._session_trusts[session_id]

    def elevate_session_trust(
        self,
        session_id: str,
        level: RiskLevel,
    ) -> None:
        """Elevate trust for a session (called when user approves with 'trust')."""
        trust = self.get_session_trust(session_id)
        trust.elevate(level, self.config.trust_timeout_minutes * 60)
        logger.info(
            "trust_elevated",
            session=session_id[:8],
            level=level.value,
            timeout_min=self.config.trust_timeout_minutes,
        )

    def check(
        self,
        tool_name: str,
        risk_level: RiskLevel,
        session_id: str,
    ) -> ApprovalDecision:
        """Check if a tool call should be auto-approved, needs user input, or is denied.

        Returns an ApprovalDecision indicating the outcome.
        """
        # 0. Force approval for specific tools (overrides auto-approve and session trust)
        if tool_name in self.config.require_approval_for:
            return ApprovalDecision(
                approved=False,
                reason=f"Tool '{tool_name}' requires explicit approval",
                method="pending",
            )

        # 1. Check if risk level is in auto-approve list
        auto_levels = {
            RISK_MAP[l] for l in self.config.auto_approve_levels if l in RISK_MAP
        }
        if risk_level in auto_levels:
            return ApprovalDecision(
                approved=True,
                reason=f"Auto-approved: {risk_level.value} is in auto-approve list",
                method="auto",
            )

        # 2. Check session trust escalation
        if self.config.session_trust_enabled:
            trust = self.get_session_trust(session_id)
            current = trust.current_level()
            if risk_level <= current:
                return ApprovalDecision(
                    approved=True,
                    reason=f"Session trust: {current.value} covers {risk_level.value}",
                    method="session_trust",
                )

        # 3. Requires user approval
        return ApprovalDecision(
            approved=False,
            reason=f"Requires approval: {tool_name} ({risk_level.value})",
            method="pending",
        )

    def cleanup_expired(self) -> int:
        """Remove expired session trusts. Returns count removed."""
        expired = [
            sid for sid, trust in self._session_trusts.items()
            if trust.is_expired
        ]
        for sid in expired:
            del self._session_trusts[sid]
        return len(expired)
