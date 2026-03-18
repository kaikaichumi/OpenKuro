"""Tool-level policy core: adapter/model/rule checks plus taint labels."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

from src.config import ToolPolicyCoreConfig, ToolPolicyRuleConfig
from src.core.security.egress import EgressBroker

_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


@dataclass
class ToolPolicyDecision:
    """Decision result for a tool call under policy-core checks."""

    allowed: bool
    reason: str = "allowed"
    matched_rule: str = "default"
    isolation_tier: str = "standard"
    require_explicit_approval: bool = False
    taint_on_success: list[str] = field(default_factory=list)


class ToolPolicyCore:
    """Evaluate per-tool policy rules before tool execution."""

    def __init__(
        self,
        config: ToolPolicyCoreConfig | None = None,
        egress_broker: EgressBroker | None = None,
    ) -> None:
        self.config = config
        self.egress = egress_broker
        # session_id -> tool_name -> count
        self._session_counts: dict[str, dict[str, int]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config and getattr(self.config, "enabled", False))

    def _resolve_rule(self, tool_name: str) -> tuple[str, ToolPolicyRuleConfig | None]:
        if self.config is None:
            return "default", None
        rules = getattr(self.config, "tool_rules", {}) or {}
        if tool_name in rules:
            return tool_name, rules[tool_name]
        for pattern, rule in sorted(rules.items(), key=lambda x: len(str(x[0])), reverse=True):
            try:
                if fnmatch.fnmatch(tool_name, str(pattern)):
                    return str(pattern), rule
            except Exception:
                continue
        return "default", getattr(self.config, "default_rule", None)

    @staticmethod
    def _extract_urls(value: Any) -> list[str]:
        out: list[str] = []
        if isinstance(value, str):
            for match in _URL_RE.findall(value):
                out.append(match.strip().rstrip(".,);]>"))
            return out
        if isinstance(value, dict):
            for v in value.values():
                out.extend(ToolPolicyCore._extract_urls(v))
            return out
        if isinstance(value, list):
            for item in value:
                out.extend(ToolPolicyCore._extract_urls(item))
            return out
        return out

    @staticmethod
    def _labels_from_session(session_labels: set[str] | None, session: Any) -> set[str]:
        if session_labels is not None:
            return {str(v).strip().lower() for v in session_labels if str(v).strip()}
        metadata = getattr(session, "metadata", {}) if session is not None else {}
        raw = metadata.get("_data_labels", []) if isinstance(metadata, dict) else []
        if not isinstance(raw, list):
            return set()
        return {str(v).strip().lower() for v in raw if str(v).strip()}

    @staticmethod
    def _matches_any_pattern(value: str, patterns: list[str]) -> bool:
        raw = str(value or "")
        for pattern in patterns:
            try:
                if fnmatch.fnmatch(raw, pattern):
                    return True
            except Exception:
                continue
        return False

    def evaluate(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        session: Any,
        active_model: str,
        guard_state: dict[str, Any] | None = None,
        session_labels: set[str] | None = None,
    ) -> ToolPolicyDecision:
        """Evaluate a tool call against tool-policy rules."""
        if not self.enabled:
            return ToolPolicyDecision(allowed=True, reason="tool policy disabled")

        matched_rule, rule = self._resolve_rule(tool_name)
        if rule is None:
            return ToolPolicyDecision(allowed=True, reason="no matching tool policy")

        if not bool(getattr(rule, "enabled", True)):
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Tool '{tool_name}' is disabled by tool policy",
                matched_rule=matched_rule,
            )

        adapter = str(getattr(session, "adapter", "") or "").strip().lower()
        allowed_adapters = [str(v).strip().lower() for v in (getattr(rule, "allowed_adapters", []) or [])]
        denied_adapters = [str(v).strip().lower() for v in (getattr(rule, "denied_adapters", []) or [])]
        if denied_adapters and adapter in denied_adapters:
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Adapter '{adapter}' is denied by tool policy",
                matched_rule=matched_rule,
            )
        if allowed_adapters and adapter not in allowed_adapters:
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Adapter '{adapter}' is outside tool allowlist",
                matched_rule=matched_rule,
            )

        model_name = str(active_model or "").strip()
        allowed_models = [str(v).strip() for v in (getattr(rule, "allowed_models", []) or [])]
        denied_models = [str(v).strip() for v in (getattr(rule, "denied_models", []) or [])]
        if denied_models and self._matches_any_pattern(model_name, denied_models):
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Model '{model_name}' is denied by tool policy",
                matched_rule=matched_rule,
            )
        if allowed_models and not self._matches_any_pattern(model_name, allowed_models):
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Model '{model_name}' is outside tool allowlist",
                matched_rule=matched_rule,
            )

        labels = self._labels_from_session(session_labels, session)
        required_labels = [str(v).strip().lower() for v in (getattr(rule, "required_labels", []) or [])]
        blocked_labels = [str(v).strip().lower() for v in (getattr(rule, "blocked_labels", []) or [])]
        if required_labels and not any(label in labels for label in required_labels):
            return ToolPolicyDecision(
                allowed=False,
                reason=(
                    "Tool policy requires one of labels: "
                    + ", ".join(sorted(set(required_labels)))
                ),
                matched_rule=matched_rule,
            )
        if blocked_labels and any(label in labels for label in blocked_labels):
            return ToolPolicyDecision(
                allowed=False,
                reason=(
                    "Tool policy blocked due to session labels: "
                    + ", ".join(sorted(set(labels.intersection(blocked_labels))))
                ),
                matched_rule=matched_rule,
            )

        session_id = str(getattr(session, "id", "") or "default")
        session_counts = self._session_counts.setdefault(session_id, {})
        next_session_count = int(session_counts.get(tool_name, 0) or 0) + 1
        max_calls_per_session = max(0, int(getattr(rule, "max_calls_per_session", 0) or 0))
        if max_calls_per_session and next_session_count > max_calls_per_session:
            return ToolPolicyDecision(
                allowed=False,
                reason=(
                    f"Tool policy session budget exceeded for '{tool_name}' "
                    f"({next_session_count}/{max_calls_per_session})"
                ),
                matched_rule=matched_rule,
            )

        next_task_count = 1
        max_calls_per_task = max(0, int(getattr(rule, "max_calls_per_task", 0) or 0))
        if guard_state is not None:
            task_counts = guard_state.setdefault("policy_tool_counts", {})
            next_task_count = int(task_counts.get(tool_name, 0) or 0) + 1
            if max_calls_per_task and next_task_count > max_calls_per_task:
                return ToolPolicyDecision(
                    allowed=False,
                    reason=(
                        f"Tool policy task budget exceeded for '{tool_name}' "
                        f"({next_task_count}/{max_calls_per_task})"
                    ),
                    matched_rule=matched_rule,
                )

        if self.egress is not None:
            urls = self._extract_urls(arguments)
            if urls:
                allowed_domains = [str(v) for v in (getattr(rule, "allowed_domains", []) or [])]
                blocked_domains = [str(v) for v in (getattr(rule, "blocked_domains", []) or [])]
                allow_private_network = getattr(rule, "allow_private_network", None)
                for url in urls:
                    decision = self.egress.evaluate_url(
                        url,
                        tool_name=tool_name,
                        allowed_domains=allowed_domains,
                        blocked_domains=blocked_domains,
                        allow_private_network=allow_private_network,
                    )
                    if not decision.allowed:
                        return ToolPolicyDecision(
                            allowed=False,
                            reason=f"Tool policy blocked URL '{url}': {decision.reason}",
                            matched_rule=matched_rule,
                        )

        # Commit counters only when all checks pass.
        session_counts[tool_name] = next_session_count
        if guard_state is not None:
            task_counts = guard_state.setdefault("policy_tool_counts", {})
            task_counts[tool_name] = next_task_count

        return ToolPolicyDecision(
            allowed=True,
            reason="allowed",
            matched_rule=matched_rule,
            isolation_tier=str(getattr(rule, "isolation_tier", "standard") or "standard"),
            require_explicit_approval=bool(getattr(rule, "require_explicit_approval", False)),
            taint_on_success=[
                str(v).strip().lower()
                for v in (getattr(rule, "taint_on_success", []) or [])
                if str(v).strip()
            ],
        )

