"""Train the multi-head DistilBERT complexity classifier.

Fine-tunes distilbert-base-multilingual-cased with 4 output heads:
1. Score (regression): 0.0-1.0
2. Tier (5-class): trivial/simple/moderate/complex/expert
3. Domain (multi-label, 5): code/math/data/system/creative
4. Intent (8-class): greeting/question/code_gen/analysis/debug/planning/creative/multi_step

Prerequisites:
    pip install torch transformers datasets scikit-learn

Usage:
    python -m training.train --data training/data/train.jsonl --val training/data/val.jsonl --epochs 5
    python -m training.train --data training/data/train.jsonl --val training/data/val.jsonl --epochs 5 --device cuda
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

# ── Constants ─────────────────────────────────────────────────────────────

TIER_NAMES = ["trivial", "simple", "moderate", "complex", "expert"]
DOMAIN_NAMES = ["code", "math", "data", "system", "creative"]
INTENT_NAMES = [
    "greeting", "question", "code_gen", "analysis",
    "debug", "planning", "creative", "multi_step",
]

NUM_TIERS = len(TIER_NAMES)
NUM_DOMAINS = len(DOMAIN_NAMES)
NUM_INTENTS = len(INTENT_NAMES)

# Label to index mappings
TIER_TO_IDX = {name: i for i, name in enumerate(TIER_NAMES)}
INTENT_TO_IDX = {name: i for i, name in enumerate(INTENT_NAMES)}
DOMAIN_TO_IDX = {name: i for i, name in enumerate(DOMAIN_NAMES)}

BASE_MODEL = "distilbert-base-multilingual-cased"


# ── Multi-Head Classifier ─────────────────────────────────────────────────


class MultiHeadClassifier(nn.Module):
    """Multi-head classifier on top of DistilBERT [CLS] embedding.

    4 output heads:
    - score_head: Linear(768, 1) → regression (sigmoid)
    - tier_head: Linear(768, 5) → classification (softmax)
    - domain_head: Linear(768, 5) → multi-label (sigmoid)
    - intent_head: Linear(768, 8) → classification (softmax)
    """

    def __init__(self, input_dim: int = 768, dropout: float = 0.1):
        super().__init__()
        self.dropout = nn.Dropout(dropout)

        # Shared projection layer
        self.shared = nn.Sequential(
            nn.Linear(input_dim, 512),
            nn.ReLU(),
            nn.Dropout(dropout),
        )

        # Head 1: Score regression
        self.score_head = nn.Sequential(
            nn.Linear(512, 128),
            nn.ReLU(),
            nn.Linear(128, 1),
            nn.Sigmoid(),  # Output 0.0-1.0
        )

        # Head 2: Tier classification (5 classes)
        self.tier_head = nn.Sequential(
            nn.Linear(512, 128),
            nn.ReLU(),
            nn.Linear(128, NUM_TIERS),
        )

        # Head 3: Domain multi-label (5 labels)
        self.domain_head = nn.Sequential(
            nn.Linear(512, 128),
            nn.ReLU(),
            nn.Linear(128, NUM_DOMAINS),
        )

        # Head 4: Intent classification (8 classes)
        self.intent_head = nn.Sequential(
            nn.Linear(512, 128),
            nn.ReLU(),
            nn.Linear(128, NUM_INTENTS),
        )

    def forward(self, cls_embedding: torch.Tensor):
        """Forward pass through all heads.

        Args:
            cls_embedding: [CLS] token embedding, shape (batch, 768)

        Returns:
            Tuple of (score, tier_logits, domain_logits, intent_logits)
        """
        x = self.dropout(cls_embedding)
        shared = self.shared(x)

        score = self.score_head(shared).squeeze(-1)       # (batch,)
        tier_logits = self.tier_head(shared)               # (batch, 5)
        domain_logits = self.domain_head(shared)           # (batch, 5)
        intent_logits = self.intent_head(shared)           # (batch, 8)

        return score, tier_logits, domain_logits, intent_logits


class ComplexityModel(nn.Module):
    """Full model: DistilBERT encoder + multi-head classifier."""

    def __init__(self, freeze_layers: int = 4):
        super().__init__()
        from transformers import DistilBertModel

        self.distilbert = DistilBertModel.from_pretrained(BASE_MODEL)
        self.classifier = MultiHeadClassifier(
            input_dim=self.distilbert.config.dim,
        )

        # Freeze early transformer layers (keep last N unfrozen)
        total_layers = len(self.distilbert.transformer.layer)
        for i, layer in enumerate(self.distilbert.transformer.layer):
            if i < freeze_layers:
                for param in layer.parameters():
                    param.requires_grad = False

        # Also freeze embeddings
        for param in self.distilbert.embeddings.parameters():
            param.requires_grad = False

        trainable = sum(p.numel() for p in self.parameters() if p.requires_grad)
        total = sum(p.numel() for p in self.parameters())
        print(f"Trainable parameters: {trainable:,} / {total:,} ({trainable/total*100:.1f}%)")

    def forward(self, input_ids, attention_mask):
        """Forward pass through encoder + classifier heads."""
        outputs = self.distilbert(input_ids=input_ids, attention_mask=attention_mask)
        cls_embedding = outputs.last_hidden_state[:, 0, :]  # [CLS] token
        return self.classifier(cls_embedding)


# ── Dataset ───────────────────────────────────────────────────────────────


class ComplexityDataset(Dataset):
    """PyTorch dataset for complexity training data."""

    def __init__(self, data_path: str, tokenizer, max_length: int = 256):
        from transformers import AutoTokenizer

        self.samples = []
        with open(data_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.samples.append(json.loads(line))

        self.tokenizer = tokenizer
        self.max_length = max_length

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        sample = self.samples[idx]

        # Tokenize text
        encoding = self.tokenizer(
            sample["text"],
            max_length=self.max_length,
            truncation=True,
            padding="max_length",
            return_tensors="pt",
        )

        # Labels
        score = float(sample["score"])
        tier = TIER_TO_IDX.get(sample["tier"], 2)  # default: moderate
        intent = INTENT_TO_IDX.get(sample["intent"], 1)  # default: question

        # Domain multi-label
        domain_labels = torch.zeros(NUM_DOMAINS, dtype=torch.float32)
        for d in sample.get("domains", []):
            if d in DOMAIN_TO_IDX:
                domain_labels[DOMAIN_TO_IDX[d]] = 1.0

        return {
            "input_ids": encoding["input_ids"].squeeze(0),
            "attention_mask": encoding["attention_mask"].squeeze(0),
            "score": torch.tensor(score, dtype=torch.float32),
            "tier": torch.tensor(tier, dtype=torch.long),
            "domain": domain_labels,
            "intent": torch.tensor(intent, dtype=torch.long),
        }


# ── Training Loop ─────────────────────────────────────────────────────────


def train_epoch(
    model: ComplexityModel,
    dataloader: DataLoader,
    optimizer: torch.optim.Optimizer,
    device: torch.device,
    loss_weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Train for one epoch. Returns average losses."""
    if loss_weights is None:
        loss_weights = {
            "score": 0.4,
            "tier": 0.3,
            "domain": 0.15,
            "intent": 0.15,
        }

    model.train()
    total_loss = 0.0
    loss_components = {"score": 0, "tier": 0, "domain": 0, "intent": 0}
    n_batches = 0

    mse_fn = nn.MSELoss()
    ce_fn = nn.CrossEntropyLoss()
    bce_fn = nn.BCEWithLogitsLoss()

    for batch in dataloader:
        input_ids = batch["input_ids"].to(device)
        attention_mask = batch["attention_mask"].to(device)
        score_true = batch["score"].to(device)
        tier_true = batch["tier"].to(device)
        domain_true = batch["domain"].to(device)
        intent_true = batch["intent"].to(device)

        optimizer.zero_grad()

        score_pred, tier_logits, domain_logits, intent_logits = model(
            input_ids, attention_mask,
        )

        # Compute losses
        score_loss = mse_fn(score_pred, score_true)
        tier_loss = ce_fn(tier_logits, tier_true)
        domain_loss = bce_fn(domain_logits, domain_true)
        intent_loss = ce_fn(intent_logits, intent_true)

        # Weighted total
        loss = (
            loss_weights["score"] * score_loss
            + loss_weights["tier"] * tier_loss
            + loss_weights["domain"] * domain_loss
            + loss_weights["intent"] * intent_loss
        )

        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()

        total_loss += loss.item()
        loss_components["score"] += score_loss.item()
        loss_components["tier"] += tier_loss.item()
        loss_components["domain"] += domain_loss.item()
        loss_components["intent"] += intent_loss.item()
        n_batches += 1

    return {
        "total": total_loss / max(n_batches, 1),
        "score": loss_components["score"] / max(n_batches, 1),
        "tier": loss_components["tier"] / max(n_batches, 1),
        "domain": loss_components["domain"] / max(n_batches, 1),
        "intent": loss_components["intent"] / max(n_batches, 1),
    }


