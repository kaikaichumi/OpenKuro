"""Task complexity estimation and adaptive model routing.

Estimates the complexity of user messages to:
1. Route simple tasks to cheap/fast models, complex tasks to frontier models
2. Decompose overly complex tasks into sub-tasks for multi-agent execution

Two-phase estimation:
- Phase 1: Zero-cost heuristic scoring based on text features (8 dimensions)
- Phase 2: Optional LLM refinement for ambiguous scores (cheap model)
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import structlog

from src.config import TaskComplexityConfig

if TYPE_CHECKING:
    from src.core.complexity_ml import MLComplexityClassifier
    from src.core.model_router import ModelRouter
    from src.core.types import Session

logger = structlog.get_logger()


# ── Data Classes ─────────────────────────────────────────────────────────


@dataclass
class ComplexityDimension:
    """Individual dimension of task complexity."""

    name: str       # e.g., "reasoning_markers", "domain_count"
    score: float    # 0.0 - 1.0
    weight: float   # contribution weight
    detail: str = ""  # human-readable detail (e.g., "markers: analyze, compare")


@dataclass
class ComplexityResult:
    """Result of complexity estimation."""

    score: float                                    # 0.0 - 1.0 composite score
    tier: str                                       # "trivial"|"simple"|"moderate"|"complex"|"expert"
    dimensions: list[ComplexityDimension]
    suggested_model: str | None = None              # resolved model name
    needs_decomposition: bool = False               # True if score > decomposition threshold
    decomposition_hint: str | None = None           # LLM-generated hint
    estimation_method: str = "heuristic"            # "heuristic" | "llm" | "hybrid" | "ml" | "hybrid_ml" | "ml_refine"
    estimation_ms: float = 0.0                      # time spent estimating
    ml_intent: str | None = None                    # ML-detected intent (if ML was used)
    ml_domains: list[str] | None = None             # ML-detected domains (if ML was used)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for logging and API responses."""
        result: dict[str, Any] = {
            "score": round(self.score, 4),
            "tier": self.tier,
            "suggested_model": self.suggested_model,
            "needs_decomposition": self.needs_decomposition,
            "estimation_method": self.estimation_method,
            "estimation_ms": round(self.estimation_ms, 1),
            "dimensions": [
                {"name": d.name, "score": round(d.score, 3), "weight": d.weight, "detail": d.detail}
                for d in self.dimensions
            ],
        }
        if self.ml_intent is not None:
            result["ml_intent"] = self.ml_intent
        if self.ml_domains is not None:
            result["ml_domains"] = self.ml_domains
        return result


@dataclass
class SubTask:
    """A decomposed sub-task."""

    id: str
    description: str
    estimated_complexity: float
    dependencies: list[str] = field(default_factory=list)
    suggested_model: str | None = None
    execution_order: int = 0


@dataclass
class SubTaskResult:
    """Result of a sub-task execution."""

    task: SubTask
    result: str
    model_used: str
    duration_ms: int = 0
    success: bool = True


# ── Heuristic Keyword Lists ─────────────────────────────────────────────

# Reasoning/analysis markers (high signal for complexity)
_REASONING_MARKERS = [
    # Chinese
    "分析", "比較", "設計", "解釋", "推理", "評估", "規劃", "策略",
    "優化", "重構", "架構", "除錯", "偵錯", "整合",
    # English
    "analyze", "analyse", "compare", "design", "explain", "reason",
    "evaluate", "plan", "strategy", "trade-off", "tradeoff",
    "pros and cons", "debug", "optimize", "refactor", "architect",
    "integrate", "troubleshoot", "diagnose",
]

# Domain keyword groups
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "code": [
        "code", "function", "程式", "class", "api", "bug", "```",
        "import", "def ", "async", "return", "variable", "method",
        "compile", "runtime", "syntax", "程式碼",
    ],
    "math": [
        "calculate", "formula", "計算", "equation", "數學",
        "algorithm", "probability", "statistics", "演算法", "機率",
    ],
    "data": [
        "csv", "json", "database", "sql", "資料庫", "data",
        "dataframe", "query", "schema", "table", "資料",
    ],
    "system": [
        "file", "directory", "shell", "install", "server", "檔案",
        "docker", "deploy", "config", "環境", "terminal", "command",
    ],
    "creative": [
        "write", "story", "poem", "寫", "文章", "創作",
        "translate", "翻譯", "summarize", "摘要",
    ],
}

