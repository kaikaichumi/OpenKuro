# ML Complexity Classifier Training Guide

> Fine-tuning DistilBERT for task complexity analysis - a complete step-by-step guide.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Step 1: Generate Training Data](#step-1-generate-training-data)
- [Step 2: Train the Model](#step-2-train-the-model)
- [Step 3: Export to ONNX](#step-3-export-to-onnx)
- [Step 4: Evaluate](#step-4-evaluate)
- [Step 5: Deploy](#step-5-deploy)
- [Training Tips](#training-tips)
- [Troubleshooting](#troubleshooting)
- [File Reference](#file-reference)

---

## Overview

Kuro includes an optional ML-based complexity classifier that runs locally on your machine.
It uses a **fine-tuned DistilBERT multilingual model** exported to ONNX format for fast CPU inference.

**Why fine-tuning instead of training from scratch?**

- DistilBERT is a pre-trained language model that already understands text structure
- Fine-tuning only adjusts the last few layers + custom output heads
- Requires far less data (thousands vs millions of samples)
- Achieves reasonable accuracy in minutes instead of days

**Model Specifications:**

| Property | Value |
|----------|-------|
| Base model | `distilbert-base-multilingual-cased` |
| Parameters | ~66M total, ~15M trainable |
| ONNX size (INT8) | ~65 MB |
| RAM usage | < 500 MB |
| Inference time | ~35 ms (CPU) |
| Languages | Multilingual (Chinese + English) |

## Architecture

The model has **4 output heads** sharing a single DistilBERT encoder:

```
Input Text
    │
    ▼
┌──────────────────────────────┐
│  DistilBERT Encoder          │
│  (6 layers, freeze first 4)  │
│  Embeddings: frozen          │
│  Layer 0-3: frozen           │
│  Layer 4-5: trainable        │
└──────────────┬───────────────┘
               │ [CLS] embedding (768-dim)
               ▼
┌──────────────────────────────┐
│  Shared Projection           │
│  Linear(768 → 512) + ReLU   │
└──────────────┬───────────────┘
               │
    ┌──────────┼──────────┬──────────┐
    ▼          ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
│ Score  │ │  Tier  │ │ Domain │ │ Intent │
│ Head   │ │  Head  │ │  Head  │ │  Head  │
│512→128 │ │512→128 │ │512→128 │ │512→128 │
│128→1   │ │128→5   │ │128→5   │ │128→8   │
│sigmoid │ │softmax │ │sigmoid │ │softmax │
└────────┘ └────────┘ └────────┘ └────────┘
    │          │          │          │
    ▼          ▼          ▼          ▼
  0.0-1.0   trivial    [code,     greeting
  score     simple      math,     question
            moderate    data,     code_gen
            complex     system,   analysis
            expert      creative] debug
                                  planning
                                  creative
                                  multi_step
```

**Loss Function (weighted multi-task):**

```
Total Loss = 0.4 × MSE(score)      # Score regression
           + 0.3 × CE(tier)        # Tier classification
           + 0.15 × BCE(domain)    # Domain multi-label
           + 0.15 × CE(intent)     # Intent classification
```

## Prerequisites

### Hardware

- **GPU (recommended):** NVIDIA GPU with 4GB+ VRAM (e.g., RTX 3060, RTX 4060)
- **CPU:** Works but training will be 5-10x slower
- **RAM:** 8 GB minimum

### Software

```bash
# Core dependencies
pip install torch transformers datasets scikit-learn

# For GPU training (NVIDIA CUDA)
pip install torch --index-url https://download.pytorch.org/whl/cu121

# For ONNX export
pip install onnx onnxruntime

# For inference only (no training)
pip install onnxruntime transformers
```

### Verify GPU Setup

```bash
python -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}'); print(f'GPU: {torch.cuda.get_device_name(0)}' if torch.cuda.is_available() else 'No GPU')"
```

---

## Step 1: Generate Training Data

The training data generator creates synthetic labeled samples using predefined templates
in both Chinese and English.

### Basic Usage

```bash
# Generate 5000 samples (default) as a single file
python -m training.generate_data --output training/data/synthetic.jsonl --count 5000

# Generate and auto-split into train/val/test (80/10/10)
python -m training.generate_data --output training/data/synthetic.jsonl --count 7000 --split --seed 42
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--output` / `-o` | `training/data/synthetic.jsonl` | Output file path |
| `--count` / `-n` | `5000` | Total number of samples |
| `--seed` / `-s` | `42` | Random seed for reproducibility |
| `--split` | `false` | Split into train/val/test |

### Data Format

Each sample is a JSON object with the following fields:

```json
{
  "text": "幫我寫一個 Python function 計算 fibonacci",
  "score": 0.2341,
  "tier": "simple",
  "domains": ["code"],
  "intent": "code_gen"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `text` | string | User message (Chinese or English) |
| `score` | float | Complexity score 0.0 - 1.0 |
| `tier` | string | `trivial` / `simple` / `moderate` / `complex` / `expert` |
| `domains` | list | Detected domains: `code`, `math`, `data`, `system`, `creative` |
| `intent` | string | Task intent (8 classes) |

### Score Ranges Per Tier

| Tier | Score Range | Example Tasks |
|------|------------|---------------|
| trivial | 0.00 - 0.12 | "Hello", "Thanks", "What time is it?" |
| simple | 0.15 - 0.32 | "Write a fibonacci function", "What is REST API?" |
| moderate | 0.38 - 0.58 | "Debug this crash with large datasets", "Design a caching layer" |
| complex | 0.62 - 0.82 | "Analyze architecture and find bottlenecks", "Build a data pipeline" |
| expert | 0.86 - 0.98 | "Design a distributed system with HA", "Build a complete SaaS product" |

### Recommended Sample Counts

| Quality Goal | Training Samples | Total (with split) |
|-------------|-----------------|-------------------|
| Quick test | 3,000 | ~3,750 |
| Baseline | 7,000 | ~8,750 |
| Good accuracy | 15,000 | ~18,750 |
| Best results | 30,000+ | ~37,500 |

### Customizing Templates

To add your own training data patterns, edit `training/generate_data.py` and modify
the `TEMPLATES` dictionary. Each tier contains a list of template dictionaries:

```python
TEMPLATES["moderate"].append({
    "text": lambda: f"Help me optimize this {random.choice(PROG_LANGS)} query for {random.choice(['MySQL', 'PostgreSQL', 'MongoDB'])}",
    "intent": "debug",
    "domains": ["code", "data"],
})
```

---

## Step 2: Train the Model

### Basic Training

```bash
# CPU training
python -m training.train \
  --data training/data/train.jsonl \
  --val training/data/val.jsonl \
  --epochs 5

# GPU training (recommended)
python -m training.train \
  --data training/data/train.jsonl \
  --val training/data/val.jsonl \
  --epochs 5 \
  --device cuda
```

### Recommended Configurations

**Quick test (verify pipeline works):**
```bash
python -m training.train \
  --data training/data/train.jsonl \
  --val training/data/val.jsonl \
  --epochs 3 \
  --lr 2e-5 \
  --device cuda
```

**Baseline training:**
```bash
python -m training.train \
  --data training/data/train.jsonl \
  --val training/data/val.jsonl \
  --epochs 5 \
  --lr 2e-5 \
  --freeze-layers 4 \
  --device cuda
```

**Better accuracy (more data + epochs + lower LR):**
```bash
# First generate more data
python -m training.generate_data -o training/data/synthetic.jsonl -n 15000 --split --seed 123

# Train with tuned hyperparameters
python -m training.train \
  --data training/data/train.jsonl \
  --val training/data/val.jsonl \
  --epochs 10 \
  --lr 1e-5 \
  --freeze-layers 3 \
  --batch-size 32 \
  --device cuda
```

### All Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--data` | (required) | Training data JSONL path |
| `--val` | `None` | Validation data JSONL path |
| `--output` / `-o` | `training/output` | Output directory |
| `--epochs` | `5` | Number of training epochs |
| `--batch-size` | `32` | Batch size (reduce if OOM) |
| `--lr` | `2e-5` | Base learning rate |
| `--freeze-layers` | `4` | Freeze first N of 6 transformer layers |
| `--max-length` | `256` | Max token length |
| `--device` | `auto` | `cpu` / `cuda` / `auto` |
| `--warmup-steps` | `100` | Learning rate warmup steps |

### Training Strategy Details

**Layer Freezing:**
- DistilBERT has 6 transformer layers (0-5) + embedding layer
- Default: freeze embeddings + layers 0-3, train layers 4-5 + classifier heads
- `--freeze-layers 3`: unfreeze more layers (better accuracy, slower training)
- `--freeze-layers 5`: only train last layer + heads (faster, less accurate)

**Differential Learning Rate:**
- Encoder (unfrozen layers): base LR (e.g., `2e-5`)
- Classifier heads: 5x base LR (e.g., `1e-4`)
- This lets classifier heads learn faster while encoder adjusts slowly

**Learning Rate Schedule:**
- Linear warmup for first N steps
- Cosine decay to 0 for remaining steps
- Gradient clipping at max_norm=1.0

### Output Files

After training, the `training/output/` directory contains:

```
training/output/
├── best_model.pt          # Best model (lowest validation MAE)
├── final_model.pt         # Final epoch model
├── checkpoint.pt          # Last checkpoint (resumable)
├── config.json            # Training hyperparameters
└── tokenizer/             # Saved tokenizer
    ├── tokenizer.json
    └── tokenizer_config.json
```

---

## Step 3: Export to ONNX

Convert the trained PyTorch model to ONNX format with INT8 quantization for fast CPU inference.

### Basic Export

```bash
# Export to ONNX + INT8 quantization
python -m training.export_onnx --model training/output

# Export and install to project models/ directory (ready to use)
python -m training.export_onnx --model training/output --install
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--model` | (required) | Directory with trained model files |
| `--output` / `-o` | same as model dir | Output directory |
| `--weights` | `best_model.pt` | Which weights to export |
| `--no-quantize` | `false` | Skip INT8 quantization |
| `--install` | `false` | Copy to project `models/` directory |

### What Happens During Export

1. **Load PyTorch model** from `best_model.pt` (or `final_model.pt`)
2. **Export to ONNX** with dynamic batch size support
3. **INT8 dynamic quantization** reduces size ~75% (260MB -> 65MB)
4. **Verification** runs a dummy inference to check output shapes
5. **(Optional) Install** copies model + tokenizer to project `models/` directory

### Output Files

```
training/output/
├── complexity_model.onnx         # Full precision ONNX (~260 MB)
├── complexity_model_int8.onnx    # INT8 quantized (~65 MB)
└── ...

models/                           # (with --install flag)
├── complexity_model_int8.onnx    # Model file
└── complexity_tokenizer/         # Tokenizer files
    ├── tokenizer.json
    └── tokenizer_config.json
```

---

## Step 4: Evaluate

### Evaluate ONNX Model

```bash
# Basic evaluation
python -m training.evaluate \
  --onnx training/output/complexity_model_int8.onnx \
  --data training/data/test.jsonl

# Evaluate installed model
python -m training.evaluate \
  --onnx models/complexity_model_int8.onnx \
  --data training/data/test.jsonl

# Compare ML vs heuristic baseline
python -m training.evaluate \
  --onnx models/complexity_model_int8.onnx \
  --data training/data/test.jsonl \
  --compare
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--data` | (required) | Test data JSONL path |
| `--onnx` | `None` | ONNX model path |
| `--model` | `None` | PyTorch model directory |
| `--tokenizer` | auto-detected | Tokenizer path |
| `--max-length` | `256` | Max token length |
| `--compare` | `false` | Compare with heuristic baseline |

### Understanding the Output

```
============================================================
  ONNX Model Evaluation Results
============================================================
  Total samples:     1500
  Avg inference:     35.2 ms
  Score MAE:         0.1716       ← Lower is better (0 = perfect)
  Score Max Error:   0.5842
  Tier Accuracy:     58.40%       ← Higher is better
  Intent Accuracy:   48.13%       ← Higher is better
  Domain F1:         56.17%       ← Higher is better

  Tier Confusion Matrix:
             |  trivial |   simple | moderate |  complex |   expert
  -----------+----------+----------+----------+----------+---------
     trivial |      180 |       40 |        5 |        0 |        0
      simple |       35 |      150 |       95 |       10 |        0
    moderate |        0 |       60 |      250 |      100 |       15
     complex |        0 |        5 |       80 |      170 |       45
      expert |        0 |        0 |       10 |       50 |      200
============================================================
```

**Key Metrics:**
- **Score MAE**: Mean Absolute Error of predicted score vs true score (target: < 0.10)
- **Tier Accuracy**: Exact tier match (target: > 70%)
- **Intent Accuracy**: Exact intent match (target: > 60%)
- **Domain F1**: F1 score for multi-label domain detection (target: > 65%)

**Confusion Matrix:** Read as "true tier (row) predicted as (column)". High diagonal = good.
Off-diagonal shows confusion patterns (e.g., simple often misclassified as moderate).

---

## Step 5: Deploy

### Enable in Web UI

1. Open Kuro Web UI → Settings → Task Complexity section
2. Toggle **ML Classifier** to enabled
3. Select **Estimation Mode**:
   - `hybrid` (recommended): 60% ML + 40% heuristic scoring
   - `ml_only`: Use ML model exclusively
   - `ml_refine`: ML only in ambiguous score zone (0.35-0.65)
4. **Model Path** and **Tokenizer Path** are auto-detected if you used `--install`
5. Settings take effect immediately (hot-reload, no restart needed)

### Enable in config.yaml

```yaml
task_complexity:
  enabled: true
  ml_model_enabled: true
  ml_estimation_mode: hybrid    # hybrid | ml_only | ml_refine
  # Optional: override default paths (relative to project root)
  # ml_model_path: models/complexity_model_int8.onnx
  # ml_tokenizer_path: models/complexity_tokenizer
```

### Verify It Works

Check Kuro logs for:
```
ml_classifier_loaded    model=models/complexity_model_int8.onnx  size_mb=65.2
ml_tokenizer_loaded     path=models/complexity_tokenizer
```

---

## Training Tips

### Improving Accuracy

1. **More training data** is the single biggest improvement factor:
   ```bash
   python -m training.generate_data -n 15000 --split --seed 123
   ```

2. **More epochs with lower learning rate**:
   ```bash
   python -m training.train --epochs 10 --lr 1e-5 --device cuda
   ```

3. **Unfreeze more layers** (train layers 3-5 instead of 4-5):
   ```bash
   python -m training.train --freeze-layers 3 --device cuda
   ```

4. **Add real-world examples** to training data:
   - Collect actual user queries from production logs
   - Manually label them with correct tier/score/intent/domains
   - Add to training JSONL files

5. **Address specific confusion patterns**:
   - Check the confusion matrix to identify problematic pairs
   - Add more distinguishing examples for confused tiers
   - Common issue: `trivial` ↔ `simple` and `complex` ↔ `moderate`

### Reducing Model Size

- Default INT8 quantized model is ~65 MB
- For even smaller size, try pruning or smaller max_length:
  ```bash
  python -m training.train --max-length 128  # Reduce from 256
  ```

### Training on CPU

If you don't have a GPU, training still works but is slower:

```bash
# Reduce batch size to avoid memory issues
python -m training.train \
  --data training/data/train.jsonl \
  --val training/data/val.jsonl \
  --epochs 5 \
  --batch-size 8 \
  --device cpu
```

Expected training times (5000 samples, 5 epochs):
- RTX 3060: ~5-10 minutes
- RTX 4090: ~2-3 minutes
- CPU (modern i7): ~30-60 minutes

---

## Troubleshooting

### CUDA Out of Memory

```
RuntimeError: CUDA out of memory
```

**Fix:** Reduce batch size:
```bash
python -m training.train --batch-size 16 --device cuda
# Or even smaller:
python -m training.train --batch-size 8 --device cuda
```

### ModuleNotFoundError: No module named 'structlog'

```bash
pip install structlog
```

### UnicodeDecodeError (Windows)

If you see `cp950 codec can't decode` on Windows, ensure all files are opened with
`encoding='utf-8'`. The training scripts already handle this.

### ONNX Export Fails

```bash
# Make sure ONNX and ONNX Runtime are installed
pip install onnx onnxruntime

# If quantization fails, try exporting without it first
python -m training.export_onnx --model training/output --no-quantize
```

### Model Not Loading in Kuro

1. Check the model file exists (from project root):
   ```bash
   ls -la models/complexity_model_int8.onnx
   ls -la models/complexity_tokenizer/
   ```

2. Check `onnxruntime` is installed:
   ```bash
   python -c "import onnxruntime; print(onnxruntime.__version__)"
   ```

3. Check `transformers` is installed (needed for tokenizer):
   ```bash
   python -c "from transformers import AutoTokenizer; print('OK')"
   ```

4. Check Kuro logs for error messages:
   ```
   ml_classifier_unavailable  reason=...
   ml_classifier_load_failed  error=...
   ```

---

## File Reference

### Training Scripts (local only, not in git)

| File | Description |
|------|-------------|
| `training/__init__.py` | Module docstring with quick-start commands |
| `training/generate_data.py` | Synthetic training data generator |
| `training/train.py` | Multi-head DistilBERT training script |
| `training/export_onnx.py` | ONNX export + INT8 quantization |
| `training/evaluate.py` | Model evaluation + confusion matrix |

### Generated Files (local only)

| File | Description |
|------|-------------|
| `training/data/train.jsonl` | Training split (80%) |
| `training/data/val.jsonl` | Validation split (10%) |
| `training/data/test.jsonl` | Test split (10%) |
| `training/output/best_model.pt` | Best model weights |
| `training/output/final_model.pt` | Final epoch weights |
| `training/output/config.json` | Training config |
| `training/output/tokenizer/` | Saved tokenizer files |
| `training/output/complexity_model.onnx` | Full-precision ONNX |
| `training/output/complexity_model_int8.onnx` | INT8 quantized ONNX |

### Inference Code (in git)

| File | Description |
|------|-------------|
| `src/core/complexity_ml.py` | ONNX inference classifier |
| `src/core/complexity.py` | Integration with heuristic estimator |
| `src/config.py` | ML config fields |

### Deployed Model Files (in project root)

| File | Description |
|------|-------------|
| `models/complexity_model_int8.onnx` | Production model |
| `models/complexity_tokenizer/` | Production tokenizer |

---

## Quick Reference

```bash
# Full pipeline (copy-paste ready)

# 1. Generate data
python -m training.generate_data -o training/data/synthetic.jsonl -n 15000 --split --seed 42

# 2. Train
python -m training.train \
  --data training/data/train.jsonl \
  --val training/data/val.jsonl \
  --epochs 10 --lr 1e-5 --freeze-layers 3 --device cuda

# 3. Export + Install
python -m training.export_onnx --model training/output --install

# 4. Evaluate
python -m training.evaluate \
  --onnx models/complexity_model_int8.onnx \
  --data training/data/test.jsonl --compare
```
