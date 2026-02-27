"""Local ML model for task complexity classification.

Uses a fine-tuned DistilBERT multilingual model exported to ONNX format
for CPU-only inference. The model has 4 output heads:
1. Score (regression): 0.0-1.0 complexity score
2. Tier (classification): trivial/simple/moderate/complex/expert
3. Domain (multi-label): code/math/data/system/creative
4. Intent (classification): greeting/question/code_gen/analysis/debug/planning/creative/multi_step

Requirements (inference only):
    pip install onnxruntime  # ~15MB, CPU-only

The model file (~65MB after INT8 quantization) is stored at:
    ~/.kuro/models/complexity_model_int8.onnx
    ~/.kuro/models/complexity_tokenizer/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

# ── Constants ──────────────────────────────────────────────────────────────

TIER_NAMES = ["trivial", "simple", "moderate", "complex", "expert"]
DOMAIN_NAMES = ["code", "math", "data", "system", "creative"]
INTENT_NAMES = [
    "greeting",
    "question",
    "code_gen",
    "analysis",
    "debug",
    "planning",
    "creative",
    "multi_step",
]

# Default model directory
_DEFAULT_MODEL_DIR = "models"
_DEFAULT_MODEL_NAME = "complexity_model_int8.onnx"
_DEFAULT_TOKENIZER_DIR = "complexity_tokenizer"


# ── Data Classes ──────────────────────────────────────────────────────────


@dataclass
class MLPrediction:
    """Result from the local ML complexity classifier."""

    score: float                        # 0.0 - 1.0 complexity score
    tier: str                           # "trivial" | "simple" | ... | "expert"
    domains: list[str]                  # detected domains (e.g., ["code", "data"])
    intent: str                         # detected intent (e.g., "analysis")
    tier_probabilities: dict[str, float] = field(default_factory=dict)
    intent_probabilities: dict[str, float] = field(default_factory=dict)
    domain_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for logging and API responses."""
        return {
            "score": round(self.score, 4),
            "tier": self.tier,
            "domains": self.domains,
            "intent": self.intent,
            "tier_probs": {k: round(v, 3) for k, v in self.tier_probabilities.items()},
            "intent_probs": {k: round(v, 3) for k, v in self.intent_probabilities.items()},
            "domain_scores": {k: round(v, 3) for k, v in self.domain_scores.items()},
        }


# ── ML Classifier ─────────────────────────────────────────────────────────


