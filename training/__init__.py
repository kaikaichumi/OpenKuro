"""Training scripts for the Kuro complexity classifier.

This package contains tools for generating training data, training
a fine-tuned DistilBERT model, exporting to ONNX, and evaluation.

Usage:
    # Step 1: Generate synthetic training data
    python -m training.generate_data --output training/data/synthetic.jsonl --count 5000 --split

    # Step 2: Train the model
    python -m training.train --data training/data/train.jsonl --val training/data/val.jsonl --epochs 5

    # Step 3: Export to ONNX with INT8 quantization
    python -m training.export_onnx --model training/output --install

    # Step 4: Evaluate
    python -m training.evaluate --onnx ~/.kuro/models/complexity_model_int8.onnx --data training/data/test.jsonl
"""
