"""Evaluate the complexity classifier model.

Supports evaluation of both:
1. PyTorch model (.pt) — requires torch + transformers
2. ONNX model (.onnx) — requires onnxruntime

Usage:
    # Evaluate PyTorch model
    python -m training.evaluate --model training/output/best_model.pt --data training/data/test.jsonl

    # Evaluate ONNX model
    python -m training.evaluate --onnx training/output/complexity_model_int8.onnx --data training/data/test.jsonl

    # Compare heuristic vs ML vs both
    python -m training.evaluate --onnx ~/.kuro/models/complexity_model_int8.onnx --data training/data/test.jsonl --compare
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from training.train import (
    TIER_NAMES,
    DOMAIN_NAMES,
    INTENT_NAMES,
    TIER_TO_IDX,
    INTENT_TO_IDX,
    DOMAIN_TO_IDX,
)


def load_test_data(path: str) -> list[dict]:
    """Load test data from JSONL file."""
    samples = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples


def evaluate_onnx(
    model_path: str,
    tokenizer_path: str,
    samples: list[dict],
    max_length: int = 256,
) -> dict:
    """Evaluate ONNX model on test samples."""
    import onnxruntime as ort
    from transformers import AutoTokenizer

    session = ort.InferenceSession(
        model_path,
        providers=["CPUExecutionProvider"],
    )
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True)

    score_errors = []
    tier_correct = 0
    intent_correct = 0
    domain_tp = 0
    domain_fp = 0
    domain_fn = 0
    total_time = 0.0
    tier_confusion = {}  # (true, pred) -> count

    for sample in samples:
        text = sample["text"]
        true_score = sample["score"]
        true_tier = sample["tier"]
        true_intent = sample.get("intent", "question")
        true_domains = set(sample.get("domains", []))

        # Tokenize
        inputs = tokenizer(
            text,
            max_length=max_length,
            truncation=True,
            padding="max_length",
            return_tensors="np",
        )

        # Inference
        start = time.perf_counter()
        outputs = session.run(None, {
            "input_ids": inputs["input_ids"].astype(np.int64),
            "attention_mask": inputs["attention_mask"].astype(np.int64),
        })
        elapsed = (time.perf_counter() - start) * 1000
        total_time += elapsed

        # Parse outputs
        pred_score = float(outputs[0].flatten()[0])
        pred_score = max(0.0, min(pred_score, 1.0))

        pred_tier_idx = int(np.argmax(outputs[1][0]))
        pred_tier = TIER_NAMES[pred_tier_idx] if pred_tier_idx < len(TIER_NAMES) else "moderate"

        domain_probs = 1.0 / (1.0 + np.exp(-outputs[2][0]))  # sigmoid
        pred_domains = set(
            DOMAIN_NAMES[i] for i, p in enumerate(domain_probs) if p > 0.5 and i < len(DOMAIN_NAMES)
        )

        pred_intent_idx = int(np.argmax(outputs[3][0]))
        pred_intent = INTENT_NAMES[pred_intent_idx] if pred_intent_idx < len(INTENT_NAMES) else "question"

        # Score MAE
        score_errors.append(abs(pred_score - true_score))

        # Tier accuracy
        if pred_tier == true_tier:
            tier_correct += 1
        key = (true_tier, pred_tier)
        tier_confusion[key] = tier_confusion.get(key, 0) + 1

        # Intent accuracy
        if pred_intent == true_intent:
            intent_correct += 1

        # Domain metrics
        for d in pred_domains & true_domains:
            domain_tp += 1
        for d in pred_domains - true_domains:
            domain_fp += 1
        for d in true_domains - pred_domains:
            domain_fn += 1

    n = len(samples)
    precision = domain_tp / max(domain_tp + domain_fp, 1)
    recall = domain_tp / max(domain_tp + domain_fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-8)

    return {
        "score_mae": sum(score_errors) / max(n, 1),
        "score_max_error": max(score_errors) if score_errors else 0,
        "tier_accuracy": tier_correct / max(n, 1),
        "intent_accuracy": intent_correct / max(n, 1),
        "domain_f1": f1,
        "domain_precision": precision,
        "domain_recall": recall,
        "avg_inference_ms": total_time / max(n, 1),
        "total_samples": n,
        "tier_confusion": tier_confusion,
    }


def print_results(results: dict, label: str = "Model") -> None:
    """Pretty-print evaluation results."""
    print(f"\n{'='*60}")
    print(f"  {label} Evaluation Results")
    print(f"{'='*60}")
    print(f"  Total samples:     {results['total_samples']}")
    print(f"  Avg inference:     {results['avg_inference_ms']:.1f} ms")
    print(f"  Score MAE:         {results['score_mae']:.4f}")
    print(f"  Score Max Error:   {results['score_max_error']:.4f}")
    print(f"  Tier Accuracy:     {results['tier_accuracy']:.2%}")
    print(f"  Intent Accuracy:   {results['intent_accuracy']:.2%}")
    print(f"  Domain F1:         {results['domain_f1']:.2%}")
    print(f"  Domain Precision:  {results['domain_precision']:.2%}")
    print(f"  Domain Recall:     {results['domain_recall']:.2%}")

    # Tier confusion matrix
    confusion = results.get("tier_confusion", {})
    if confusion:
        print(f"\n  Tier Confusion Matrix:")
        print(f"  {'':>10} | " + " | ".join(f"{t:>8}" for t in TIER_NAMES))
        print(f"  {'-'*10}-+-" + "-+-".join("-"*8 for _ in TIER_NAMES))
        for true_tier in TIER_NAMES:
            row = []
            for pred_tier in TIER_NAMES:
                count = confusion.get((true_tier, pred_tier), 0)
                row.append(f"{count:>8}")
            print(f"  {true_tier:>10} | " + " | ".join(row))

    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Evaluate complexity classifier")
    parser.add_argument("--data", type=str, required=True, help="Test data JSONL")
    parser.add_argument("--onnx", type=str, default=None, help="ONNX model path")
    parser.add_argument("--model", type=str, default=None, help="PyTorch model path (directory)")
    parser.add_argument("--tokenizer", type=str, default=None, help="Tokenizer path (auto-detected)")
    parser.add_argument("--max-length", type=int, default=256, help="Max token length")
    parser.add_argument("--compare", action="store_true", help="Compare with heuristic estimator")
    args = parser.parse_args()

    # Load test data
    print(f"Loading test data: {args.data}")
    samples = load_test_data(args.data)
    print(f"Loaded {len(samples)} samples")

    # Evaluate ONNX model
    if args.onnx:
        # Auto-detect tokenizer path
        tokenizer_path = args.tokenizer
        if not tokenizer_path:
            onnx_dir = Path(args.onnx).parent
            candidates = [
                onnx_dir / "complexity_tokenizer",
                onnx_dir / "tokenizer",
                onnx_dir.parent / "output" / "tokenizer",
            ]
            for c in candidates:
                if c.exists():
                    tokenizer_path = str(c)
                    break

        if not tokenizer_path:
            print("Error: Could not find tokenizer directory. Use --tokenizer to specify.")
            return

        print(f"Using tokenizer: {tokenizer_path}")
        results = evaluate_onnx(args.onnx, tokenizer_path, samples, args.max_length)
        print_results(results, "ONNX Model")

    # Compare with heuristic (if requested and Kuro is available)
    if args.compare:
        try:
            from src.config import TaskComplexityConfig
            from src.core.complexity import ComplexityEstimator

            print("\nEvaluating heuristic estimator for comparison...")
            heuristic_config = TaskComplexityConfig(
                llm_refinement=False,
                ml_model_enabled=False,
            )

            # Create a mock session for heuristic evaluation
            class MockSession:
                messages = []

            mock_session = MockSession()

            score_errors = []
            tier_correct = 0
            total_time = 0.0

            for sample in samples:
                start = time.perf_counter()

                # Use internal heuristic method directly
                estimator = ComplexityEstimator(heuristic_config, model_router=None)
                dims = estimator._heuristic_dimensions(sample["text"], mock_session)
                h_score = sum(d.score * d.weight for d in dims)
                h_score = min(h_score, 1.0)
                h_tier = estimator._score_to_tier(h_score)

                elapsed = (time.perf_counter() - start) * 1000
                total_time += elapsed

                score_errors.append(abs(h_score - sample["score"]))
                if h_tier == sample["tier"]:
                    tier_correct += 1

            n = len(samples)
            heuristic_results = {
                "score_mae": sum(score_errors) / max(n, 1),
                "score_max_error": max(score_errors) if score_errors else 0,
                "tier_accuracy": tier_correct / max(n, 1),
                "intent_accuracy": 0,  # Heuristic doesn't classify intent
                "domain_f1": 0,        # Heuristic doesn't classify domains
                "domain_precision": 0,
                "domain_recall": 0,
                "avg_inference_ms": total_time / max(n, 1),
                "total_samples": n,
                "tier_confusion": {},
            }
            print_results(heuristic_results, "Heuristic (baseline)")

        except ImportError as e:
            print(f"Could not import Kuro modules for comparison: {e}")


if __name__ == "__main__":
    main()