@torch.no_grad()
def evaluate(
    model: ComplexityModel,
    dataloader: DataLoader,
    device: torch.device,
) -> dict[str, float]:
    """Evaluate model on a dataset. Returns metrics."""
    model.eval()

    score_errors = []
    tier_correct = 0
    intent_correct = 0
    domain_tp = 0
    domain_fp = 0
    domain_fn = 0
    total = 0

    for batch in dataloader:
        input_ids = batch["input_ids"].to(device)
        attention_mask = batch["attention_mask"].to(device)
        score_true = batch["score"].to(device)
        tier_true = batch["tier"].to(device)
        domain_true = batch["domain"].to(device)
        intent_true = batch["intent"].to(device)

        score_pred, tier_logits, domain_logits, intent_logits = model(
            input_ids, attention_mask,
        )

        bs = score_true.size(0)
        total += bs

        # Score MAE
        score_errors.extend((score_pred - score_true).abs().cpu().tolist())

        # Tier accuracy
        tier_pred = tier_logits.argmax(dim=-1)
        tier_correct += (tier_pred == tier_true).sum().item()

        # Intent accuracy
        intent_pred = intent_logits.argmax(dim=-1)
        intent_correct += (intent_pred == intent_true).sum().item()

        # Domain F1 components
        domain_pred = (torch.sigmoid(domain_logits) > 0.5).float()
        domain_tp += (domain_pred * domain_true).sum().item()
        domain_fp += (domain_pred * (1 - domain_true)).sum().item()
        domain_fn += ((1 - domain_pred) * domain_true).sum().item()

    # Compute metrics
    score_mae = sum(score_errors) / max(len(score_errors), 1)
    tier_acc = tier_correct / max(total, 1)
    intent_acc = intent_correct / max(total, 1)

    precision = domain_tp / max(domain_tp + domain_fp, 1)
    recall = domain_tp / max(domain_tp + domain_fn, 1)
    domain_f1 = 2 * precision * recall / max(precision + recall, 1e-8)

    return {
        "score_mae": score_mae,
        "tier_accuracy": tier_acc,
        "intent_accuracy": intent_acc,
        "domain_f1": domain_f1,
        "domain_precision": precision,
        "domain_recall": recall,
        "total_samples": total,
    }


