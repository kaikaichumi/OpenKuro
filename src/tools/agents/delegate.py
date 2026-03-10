"""Delegate tools for sub-agent execution and discovery."""

from __future__ import annotations

import copy
import json
from typing import Any

from src.config import TaskComplexityConfig
from src.core.complexity import ComplexityEstimator
from src.core.types import Session
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

_DEFAULT_TIER_BOUNDARIES: dict[str, float] = {
    "trivial": 0.15,
    "simple": 0.35,
    "moderate": 0.60,
    "complex": 0.85,
}

_TIER_LEVELS: dict[str, int] = {
    "trivial": 0,
    "simple": 1,
    "moderate": 2,
    "complex": 3,
    "expert": 4,
}


def _normalize_model_selector(value: Any) -> str:
    model = str(value or "").strip()
    if not model:
        return ""
    if model.startswith("oauth:"):
        model = model[len("oauth:"):].strip()
    elif model.startswith("api:"):
        model = model[len("api:"):].strip()
    return model


def _normalize_tier(value: Any) -> str:
    tier = str(value or "moderate").strip().lower()
    return tier if tier in _TIER_LEVELS else "moderate"


def _normalize_boundaries(raw: Any) -> dict[str, float]:
    values = raw if isinstance(raw, dict) else {}
    normalized: dict[str, float] = {}
    prev = 0.0
    for key in ("trivial", "simple", "moderate", "complex"):
        default_val = _DEFAULT_TIER_BOUNDARIES[key]
        try:
            val = float(values.get(key, default_val))
        except Exception:
            val = default_val
        val = max(0.0, min(1.0, val))
        # Keep thresholds monotonic to avoid invalid ranges.
        val = max(prev, val)
        normalized[key] = val
        prev = val
    return normalized


def _score_to_tier(score: float, boundaries: dict[str, float]) -> str:
    if score < boundaries.get("trivial", _DEFAULT_TIER_BOUNDARIES["trivial"]):
        return "trivial"
    if score < boundaries.get("simple", _DEFAULT_TIER_BOUNDARIES["simple"]):
        return "simple"
    if score < boundaries.get("moderate", _DEFAULT_TIER_BOUNDARIES["moderate"]):
        return "moderate"
    if score < boundaries.get("complex", _DEFAULT_TIER_BOUNDARIES["complex"]):
        return "complex"
    return "expert"


def _pick_best_agent(
    definitions: list[Any],
    required_tier: str,
    *,
    enforce_min_tier: bool,
    preferred_model: str = "",
) -> Any | None:
    if not definitions:
        return None

    required_level = _TIER_LEVELS[_normalize_tier(required_tier)]
    ranked = sorted(
        definitions,
        key=lambda d: (
            _TIER_LEVELS[_normalize_tier(getattr(d, "complexity_tier", "moderate"))],
            str(getattr(d, "name", "")),
        ),
    )

    sufficient = [
        d for d in ranked
        if _TIER_LEVELS[_normalize_tier(getattr(d, "complexity_tier", "moderate"))] >= required_level
    ]
    if preferred_model:
        preferred_model_norm = _normalize_model_selector(preferred_model)
        preferred_sufficient = [
            d for d in sufficient
            if _normalize_model_selector(getattr(d, "model", "")) == preferred_model_norm
        ]
        if preferred_sufficient:
            return preferred_sufficient[0]

    if sufficient:
        # Choose the lowest capable tier first.
        return sufficient[0]
    if enforce_min_tier:
        return None
    # Fallback to the highest available tier.
    if preferred_model:
        preferred_model_norm = _normalize_model_selector(preferred_model)
        preferred_ranked = [
            d for d in ranked
            if _normalize_model_selector(getattr(d, "model", "")) == preferred_model_norm
        ]
        if preferred_ranked:
            return preferred_ranked[-1]
    return ranked[-1]