class MLComplexityClassifier:
    """Local ML model for complexity classification.

    Uses ONNX Runtime for CPU-only inference with a fine-tuned
    DistilBERT multilingual model. The model has 4 output heads
    for simultaneous score regression, tier/intent classification,
    and domain detection.

    Usage:
        classifier = MLComplexityClassifier("~/.kuro/models/complexity_model_int8.onnx")
        prediction = classifier.predict("分析這個程式的效能瓶頸")
        print(prediction.score, prediction.tier, prediction.domains)
    """

    def __init__(
        self,
        model_path: str | Path,
        tokenizer_path: str | Path | None = None,
        max_length: int = 256,
    ) -> None:
        """Initialize the ML classifier.

        Args:
            model_path: Path to the ONNX model file.
            tokenizer_path: Path to the tokenizer directory.
                           If None, inferred from model_path.
            max_length: Maximum token length for input text.
        """
        self.model_path = Path(model_path)
        self.max_length = max_length
        self._session = None
        self._tokenizer = None
        self._loaded = False

        # Infer tokenizer path
        if tokenizer_path is None:
            # Look for tokenizer directory next to model file
            self.tokenizer_path = self.model_path.parent / _DEFAULT_TOKENIZER_DIR
        else:
            self.tokenizer_path = Path(tokenizer_path)

        # Lazy load on first predict
        self._load_error: str | None = None

    def _ensure_loaded(self) -> bool:
        """Lazy-load the model and tokenizer on first use.

        Returns True if successfully loaded, False otherwise.
        """
        if self._loaded:
            return True

        if self._load_error:
            return False

        try:
            import onnxruntime as ort

            # Create ONNX inference session (CPU only)
            sess_options = ort.SessionOptions()
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_options.intra_op_num_threads = 2  # Limit CPU threads

            self._session = ort.InferenceSession(
                str(self.model_path),
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            )

            logger.info(
                "ml_classifier_loaded",
                model=str(self.model_path),
                size_mb=round(self.model_path.stat().st_size / 1024 / 1024, 1),
            )

        except ImportError:
            self._load_error = (
                "onnxruntime not installed. Install with: pip install onnxruntime"
            )
            logger.warning("ml_classifier_unavailable", reason=self._load_error)
            return False
        except Exception as e:
            self._load_error = f"Failed to load ONNX model: {e}"
            logger.warning("ml_classifier_load_failed", error=str(e))
            return False

        try:
            from transformers import AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(
                str(self.tokenizer_path),
                local_files_only=True,
            )
            logger.info("ml_tokenizer_loaded", path=str(self.tokenizer_path))

        except ImportError:
            # Fallback: try to use a basic tokenizer without transformers
            self._load_error = (
                "transformers not installed for tokenizer. "
                "Install with: pip install transformers"
            )
            logger.warning("ml_tokenizer_unavailable", reason=self._load_error)
            # Clean up session
            self._session = None
            return False
        except Exception as e:
            self._load_error = f"Failed to load tokenizer: {e}"
            logger.warning("ml_tokenizer_load_failed", error=str(e))
            self._session = None
            return False

        self._loaded = True
        return True

    def predict(self, text: str) -> MLPrediction | None:
        """Run inference on the input text.

        Returns an MLPrediction with score, tier, domains, and intent,
        or None if the model is not available.

        Args:
            text: User message text to classify.
        """
        if not self._ensure_loaded():
            return None

        import numpy as np

        try:
            # Tokenize input
            inputs = self._tokenizer(
                text,
                max_length=self.max_length,
                truncation=True,
                padding="max_length",
                return_tensors="np",
            )

            # Build feed dict (ONNX expects specific input names)
            feed = {
                "input_ids": inputs["input_ids"].astype(np.int64),
                "attention_mask": inputs["attention_mask"].astype(np.int64),
            }

            # Run inference
            outputs = self._session.run(None, feed)

            # Parse outputs (4 heads)
            # Head 1: Score regression (shape: [1, 1])
            raw_score = float(outputs[0][0][0]) if outputs[0].ndim > 1 else float(outputs[0][0])
            score = max(0.0, min(raw_score, 1.0))  # Clamp to [0, 1]

            # Head 2: Tier classification (shape: [1, 5])
            tier_logits = outputs[1][0]
            tier_probs = _softmax(tier_logits)
            tier_idx = int(np.argmax(tier_probs))
            tier = TIER_NAMES[tier_idx] if tier_idx < len(TIER_NAMES) else "moderate"

            # Head 3: Domain multi-label (shape: [1, 5])
            domain_logits = outputs[2][0]
            domain_probs = _sigmoid(domain_logits)
            domains = [
                DOMAIN_NAMES[i]
                for i, prob in enumerate(domain_probs)
                if prob > 0.5 and i < len(DOMAIN_NAMES)
            ]
            domain_scores = {
                DOMAIN_NAMES[i]: float(prob)
                for i, prob in enumerate(domain_probs)
                if i < len(DOMAIN_NAMES)
            }

            # Head 4: Intent classification (shape: [1, 8])
            intent_logits = outputs[3][0]
            intent_probs = _softmax(intent_logits)
            intent_idx = int(np.argmax(intent_probs))
            intent = INTENT_NAMES[intent_idx] if intent_idx < len(INTENT_NAMES) else "question"

            return MLPrediction(
                score=score,
                tier=tier,
                domains=domains,
                intent=intent,
                tier_probabilities={
                    TIER_NAMES[i]: float(p) for i, p in enumerate(tier_probs) if i < len(TIER_NAMES)
                },
                intent_probabilities={
                    INTENT_NAMES[i]: float(p) for i, p in enumerate(intent_probs) if i < len(INTENT_NAMES)
                },
                domain_scores=domain_scores,
            )

        except Exception as e:
            logger.warning("ml_predict_failed", error=str(e))
            return None

    @property
    def is_available(self) -> bool:
        """Check if the model is loaded and ready for inference."""
        return self._loaded or (self._load_error is None and self.model_path.exists())

    @property
    def load_error(self) -> str | None:
        """Return the load error message, if any."""
        return self._load_error

    def get_info(self) -> dict[str, Any]:
        """Return model metadata for diagnostics."""
        info: dict[str, Any] = {
            "loaded": self._loaded,
            "model_path": str(self.model_path),
            "tokenizer_path": str(self.tokenizer_path),
            "model_exists": self.model_path.exists(),
            "tokenizer_exists": self.tokenizer_path.exists(),
        }
        if self._load_error:
            info["error"] = self._load_error
        if self._loaded and self.model_path.exists():
            info["model_size_mb"] = round(self.model_path.stat().st_size / 1024 / 1024, 1)
        if self._session:
            info["input_names"] = [inp.name for inp in self._session.get_inputs()]
            info["output_names"] = [out.name for out in self._session.get_outputs()]
        return info


# ── Utility Functions ─────────────────────────────────────────────────────


def _softmax(x):
    """Compute softmax probabilities from logits."""
    import numpy as np

    x = np.array(x, dtype=np.float64)
    x_max = np.max(x)
    exp_x = np.exp(x - x_max)
    return exp_x / np.sum(exp_x)


def _sigmoid(x):
    """Compute sigmoid probabilities from logits."""
    import numpy as np

    x = np.array(x, dtype=np.float64)
    return 1.0 / (1.0 + np.exp(-x))


def get_default_model_path() -> Path:
    """Return the default model file path (~/.kuro/models/complexity_model_int8.onnx)."""
    from src.config import get_kuro_home

    return get_kuro_home() / _DEFAULT_MODEL_DIR / _DEFAULT_MODEL_NAME


def get_default_tokenizer_path() -> Path:
    """Return the default tokenizer directory path."""
    from src.config import get_kuro_home

    return get_kuro_home() / _DEFAULT_MODEL_DIR / _DEFAULT_TOKENIZER_DIR