def main():
    parser = argparse.ArgumentParser(description="Train complexity classifier")
    parser.add_argument("--data", type=str, required=True, help="Training data JSONL")
    parser.add_argument("--val", type=str, default=None, help="Validation data JSONL")
    parser.add_argument("--output", "-o", type=str, default="training/output", help="Output directory")
    parser.add_argument("--epochs", type=int, default=5, help="Number of training epochs")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size")
    parser.add_argument("--lr", type=float, default=2e-5, help="Learning rate")
    parser.add_argument("--freeze-layers", type=int, default=4, help="Freeze first N transformer layers")
    parser.add_argument("--max-length", type=int, default=256, help="Max token length")
    parser.add_argument("--device", type=str, default="auto", help="Device: cpu/cuda/auto")
    parser.add_argument("--warmup-steps", type=int, default=100, help="Warmup steps")
    args = parser.parse_args()

    # Setup device
    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)
    print(f"Using device: {device}")

    # Load tokenizer
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)

    # Load datasets
    print(f"Loading training data: {args.data}")
    train_dataset = ComplexityDataset(args.data, tokenizer, args.max_length)
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=0,
        pin_memory=(device.type == "cuda"),
    )
    print(f"Training samples: {len(train_dataset)}")

    val_loader = None
    if args.val:
        print(f"Loading validation data: {args.val}")
        val_dataset = ComplexityDataset(args.val, tokenizer, args.max_length)
        val_loader = DataLoader(
            val_dataset,
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=0,
        )
        print(f"Validation samples: {len(val_dataset)}")

    # Build model
    print(f"\nInitializing model (freeze first {args.freeze_layers} layers)...")
    model = ComplexityModel(freeze_layers=args.freeze_layers)
    model.to(device)

    # Optimizer: different LR for encoder vs classifier
    encoder_params = [p for n, p in model.named_parameters() if "distilbert" in n and p.requires_grad]
    classifier_params = [p for n, p in model.named_parameters() if "classifier" in n]

    optimizer = torch.optim.AdamW([
        {"params": encoder_params, "lr": args.lr},
        {"params": classifier_params, "lr": args.lr * 5},  # 5x LR for classifier heads
    ], weight_decay=0.01)

    # Linear warmup + cosine decay scheduler
    total_steps = len(train_loader) * args.epochs
    warmup_steps = min(args.warmup_steps, total_steps // 5)

    def lr_lambda(step):
        if step < warmup_steps:
            return step / max(warmup_steps, 1)
        progress = (step - warmup_steps) / max(total_steps - warmup_steps, 1)
        return 0.5 * (1.0 + math.cos(math.pi * progress))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)

    # Training loop
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    best_val_score = float("inf")

    print(f"\n{'='*60}")
    print(f"Training for {args.epochs} epochs")
    print(f"{'='*60}\n")

    for epoch in range(args.epochs):
        # Train
        losses = train_epoch(model, train_loader, optimizer, device)
        scheduler.step()

        print(
            f"Epoch {epoch+1}/{args.epochs} | "
            f"Loss: {losses['total']:.4f} "
            f"(score: {losses['score']:.4f}, tier: {losses['tier']:.4f}, "
            f"domain: {losses['domain']:.4f}, intent: {losses['intent']:.4f})"
        )

        # Validate
        if val_loader:
            metrics = evaluate(model, val_loader, device)
            print(
                f"  Val → Score MAE: {metrics['score_mae']:.4f} | "
                f"Tier Acc: {metrics['tier_accuracy']:.2%} | "
                f"Intent Acc: {metrics['intent_accuracy']:.2%} | "
                f"Domain F1: {metrics['domain_f1']:.2%}"
            )

            # Save best model
            if metrics["score_mae"] < best_val_score:
                best_val_score = metrics["score_mae"]
                torch.save(model.state_dict(), output_dir / "best_model.pt")
                print(f"  ★ New best model saved (MAE: {best_val_score:.4f})")

        # Save checkpoint
        torch.save({
            "epoch": epoch,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "losses": losses,
        }, output_dir / "checkpoint.pt")

    # Save final model
    torch.save(model.state_dict(), output_dir / "final_model.pt")

    # Save tokenizer
    tokenizer.save_pretrained(str(output_dir / "tokenizer"))

    # Save training config
    config = {
        "base_model": BASE_MODEL,
        "freeze_layers": args.freeze_layers,
        "max_length": args.max_length,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "lr": args.lr,
        "tier_names": TIER_NAMES,
        "domain_names": DOMAIN_NAMES,
        "intent_names": INTENT_NAMES,
    }
    with open(output_dir / "config.json", "w") as f:
        json.dump(config, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Training complete!")
    print(f"Output directory: {output_dir}")
    print(f"Files: final_model.pt, best_model.pt, tokenizer/, config.json")
    print(f"\nNext step: python -m training.export_onnx --model {output_dir}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