class DelegateToAgentTool(BaseTool):
    """Delegate a task to a named sub-agent."""

    name = "delegate_to_agent"
    description = (
        "Delegate a task to a sub-agent. You MUST use this tool to actually run "
        "a sub-agent. Do not pretend to delegate in plain text. Use list_agents "
        "to see available agents. If delegation complexity routing is enabled, "
        "set use_complexity=true to auto-select the best sub-agent tier."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": (
                    "Sub-agent name (for example: 'fast', 'coder'). Optional "
                    "when use_complexity=true and auto-select is enabled."
                ),
            },
            "task": {
                "type": "string",
                "description": "Task description to send to the sub-agent",
            },
            "use_complexity": {
                "type": "boolean",
                "description": (
                    "Use complexity scoring to route delegation by sub-agent tier. "
                    "Requires delegation_complexity.enabled=true."
                ),
            },
            "allow_auto_select": {
                "type": "boolean",
                "description": (
                    "Allow auto-selecting another sub-agent if agent_name is missing "
                    "or below required tier."
                ),
            },
        },
        "required": ["task"],
    }
    risk_level = RiskLevel.LOW

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Delegate a task to a sub-agent with optional complexity routing."""
        agent_name = str(params.get("agent_name", "") or "").strip()
        task = str(params.get("task", "") or "").strip()

        if not task:
            return ToolResult.fail("task is required")

        agent_manager = getattr(context, "agent_manager", None)
        if agent_manager is None:
            return ToolResult.fail("Agent system is not available")

        definitions = list(agent_manager.list_definitions() or [])
        if not definitions:
            return ToolResult.fail("No sub-agents are registered")

        cfg = getattr(context.config, "delegation_complexity", None)
        complexity_enabled = bool(getattr(cfg, "enabled", False))
        default_use_complexity = bool(
            getattr(cfg, "default_use_complexity", False)
        )
        allow_auto_select_default = bool(getattr(cfg, "allow_auto_select", True))
        enforce_min_tier = bool(getattr(cfg, "enforce_min_tier", True))
        tier_boundaries = _normalize_boundaries(
            getattr(cfg, "tier_boundaries", _DEFAULT_TIER_BOUNDARIES)
        )
        tier_models_raw = getattr(cfg, "tier_models", {}) or {}
        if not isinstance(tier_models_raw, dict):
            tier_models_raw = {}
        tier_models = {
            key: str(tier_models_raw.get(key, "") or "").strip()
            for key in ("trivial", "simple", "moderate", "complex")
        }

        use_complexity = (
            default_use_complexity
            if params.get("use_complexity") is None
            else bool(params.get("use_complexity"))
        )
        if use_complexity and not complexity_enabled:
            return ToolResult.fail(
                "delegation complexity routing is disabled in settings"
            )

        allow_auto_select = (
            allow_auto_select_default
            if params.get("allow_auto_select") is None
            else bool(params.get("allow_auto_select"))
        )

        selected_agent_name = agent_name
        selected_defn = (
            next((d for d in definitions if d.name == agent_name), None)
            if agent_name
            else None
        )

        score: float | None = None
        required_tier: str | None = None
        route_note = ""

        try:
            parent_session = getattr(context, "session", None)
            current_depth = 0
            if parent_session and hasattr(parent_session, "metadata"):
                current_depth = parent_session.metadata.get("depth", 0)

            if use_complexity:
                if selected_defn is None and selected_agent_name:
                    return ToolResult.fail(
                        f"Agent '{selected_agent_name}' not found. Use list_agents first."
                    )

                session_for_estimation = (
                    parent_session
                    if parent_session is not None
                    else Session(adapter="tool", user_id="delegate_to_agent")
                )

                estimator = None
                engine = getattr(agent_manager, "_engine", None)
                if engine is not None:
                    estimator = getattr(engine, "complexity_estimator", None)

                if estimator is None:
                    if context.model_router is None:
                        return ToolResult.fail(
                            "Model router unavailable for complexity estimation"
                        )
                    task_cfg = getattr(context.config, "task_complexity", None)
                    if task_cfg is None:
                        task_cfg = TaskComplexityConfig()
                    else:
                        task_cfg = copy.deepcopy(task_cfg)
                    estimator = ComplexityEstimator(
                        config=task_cfg,
                        model_router=context.model_router,
                    )

                complexity = await estimator.estimate(task, session_for_estimation)
                score = float(complexity.score)
                required_tier = _score_to_tier(score, tier_boundaries)
                preferred_model = tier_models.get(required_tier, "")
                if required_tier == "expert" and not preferred_model:
                    preferred_model = tier_models.get("complex", "")

                if selected_defn is not None:
                    selected_tier = _normalize_tier(
                        getattr(selected_defn, "complexity_tier", "moderate")
                    )
                    if (
                        enforce_min_tier
                        and _TIER_LEVELS[selected_tier] < _TIER_LEVELS[required_tier]
                    ):
                        if not allow_auto_select:
                            return ToolResult.fail(
                                f"Agent '{selected_defn.name}' tier '{selected_tier}' is below "
                                f"required tier '{required_tier}'."
                            )
                        picked = _pick_best_agent(
                            definitions,
                            required_tier,
                            enforce_min_tier=enforce_min_tier,
                            preferred_model=preferred_model,
                        )
                        if picked is None:
                            return ToolResult.fail(
                                f"No sub-agent satisfies required tier '{required_tier}'."
                            )
                        selected_defn = picked
                        selected_agent_name = picked.name
                        route_note = f"auto_fallback_from={agent_name}"
                else:
                    if not allow_auto_select:
                        return ToolResult.fail(
                            "agent_name is required when auto-select is disabled"
                        )
                    picked = _pick_best_agent(
                        definitions,
                        required_tier,
                        enforce_min_tier=enforce_min_tier,
                        preferred_model=preferred_model,
                    )
                    if picked is None:
                        return ToolResult.fail(
                            f"No sub-agent satisfies required tier '{required_tier}'."
                        )
                    selected_defn = picked
                    selected_agent_name = picked.name
                    route_note = "auto_selected"

            if not selected_agent_name:
                return ToolResult.fail("agent_name is required")
            if selected_defn is None:
                selected_defn = next(
                    (d for d in definitions if d.name == selected_agent_name),
                    None,
                )
                if selected_defn is None:
                    return ToolResult.fail(
                        f"Agent '{selected_agent_name}' not found. Use list_agents first."
                    )

            result = await agent_manager.delegate(
                selected_agent_name,
                task,
                parent_session=parent_session,
                depth=current_depth + 1,
            )

            route_prefix = ""
            if use_complexity and score is not None and required_tier is not None:
                selected_tier = _normalize_tier(
                    getattr(selected_defn, "complexity_tier", "moderate")
                )
                route_prefix = (
                    "[Delegation route] "
                    f"score={score:.3f} required_tier={required_tier} "
                    f"selected={selected_agent_name} selected_tier={selected_tier}"
                )
                if route_note:
                    route_prefix += f" {route_note}"
                route_prefix += "\n"

            if isinstance(result, dict):
                formatted = json.dumps(result, ensure_ascii=False, indent=2)
                return ToolResult.ok(
                    f"{route_prefix}[Agent '{selected_agent_name}' structured result]\n{formatted}"
                )
            return ToolResult.ok(
                f"{route_prefix}[Agent '{selected_agent_name}' result]\n{result}"
            )
        except Exception as e:
            failed_agent = selected_agent_name or agent_name or "unknown"
            return ToolResult.fail(f"Agent '{failed_agent}' failed: {e}")


class ListAgentsTool(BaseTool):
    """List available sub-agents."""

    name = "list_agents"
    description = (
        "List all registered sub-agents with their names, models, and "
        "capabilities. Use this to discover which agents are available "
        "for delegation."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(
        self, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """List all registered agents."""
        agent_manager = getattr(context, "agent_manager", None)
        if agent_manager is None:
            return ToolResult.fail("Agent system is not available")

        definitions = agent_manager.list_definitions()
        if not definitions:
            return ToolResult.ok(
                "No agents registered. Use /agent create to create one."
            )

        lines = ["Available agents:"]
        for defn in definitions:
            tools_info = ""
            if defn.allowed_tools:
                tools_info = f", tools: {', '.join(defn.allowed_tools)}"
            elif defn.denied_tools:
                tools_info = f", denied: {', '.join(defn.denied_tools)}"

            extras = []
            if defn.inherit_context:
                extras.append("inherits_context")
            if defn.output_schema:
                extras.append("structured_output")
            if defn.max_depth > 0:
                extras.append(f"max_depth={defn.max_depth}")
            extras_str = f", [{', '.join(extras)}]" if extras else ""

            lines.append(
                f"- {defn.name}: model={defn.model}, "
                f"tier={_normalize_tier(getattr(defn, 'complexity_tier', 'moderate'))}, "
                f"rounds={defn.max_tool_rounds}{tools_info}{extras_str}"
            )

        return ToolResult.ok("\n".join(lines))