# Step indicator patterns (regex)
_STEP_PATTERNS = [
    r"\d+[\.\)]\s",           # "1. " or "1) "
    r"first.*then",           # "first... then..."
    r"step\s*\d",             # "step 1"
    r"第[一二三四五六七八九十]",  # Chinese ordinals
    r"首先.*然後",             # "首先...然後..."
    r"接著|最後|finally",      # sequence markers
]

# Context reference keywords
_CONTEXT_REFS = [
    "above", "earlier", "之前", "剛才", "that file", "那個",
    "前面", "mentioned", "said before", "上面",
]

# Constraint keywords
_CONSTRAINT_WORDS = [
    "must", "should", "cannot", "必須", "不能", "限制",
    "requirement", "constraint", "at most", "at least",
    "不可以", "需要", "確保", "ensure", "required",
]

# Specificity markers (lower ambiguity)
_SPECIFICITY_MARKERS = [
    "exactly", "specifically", "the file at", "具體", "明確",
    "precisely", "in particular", "特別是",
]

# Vagueness markers (higher ambiguity)
_VAGUE_MARKERS = [
    "somehow", "something", "maybe", "不知道", "大概", "隨便",
    "whatever", "anything", "some kind of", "可能",
]


# ── Complexity Estimator ─────────────────────────────────────────────────


