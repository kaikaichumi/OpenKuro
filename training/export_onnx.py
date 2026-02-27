"""Export the trained PyTorch model to ONNX format with INT8 quantization.

Converts the fine-tuned DistilBERT complexity classifier to ONNX,
then applies INT8 dynamic quantization for smaller size and faster inference.

Prerequisites:
    pip install torch transformers onnx onnxruntime

Usage:
    python -m training.export_onnx --model training/output
    python -m training.export_onnx --model training/output --output ~/.kuro/models/
    python -m training.export_onnx --model training/output --no-quantize
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import torch

from training.train import ComplexityModel, BASE_MODEL


def export_to_onnx(
    model: ComplexityModel,
    output_path: Path,
    max_length: int = 256,
    opset_version: int = 14,
) -> None:
    """Export PyTorch model to ONNX format.

    Args:
        model: Trained ComplexityModel.
        output_path: Path for the .onnx file.
        max_length: Max sequence length (must match training).
        opset_version: ONNX opset version.
    """
    model.eval()
    device = next(model.parameters()).device

    # Create dummy input
    dummy_input_ids = torch.ones(1, max_length, dtype=torch.long, device=device)
    dummy_attention_mask = torch.ones(1, max_length, dtype=torch.long, device=device)

    # Define input/output names
    input_names = ["input_ids", "attention_mask"]
    output_names = ["score", "tier_logits", "domain_logits", "intent_logits"]

    # Dynamic axes for variable batch size
    dynamic_axes = {
        "input_ids": {0: "batch_size"},
        "attention_mask": {0: "batch_size"},
        "score": {0: "batch_size"},
        "tier_logits": {0: "batch_size"},
        "domain_logits": {0: "batch_size"},
        "intent_logits": {0: "batch_size"},
    }

    print(f"Exporting to ONNX: {output_path}")
    torch.onnx.export(
        model,
        (dummy_input_ids, dummy_attention_mask),
        str(output_path),
        input_names=input_names,
        output_names=output_names,
        dynamic_axes=dynamic_axes,
        opset_version=opset_version,
        do_constant_folding=True,
    )

    size_mb = output_path.stat().st_size / 1024 / 1024
    print(f"ONNX model saved: {output_path} ({size_mb:.1f} MB)")


def quantize_onnx(
    input_path: Path,
    output_path: Path,
) -> None:
    """Apply INT8 dynamic quantization to the ONNX model.

    This reduces model size by ~75% and improves inference speed.

    Args:
        input_path: Path to the original ONNX model.
        output_path: Path for the quantized model.
    """
    from onnxruntime.quantization import quantize_dynamic, QuantType

    print(f"Quantizing to INT8: {output_path}")
    quantize_dynamic(
        model_input=str(input_path),
        model_output=str(output_path),
        weight_type=QuantType.QInt8,
    )

    original_size = input_path.stat().st_size / 1024 / 1024
    quantized_size = output_path.stat().st_size / 1024 / 1024
    reduction = (1 - quantized_size / original_size) * 100

    print(f"Quantized model saved: {output_path}")
    print(f"Size: {original_size:.1f} MB → {quantized_size:.1f} MB ({reduction:.0f}% reduction)")


def verify_onnx(model_path: Path, max_length: int = 256) -> bool:
    """Verify the ONNX model works correctly.

    Runs a simple inference test and checks output shapes.
    """
    import numpy as np
    import onnxruntime as ort

    print(f"\nVerifying ONNX model: {model_path}")

    session = ort.InferenceSession(
        str(model_path),
        providers=["CPUExecutionProvider"],
    )

    # Check input/output names
    inputs = {inp.name: inp for inp in session.get_inputs()}
    outputs = {out.name: out for out in session.get_outputs()}

    print(f"  Inputs: {list(inputs.keys())}")
    print(f"  Outputs: {list(outputs.keys())}")

    # Run dummy inference
    dummy_ids = np.ones((1, max_length), dtype=np.int64)
    dummy_mask = np.ones((1, max_length), dtype=np.int64)

    results = session.run(None, {
        "input_ids": dummy_ids,
        "attention_mask": dummy_mask,
    })

    score = results[0]
    tier_logits = results[1]
    domain_logits = results[2]
    intent_logits = results[3]

    print(f"  Score shape: {score.shape} (expected: (1,) or (1,1))")
    print(f"  Tier logits shape: {tier_logits.shape} (expected: (1, 5))")
    print(f"  Domain logits shape: {domain_logits.shape} (expected: (1, 5))")
    print(f"  Intent logits shape: {intent_logits.shape} (expected: (1, 8))")

    # Verify value ranges
    score_val = float(score.flatten()[0])
    print(f"  Sample score: {score_val:.4f} (should be 0.0-1.0)")

    if 0.0 <= score_val <= 1.0:
        print("  ✓ Verification passed!")
        return True
    else:
        print("  ✗ Score out of range!")
        return False


def main():
    parser = argparse.ArgumentParser(description="Export trained model to ONNX")
    parser.add_argument(
        "--model", type=str, required=True,
        help="Directory containing trained model (final_model.pt, config.json)",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output directory (default: same as model dir)",
    )
    parser.add_argument(
        "--weights", type=str, default="best_model.pt",
        help="Which weights file to export (best_model.pt or final_model.pt)",
    )
    parser.add_argument(
        "--no-quantize", action="store_true",
        help="Skip INT8 quantization",
    )
    parser.add_argument(
        "--install", action="store_true",
        help="Copy model to ~/.kuro/models/ for use by Kuro",
    )
    args = parser.parse_args()

    model_dir = Path(args.model)
    output_dir = Path(args.output) if args.output else model_dir

    # Load training config
    config_path = model_dir / "config.json"
    if not config_path.exists():
        print(f"Error: config.json not found in {model_dir}")
        return

    with open(config_path) as f:
        config = json.load(f)

    max_length = config.get("max_length", 256)
    freeze_layers = config.get("freeze_layers", 4)

    # Load model
    weights_path = model_dir / args.weights
    if not weights_path.exists():
        print(f"Error: {weights_path} not found")
        return

    print(f"Loading model from: {weights_path}")
    model = ComplexityModel(freeze_layers=freeze_layers)
    model.load_state_dict(torch.load(weights_path, map_location="cpu"))
    model.eval()

    # Export to ONNX
    output_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = output_dir / "complexity_model.onnx"
    export_to_onnx(model, onnx_path, max_length=max_length)

    # Quantize
    if not args.no_quantize:
        quantized_path = output_dir / "complexity_model_int8.onnx"
        quantize_onnx(onnx_path, quantized_path)

        # Verify quantized model
        verify_onnx(quantized_path, max_length)
    else:
        verify_onnx(onnx_path, max_length)

    # Install to ~/.kuro/models/ if requested
    if args.install:
        from src.config import get_kuro_home

        install_dir = get_kuro_home() / "models"
        install_dir.mkdir(parents=True, exist_ok=True)

        # Copy ONNX model
        src_model = output_dir / "complexity_model_int8.onnx"
        if not src_model.exists():
            src_model = onnx_path
        dst_model = install_dir / "complexity_model_int8.onnx"
        shutil.copy2(src_model, dst_model)
        print(f"\nInstalled model: {dst_model}")

        # Copy tokenizer
        src_tokenizer = model_dir / "tokenizer"
        dst_tokenizer = install_dir / "complexity_tokenizer"
        if src_tokenizer.exists():
            if dst_tokenizer.exists():
                shutil.rmtree(dst_tokenizer)
            shutil.copytree(src_tokenizer, dst_tokenizer)
            print(f"Installed tokenizer: {dst_tokenizer}")

        print(f"\n✓ Model installed to {install_dir}")
        print("Enable in config.yaml:")
        print("  task_complexity:")
        print("    ml_model_enabled: true")
        print("    ml_estimation_mode: hybrid")

    print("\nDone!")


if __name__ == "__main__":
    main()