class ComplexityEstimator:
    """Two-phase task complexity estimator.

    Phase 1: Zero-cost heuristic scoring from text features
    Phase 2: Optional LLM refinement when heuristic is ambiguous
    """

    def __init__(
        self,
        config: TaskComplexityConfig,
        model_router: "ModelRouter",
        ml_classifier: "MLComplexityClassifier | None" = None,
    ) -> None:
        self.config = config
        self.model = model_router
        self.ml_classifier = ml_classifier

    def reload_ml_classifier(self, config: TaskComplexityConfig) -> str:
        """Hot-reload ML classifier based on updated config.

        Dynamically loads, unloads, or reconfigures the ML classifier
        when settings change from the Web UI. No restart needed.

        Returns a status string describing what changed.
        """
        from pathlib import Path

        old_enabled = self.config.ml_model_enabled
        new_enabled = config.ml_model_enabled
        self.config = config

        # Case 1: ML was disabled → still disabled (no-op)
        if not old_enabled and not new_enabled:
            return "ml_unchanged"

        # Case 2: ML was enabled → now disabled (unload)
        if old_enabled and not new_enabled:
            self.ml_classifier = None
            logger.info("ml_classifier_unloaded")
            return "ml_disabled"

        # Case 3: ML was disabled → now enabled (load)
        # Case 4: ML was enabled → still enabled (reload if path changed)
        from src.core.complexity_ml import (
            MLComplexityClassifier,
            get_default_model_path,
            get_default_tokenizer_path,
        )

        model_path = (
            Path(config.ml_model_path)
            if config.ml_model_path
            else get_default_model_path()
        )
        tokenizer_path = (
            Path(config.ml_tokenizer_path)
            if config.ml_tokenizer_path
            else get_default_tokenizer_path()
        )

        if not model_path.exists():
            self.ml_classifier = None
            logger.warning("ml_classifier_model_not_found", path=str(model_path))
            return "ml_model_not_found"

        # Load or reload the classifier
        self.ml_classifier = MLComplexityClassifier(
            model_path=model_path,
            tokenizer_path=tokenizer_path,
        )
        logger.info(
            "ml_classifier_reloaded",
            model_path=str(model_path),
            mode=config.ml_estimation_mode,
        )
        return "ml_enabled" if not old_enabled else "ml_reloaded"

    async def estimate(
        self,
        user_text: str,
        session: "Session",
    ) -> ComplexityResult:
        """Estimate task complexity.

        Returns a ComplexityResult with score, tier, dimensions, and suggested model.
        """
        start = time.monotonic()

        # Phase 1: Heuristic scoring
        dimensions = self._heuristic_dimensions(user_text, session)
        heuristic_score = sum(d.score * d.weight for d in dimensions)
        heuristic_score = min(heuristic_score, 1.0)
        score = heuristic_score
        method = "heuristic"
        ml_prediction = None

        # Phase 1.5: ML model (if enabled and available)
        ml_mode = self.config.ml_estimation_mode
        if self.ml_classifier and self.config.ml_model_enabled:
            try:
                ml_prediction = self.ml_classifier.predict(user_text)
            except Exception as e:
                logger.debug("complexity_ml_predict_failed", error=str(e))

            if ml_prediction is not None:
                if ml_mode == "ml_only":
                    # Use ML score exclusively
                    score = ml_prediction.score
                    method = "ml"
                elif ml_mode == "hybrid":
                    # Blend: 60% ML + 40% heuristic
                    score = 0.6 * ml_prediction.score + 0.4 * heuristic_score
                    method = "hybrid_ml"
                elif ml_mode == "ml_refine":
                    # Use ML only in the ambiguous zone (replaces LLM refinement)
                    if self.config.ambiguity_low < heuristic_score < self.config.ambiguity_high:
                        score = 0.6 * ml_prediction.score + 0.4 * heuristic_score
                        method = "ml_refine"
                    else:
                        score = heuristic_score
                        method = "heuristic"

                logger.debug(
                    "complexity_ml_used",
                    mode=ml_mode,
                    ml_score=round(ml_prediction.score, 3),
                    heuristic_score=round(heuristic_score, 3),
                    final_score=round(score, 3),
                    ml_tier=ml_prediction.tier,
                    ml_intent=ml_prediction.intent,
                    ml_domains=ml_prediction.domains,
                )

        # Phase 2: LLM refinement (only for ambiguous scores, skip if ML already handled it)
        if (
            method == "heuristic"  # ML didn't handle it
            and self.config.llm_refinement
            and self.config.ambiguity_low < score < self.config.ambiguity_high
        ):
            try:
                llm_score = await self._llm_refine(user_text, score)
                if llm_score is not None:
                    # Blend: 60% LLM, 40% heuristic
                    score = 0.6 * llm_score + 0.4 * score
                    method = "hybrid"
            except Exception as e:
                logger.debug("complexity_llm_refine_failed", error=str(e))
                # Fall back to heuristic only

        elapsed_ms = (time.monotonic() - start) * 1000

        # Determine tier
        tier = self._score_to_tier(score)

        # Resolve model
        capability_map = ModelCapabilityMap(self.config, self.model)
        suggested_model = capability_map.resolve_model(score, tier)

        # Check if decomposition is needed
        needs_decomp = (
            self.config.decomposition_enabled
            and score >= self.config.decomposition_threshold
        )

        result = ComplexityResult(
            score=score,
            tier=tier,
            dimensions=dimensions,
            suggested_model=suggested_model,
            needs_decomposition=needs_decomp,
            estimation_method=method,
            estimation_ms=elapsed_ms,
        )

        # Attach ML prediction details if available
        if ml_prediction is not None:
            result.ml_intent = ml_prediction.intent
            result.ml_domains = ml_prediction.domains

        logger.info(
            "complexity_estimated",
            score=round(score, 3),
            tier=tier,
            method=method,
            model=suggested_model,
            decompose=needs_decomp,
            ms=round(elapsed_ms, 1),
        )

        return result

    def _heuristic_dimensions(
        self,
        text: str,
        session: "Session",
    ) -> list[ComplexityDimension]:
        """Compute all heuristic dimensions from text features."""
        weights = self.config.dimension_weights
        text_lower = text.lower()
        dims: list[ComplexityDimension] = []

        # 1. Token length (proxy via char count / 4)
        est_tokens = len(text) / 4
        length_score = min(est_tokens / 2000, 1.0)
        dims.append(ComplexityDimension(
            name="token_length",
            score=length_score,
            weight=weights.get("token_length", 0.10),
            detail=f"~{int(est_tokens)} tokens",
        ))

        # 2. Reasoning markers
        found_markers = [m for m in _REASONING_MARKERS if m.lower() in text_lower]
        reasoning_score = min(len(found_markers) / 5, 1.0)
        dims.append(ComplexityDimension(
            name="reasoning_markers",
            score=reasoning_score,
            weight=weights.get("reasoning_markers", 0.20),
            detail=f"found: {', '.join(found_markers[:5])}" if found_markers else "none",
        ))

        # 3. Domain count
        domains_detected: list[str] = []
        for domain, keywords in _DOMAIN_KEYWORDS.items():
            if any(k.lower() in text_lower for k in keywords):
                domains_detected.append(domain)
        domain_score = min(len(domains_detected) / 3, 1.0)
        dims.append(ComplexityDimension(
            name="domain_count",
            score=domain_score,
            weight=weights.get("domain_count", 0.15),
            detail=f"domains: {', '.join(domains_detected)}" if domains_detected else "none",
        ))

        # 4. Step indicators
        step_count = sum(
            1 for p in _STEP_PATTERNS if re.search(p, text, re.IGNORECASE)
        )
        step_score = min(step_count / 3, 1.0)
        dims.append(ComplexityDimension(
            name="step_indicators",
            score=step_score,
            weight=weights.get("step_indicators", 0.15),
            detail=f"{step_count} step patterns",
        ))

        # 5. Code complexity
        code_blocks = text.count("```")
        # Also check for inline code patterns
        inline_code = len(re.findall(r"`[^`]+`", text))
        code_score = min((code_blocks + inline_code * 0.3) / 4, 1.0)
        dims.append(ComplexityDimension(
            name="code_complexity",
            score=code_score,
            weight=weights.get("code_complexity", 0.15),
            detail=f"{code_blocks} code blocks, {inline_code} inline",
        ))

        # 6. Context dependency
        context_ref_count = sum(
            1 for w in _CONTEXT_REFS if w.lower() in text_lower
        )
        # Factor in conversation length
        from src.core.types import Role
        history_len = len([
            m for m in session.messages if m.role == Role.USER
        ]) if session.messages else 0
        context_score = min((context_ref_count + history_len / 20) / 3, 1.0)
        dims.append(ComplexityDimension(
            name="context_dependency",
            score=context_score,
            weight=weights.get("context_dependency", 0.10),
            detail=f"{context_ref_count} refs, {history_len} history msgs",
        ))

        # 7. Constraint count
        constraint_count = sum(
            1 for c in _CONSTRAINT_WORDS if c.lower() in text_lower
        )
        constraint_score = min(constraint_count / 4, 1.0)
        dims.append(ComplexityDimension(
            name="constraint_count",
            score=constraint_score,
            weight=weights.get("constraint_count", 0.10),
            detail=f"{constraint_count} constraints",
        ))

        # 8. Ambiguity (inverse specificity)
        specificity = sum(1 for s in _SPECIFICITY_MARKERS if s.lower() in text_lower)
        vagueness = sum(1 for v in _VAGUE_MARKERS if v.lower() in text_lower)
        ambiguity_score = max(0, min((vagueness - specificity + 1) / 3, 1.0))
        dims.append(ComplexityDimension(
            name="ambiguity",
            score=ambiguity_score,
            weight=weights.get("ambiguity", 0.05),
            detail=f"vague={vagueness}, specific={specificity}",
        ))

        return dims

    async def _llm_refine(
        self,
        user_text: str,
        heuristic_score: float,
    ) -> float | None:
        """Use a cheap/fast model to refine the heuristic score.

        Returns a float 0.0-1.0 or None if refinement fails.
        """
        refinement_model = self.config.refinement_model or None

        prompt = (
            "You are a task complexity evaluator. Rate the complexity of the "
            "following user task from 1 to 10 (1=trivial greeting, 10=expert "
            "multi-domain analysis requiring extensive reasoning).\n\n"
            "Consider:\n"
            "- How many reasoning steps are needed?\n"
            "- How many different domains/skills does it require?\n"
            "- Could a simple FAQ chatbot handle this?\n"
            "- Does it need code generation, analysis, or multi-step planning?\n\n"
            f"Task: {user_text[:1000]}\n\n"
            f"Current heuristic estimate: {heuristic_score:.2f} (0-1 scale)\n\n"
            'Respond with ONLY valid JSON: {"score": N, "reason": "brief reason"}'
        )

        try:
            response = await self.model.complete(
                messages=[
                    {"role": "system", "content": "You are a task complexity evaluator. Respond only with JSON."},
                    {"role": "user", "content": prompt},
                ],
                model=refinement_model,
                tools=None,
                temperature=0.1,
                max_tokens=100,
            )

            import json
            content = (response.content or "").strip()
            # Extract JSON from potential markdown wrapper
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            data = json.loads(content)
            raw_score = float(data.get("score", 5))
            # Convert 1-10 scale to 0.0-1.0
            return max(0.0, min((raw_score - 1) / 9, 1.0))

        except Exception as e:
            logger.debug("llm_refine_parse_failed", error=str(e))
            return None

    def _score_to_tier(self, score: float) -> str:
        """Convert a numeric score to a tier name."""
        boundaries = self.config.tier_boundaries
        if score < boundaries.get("trivial", 0.15):
            return "trivial"
        elif score < boundaries.get("simple", 0.35):
            return "simple"
        elif score < boundaries.get("moderate", 0.60):
            return "moderate"
        elif score < boundaries.get("complex", 0.85):
            return "complex"
        else:
            return "expert"


# ── Model Capability Map ─────────────────────────────────────────────────


class ModelCapabilityMap:
    """Maps complexity tiers to available models.

    Uses configured model preferences or auto-detects from available providers.
    """

    def __init__(
        self,
        config: TaskComplexityConfig,
        model_router: "ModelRouter",
    ) -> None:
        self.config = config
        self.model_router = model_router

    def resolve_model(self, score: float, tier: str) -> str | None:
        """Return the best model for the given complexity level.

        Returns None to keep the default model (no override).
        """
        if tier in ("trivial", "simple"):
            model = self.config.fast_model
            if model:
                return model
            # Auto-detect: try to find a fast model from known providers
            return self._auto_detect_fast()

        elif tier == "moderate":
            model = self.config.standard_model
            if model:
                return model
            # Use default model (no override needed)
            return None

        elif tier in ("complex", "expert"):
            model = self.config.frontier_model
            if model:
                return model
            return self._auto_detect_frontier()

        return None

    def _auto_detect_fast(self) -> str | None:
        """Try to auto-detect the cheapest available model."""
        # Priority order: local → gemini-flash → haiku
        providers = self.model_router.config.models.providers
        fast_candidates = [
            # Local models (free)
            "ollama/llama3.2:3b",
            "ollama/mistral-nemo",
            "ollama/qwen3:32b",
            # Cloud flash/haiku models (cheap)
            "gemini/gemini-3-flash",
            "gemini/gemini-2.5-flash",
            "anthropic/claude-haiku-4.5",
            "openai/gpt-oss-20b",
        ]
        for candidate in fast_candidates:
            provider = candidate.split("/")[0]
            if provider in providers:
                provider_cfg = providers[provider]
                if provider_cfg.base_url or provider_cfg.get_api_key():
                    # Check if the model is in known_models
                    if candidate in provider_cfg.known_models or provider == "ollama":
                        return candidate
        return None

    def _auto_detect_frontier(self) -> str | None:
        """Try to auto-detect the most capable available model."""
        providers = self.model_router.config.models.providers
        frontier_candidates = [
            "anthropic/claude-opus-4.6",
            "openai/gpt-5.3-codex",
            "openai/gpt-5.2",
            "gemini/gemini-3-pro",
            "gemini/gemini-2.5-pro",
        ]
        for candidate in frontier_candidates:
            provider = candidate.split("/")[0]
            if provider in providers:
                provider_cfg = providers[provider]
                if provider_cfg.get_api_key():
                    if candidate in provider_cfg.known_models:
                        return candidate
        return None


# ── Task Decomposer ──────────────────────────────────────────────────────


class TaskDecomposer:
    """Decomposes complex tasks into manageable sub-tasks.

    Uses a frontier model to analyze the task and produce a structured
    decomposition plan with dependencies and execution order.
    """

    def __init__(
        self,
        model_router: "ModelRouter",
        config: TaskComplexityConfig,
    ) -> None:
        self.model = model_router
        self.config = config

    async def decompose(
        self,
        user_text: str,
        complexity: ComplexityResult,
        session: "Session",
    ) -> list[SubTask]:
        """Break a complex task into sub-tasks using LLM.

        Each sub-task has a description, estimated complexity, dependencies,
        and execution order.
        """
        import json

        max_subtasks = self.config.max_subtasks

        prompt = (
            "You are a task decomposition specialist. Break down the following "
            "complex task into smaller, manageable sub-tasks.\n\n"
            f"Original task: {user_text[:2000]}\n\n"
            f"Estimated complexity: {complexity.score:.2f} ({complexity.tier})\n\n"
            "Rules:\n"
            f"- Create at most {max_subtasks} sub-tasks\n"
            "- Each sub-task should be independently executable\n"
            "- Specify dependencies between sub-tasks (by ID)\n"
            "- Assign execution_order (0-based, tasks with same order can run in parallel)\n"
            "- Estimate complexity for each sub-task (0.0-1.0)\n\n"
            "Respond with ONLY valid JSON:\n"
            "{\n"
            '  "subtasks": [\n'
            "    {\n"
            '      "id": "t1",\n'
            '      "description": "Clear description of what to do",\n'
            '      "estimated_complexity": 0.4,\n'
            '      "dependencies": [],\n'
            '      "execution_order": 0\n'
            "    }\n"
            "  ]\n"
            "}"
        )

        try:
            # Use frontier model for decomposition
            decompose_model = self.config.frontier_model or None

            response = await self.model.complete(
                messages=[
                    {"role": "system", "content": "You are a task decomposition specialist. Respond only with JSON."},
                    {"role": "user", "content": prompt},
                ],
                model=decompose_model,
                tools=None,
                temperature=0.3,
                max_tokens=1500,
            )

            content = (response.content or "").strip()
            # Extract JSON from potential markdown wrapper
            if "```" in content:
                parts = content.split("```")
                for part in parts[1:]:
                    if part.strip().startswith("json"):
                        content = part.strip()[4:].strip()
                        break
                    elif part.strip().startswith("{"):
                        content = part.strip()
                        break

            data = json.loads(content)
            subtasks_data = data.get("subtasks", [])

            subtasks: list[SubTask] = []
            estimator = ComplexityEstimator(self.config, self.model)
            capability_map = ModelCapabilityMap(self.config, self.model)

            for item in subtasks_data[:max_subtasks]:
                est_complexity = float(item.get("estimated_complexity", 0.5))
                tier = estimator._score_to_tier(est_complexity)
                suggested = capability_map.resolve_model(est_complexity, tier)

                subtasks.append(SubTask(
                    id=item.get("id", f"t{len(subtasks)+1}"),
                    description=item.get("description", ""),
                    estimated_complexity=est_complexity,
                    dependencies=item.get("dependencies", []),
                    suggested_model=suggested,
                    execution_order=int(item.get("execution_order", 0)),
                ))

            logger.info(
                "task_decomposed",
                original_complexity=round(complexity.score, 3),
                subtask_count=len(subtasks),
            )

            return subtasks

        except Exception as e:
            logger.warning("task_decomposition_failed", error=str(e))
            # Fallback: return the original task as a single sub-task
            return [SubTask(
                id="t1",
                description=user_text,
                estimated_complexity=complexity.score,
                suggested_model=complexity.suggested_model,
                execution_order=0,
            )]

    async def synthesize(
        self,
        original_task: str,
        sub_results: list[SubTaskResult],
    ) -> str:
        """Combine sub-task results into a coherent final response.

        Uses the frontier model to synthesize all sub-results into a
        unified response that addresses the original task.
        """
        if not sub_results:
            return "No sub-task results to synthesize."

        # If only one sub-task, just return its result
        if len(sub_results) == 1:
            return sub_results[0].result

        # Build synthesis prompt
        results_text = ""
        for i, sr in enumerate(sub_results, 1):
            status = "✓" if sr.success else "✗"
            results_text += (
                f"\n--- Sub-task {i} [{status}] (model: {sr.model_used}) ---\n"
                f"Task: {sr.task.description}\n"
                f"Result:\n{sr.result[:3000]}\n"
            )

        prompt = (
            "You need to synthesize the results of multiple sub-tasks into a "
            "coherent, unified response for the user.\n\n"
            f"Original user request: {original_task[:1000]}\n\n"
            f"Sub-task results:{results_text}\n\n"
            "Instructions:\n"
            "- Combine all sub-task results into one coherent response\n"
            "- Address the original request directly\n"
            "- Resolve any conflicts between sub-task results\n"
            "- Use the user's language (Chinese or English based on the original request)\n"
            "- Do NOT mention that the task was split — present as a unified answer"
        )

        try:
            response = await self.model.complete(
                messages=[
                    {"role": "system", "content": "You are a helpful assistant synthesizing multiple analysis results."},
                    {"role": "user", "content": prompt},
                ],
                model=self.config.frontier_model or None,
                tools=None,
                temperature=0.5,
                max_tokens=4096,
            )
            return response.content or "Failed to synthesize results."
        except Exception as e:
            logger.warning("synthesis_failed", error=str(e))
            # Fallback: concatenate results
            parts = []
            for sr in sub_results:
                parts.append(f"**{sr.task.description}**\n{sr.result}")
            return "\n\n---\n\n".join(parts)
