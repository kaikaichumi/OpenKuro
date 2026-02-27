"""Generate synthetic training data for the complexity classifier.

Produces labeled samples in JSONL format with the following fields:
- text: User message (str)
- score: Complexity score 0.0-1.0 (float)
- tier: "trivial"|"simple"|"moderate"|"complex"|"expert" (str)
- domains: List of detected domains (list[str])
- intent: Task intent classification (str)

Usage:
    python -m training.generate_data --output training/data/synthetic.jsonl --count 5000
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

# ── Template Variables ────────────────────────────────────────────────────

GREETINGS_ZH = ["你好", "嗨", "哈囉", "早安", "午安", "晚安", "Hey"]
GREETINGS_EN = ["Hello", "Hi", "Hey", "Good morning", "Good evening", "Howdy"]
NAMES = ["John", "Alice", "小明", "美玲", "Bob", "David", "小華"]

LANGUAGES_ZH = ["英文", "日文", "法文", "韓文", "德文", "西班牙文"]
LANGUAGES_EN = ["English", "Japanese", "French", "Korean", "German", "Spanish"]

PROG_LANGS = ["Python", "JavaScript", "TypeScript", "Go", "Rust", "Java", "C++"]
SIMPLE_TASKS = [
    "fibonacci", "factorial", "reverse string", "sort array", "binary search",
    "palindrome check", "FizzBuzz", "max element", "count words",
]

CONCEPTS_ZH = ["機器學習", "區塊鏈", "微服務", "容器化", "CI/CD", "REST API"]
CONCEPTS_EN = ["machine learning", "blockchain", "microservices", "containerization",
               "CI/CD", "REST API", "WebSocket", "GraphQL"]

FILENAMES = ["main.py", "config.yaml", "index.html", "README.md", "app.js", "server.go"]

DEBUG_ACTIONS_ZH = ["除錯", "修復", "找出問題", "分析錯誤"]
DEBUG_ACTIONS_EN = ["debug", "fix", "troubleshoot", "diagnose"]

ERRORS_ZH = ["崩潰", "回傳錯誤", "執行很慢", "記憶體洩漏", "無限迴圈"]
ERRORS_EN = ["crash", "return wrong result", "run slowly", "memory leak", "infinite loop"]

COMPONENTS_ZH = ["使用者認證系統", "快取層", "訊息佇列", "搜尋引擎", "推薦系統"]
COMPONENTS_EN = ["auth system", "caching layer", "message queue", "search engine", "recommendation system"]

FEATURES_ZH = ["分頁", "排序", "篩選", "匯出", "多語言", "深色模式", "即時同步"]
FEATURES_EN = ["pagination", "sorting", "filtering", "export", "i18n", "dark mode", "real-time sync"]

CODEBASE_TYPES = ["monorepo", "microservice", "Django project", "Next.js app", "FastAPI backend"]
SYSTEM_TYPES = ["message queue", "API gateway", "task scheduler", "notification system"]

# ── Templates ─────────────────────────────────────────────────────────────

TEMPLATES: dict[str, list[dict]] = {
    "trivial": [
        {"text": lambda: random.choice(GREETINGS_ZH), "intent": "greeting", "domains": []},
        {"text": lambda: random.choice(GREETINGS_EN), "intent": "greeting", "domains": []},
        {"text": lambda: f"{random.choice(GREETINGS_ZH)}，{random.choice(NAMES)}", "intent": "greeting", "domains": []},
        {"text": lambda: f"{random.choice(GREETINGS_EN)} {random.choice(NAMES)}", "intent": "greeting", "domains": []},
        {"text": lambda: random.choice(["謝謝", "Thank you", "Thanks!", "好的", "OK", "了解", "Got it"]), "intent": "greeting", "domains": []},
        {"text": lambda: random.choice(["現在幾點？", "What time is it?", "今天星期幾？", "What day is it?"]), "intent": "question", "domains": []},
        {"text": lambda: random.choice(["你是誰？", "Who are you?", "你能做什麼？", "What can you do?"]), "intent": "question", "domains": []},
    ],
    "simple": [
        {
            "text": lambda: f"幫我寫一個 {random.choice(PROG_LANGS)} function 計算 {random.choice(SIMPLE_TASKS)}",
            "intent": "code_gen", "domains": ["code"],
        },
        {
            "text": lambda: f"Write a {random.choice(PROG_LANGS)} function for {random.choice(SIMPLE_TASKS)}",
            "intent": "code_gen", "domains": ["code"],
        },
        {
            "text": lambda: f"什麼是 {random.choice(CONCEPTS_ZH)}？",
            "intent": "question", "domains": [],
        },
        {
            "text": lambda: f"What is {random.choice(CONCEPTS_EN)}?",
            "intent": "question", "domains": [],
        },
        {
            "text": lambda: f"幫我讀取 {random.choice(FILENAMES)} 的內容",
            "intent": "question", "domains": ["system"],
        },
        {
            "text": lambda: f"Read the file {random.choice(FILENAMES)}",
            "intent": "question", "domains": ["system"],
        },
        {
            "text": lambda: f"把這段話翻譯成{random.choice(LANGUAGES_ZH)}",
            "intent": "creative", "domains": ["creative"],
        },
        {
            "text": lambda: f"Translate this to {random.choice(LANGUAGES_EN)}",
            "intent": "creative", "domains": ["creative"],
        },
        {
            "text": lambda: f"Summarize this article in 3 sentences",
            "intent": "creative", "domains": ["creative"],
        },
    ],
    "moderate": [
        {
            "text": lambda: (
                f"幫我{random.choice(DEBUG_ACTIONS_ZH)}這個 {random.choice(PROG_LANGS)} 程式，"
                f"它在處理大量資料時會{random.choice(ERRORS_ZH)}"
            ),
            "intent": "debug", "domains": ["code"],
        },
        {
            "text": lambda: (
                f"{random.choice(DEBUG_ACTIONS_EN).capitalize()} this {random.choice(PROG_LANGS)} code, "
                f"it will {random.choice(ERRORS_EN)} when processing large datasets"
            ),
            "intent": "debug", "domains": ["code"],
        },
        {
            "text": lambda: (
                f"Compare {random.choice(CONCEPTS_EN)} and {random.choice(CONCEPTS_EN)} "
                f"for a {random.choice(['startup', 'enterprise', 'personal project', 'team of 5'])}"
            ),
            "intent": "analysis", "domains": [],
        },
        {
            "text": lambda: (
                f"設計一個{random.choice(COMPONENTS_ZH)}，"
                f"要支援{random.choice(FEATURES_ZH)}和{random.choice(FEATURES_ZH)}"
            ),
            "intent": "planning", "domains": ["code"],
        },
        {
            "text": lambda: (
                f"Design a {random.choice(COMPONENTS_EN)} that supports "
                f"{random.choice(FEATURES_EN)} and {random.choice(FEATURES_EN)}"
            ),
            "intent": "planning", "domains": ["code"],
        },
        {
            "text": lambda: (
                f"Explain how {random.choice(CONCEPTS_EN)} works "
                f"with examples in {random.choice(PROG_LANGS)}"
            ),
            "intent": "question", "domains": ["code"],
        },
        {
            "text": lambda: (
                f"Write a SQL query to find the top 10 customers by revenue, "
                f"joining with the orders and products tables"
            ),
            "intent": "code_gen", "domains": ["code", "data"],
        },
    ],
    "complex": [
        {
            "text": lambda: (
                f"分析這個 {random.choice(CODEBASE_TYPES)} 的架構，"
                f"找出效能瓶頸，然後提出 3 種改善方案並比較 trade-off"
            ),
            "intent": "analysis", "domains": ["code", "system"],
        },
        {
            "text": lambda: (
                f"Design a {random.choice(SYSTEM_TYPES)} that must handle "
                f"{random.choice(FEATURES_EN)}, {random.choice(FEATURES_EN)}, "
                f"and {random.choice(FEATURES_EN)}. Include error handling and monitoring."
            ),
            "intent": "planning", "domains": ["code", "system"],
        },
        {
            "text": lambda: (
                f"重構 {random.choice(COMPONENTS_ZH)} 模組，需要保持向後相容，"
                f"並且要加入{random.choice(FEATURES_ZH)}、{random.choice(FEATURES_ZH)}和{random.choice(FEATURES_ZH)}"
            ),
            "intent": "planning", "domains": ["code"],
        },
        {
            "text": lambda: (
                f"Write a comprehensive data pipeline that: "
                f"1. Ingests CSV data from S3 "
                f"2. Validates and transforms with pandas "
                f"3. Loads into PostgreSQL "
                f"4. Generates summary statistics"
            ),
            "intent": "multi_step", "domains": ["code", "data", "system"],
        },
        {
            "text": lambda: (
                f"Analyze the performance of our {random.choice(PROG_LANGS)} application, "
                f"profile memory usage, identify bottlenecks, and suggest optimizations. "
                f"Consider both time and space complexity."
            ),
            "intent": "analysis", "domains": ["code"],
        },
    ],
    "expert": [
        {
            "text": lambda: (
                f"分析整個 {random.choice(CODEBASE_TYPES)} 的架構，"
                f"比較 3 種方案的 trade-off，然後設計一個包含"
                f"{random.choice(COMPONENTS_ZH)}、{random.choice(COMPONENTS_ZH)}、"
                f"{random.choice(COMPONENTS_ZH)}的完整系統，"
                f"需要考慮效能、安全性和可擴展性"
            ),
            "intent": "multi_step", "domains": ["code", "system", "data"],
        },
        {
            "text": lambda: (
                f"Build a distributed {random.choice(SYSTEM_TYPES)} with "
                f"{random.choice(FEATURES_EN)}, {random.choice(FEATURES_EN)}, "
                f"and {random.choice(FEATURES_EN)}. "
                f"Ensure high availability and fault tolerance. "
                f"Include monitoring, testing, and deployment strategy. "
                f"The system must handle 10K requests/sec."
            ),
            "intent": "multi_step", "domains": ["code", "system", "data"],
        },
        {
            "text": lambda: (
                f"Design a complete machine learning pipeline: "
                f"1. Data collection and preprocessing "
                f"2. Feature engineering with domain expertise "
                f"3. Model selection and hyperparameter tuning "
                f"4. A/B testing framework "
                f"5. Deployment with CI/CD "
                f"6. Monitoring and drift detection"
            ),
            "intent": "multi_step", "domains": ["code", "data", "math", "system"],
        },
        {
            "text": lambda: (
                f"我需要你幫我從零開始建立一個完整的 SaaS 產品，包括："
                f"前端（React + TypeScript）、後端（{random.choice(PROG_LANGS)}）、"
                f"資料庫設計、認證系統、付款整合、"
                f"CI/CD pipeline、和 Kubernetes 部署方案"
            ),
            "intent": "multi_step", "domains": ["code", "system", "data", "creative"],
        },
    ],
}

# Score ranges for each tier
TIER_SCORE_RANGES = {
    "trivial": (0.0, 0.12),
    "simple": (0.15, 0.32),
    "moderate": (0.38, 0.58),
    "complex": (0.62, 0.82),
    "expert": (0.86, 0.98),
}


def generate_sample(tier: str) -> dict:
    """Generate a single training sample for the given tier."""
    templates = TEMPLATES[tier]
    template = random.choice(templates)

    text = template["text"]()
    score_low, score_high = TIER_SCORE_RANGES[tier]
    score = round(random.uniform(score_low, score_high), 4)

    return {
        "text": text,
        "score": score,
        "tier": tier,
        "domains": template["domains"],
        "intent": template["intent"],
    }


def generate_dataset(
    count: int = 5000,
    tier_weights: dict[str, float] | None = None,
) -> list[dict]:
    """Generate a balanced synthetic training dataset.

    Args:
        count: Total number of samples to generate.
        tier_weights: Weight distribution per tier (default: balanced with
                     more moderate examples since they're most common).
    """
    if tier_weights is None:
        tier_weights = {
            "trivial": 0.15,
            "simple": 0.20,
            "moderate": 0.30,
            "complex": 0.20,
            "expert": 0.15,
        }

    dataset = []
    for tier, weight in tier_weights.items():
        tier_count = int(count * weight)
        for _ in range(tier_count):
            dataset.append(generate_sample(tier))

    # Fill remaining if rounding caused shortfall
    while len(dataset) < count:
        tier = random.choice(list(tier_weights.keys()))
        dataset.append(generate_sample(tier))

    random.shuffle(dataset)
    return dataset


def save_jsonl(data: list[dict], output_path: Path) -> None:
    """Save dataset as JSONL (one JSON per line)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for item in data:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Generate synthetic training data")
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="training/data/synthetic.jsonl",
        help="Output JSONL file path",
    )
    parser.add_argument(
        "--count", "-n",
        type=int,
        default=5000,
        help="Number of samples to generate",
    )
    parser.add_argument(
        "--seed", "-s",
        type=int,
        default=42,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help="Split into train/val/test (80/10/10)",
    )
    args = parser.parse_args()

    random.seed(args.seed)

    print(f"Generating {args.count} samples...")
    dataset = generate_dataset(args.count)

    if args.split:
        # Split into train/val/test
        n = len(dataset)
        train_end = int(n * 0.8)
        val_end = int(n * 0.9)

        train = dataset[:train_end]
        val = dataset[train_end:val_end]
        test = dataset[val_end:]

        output_dir = Path(args.output).parent
        save_jsonl(train, output_dir / "train.jsonl")
        save_jsonl(val, output_dir / "val.jsonl")
        save_jsonl(test, output_dir / "test.jsonl")

        print(f"Saved: train={len(train)}, val={len(val)}, test={len(test)}")
        print(f"Output dir: {output_dir}")
    else:
        output_path = Path(args.output)
        save_jsonl(dataset, output_path)
        print(f"Saved {len(dataset)} samples to {output_path}")

    # Print distribution
    tier_counts = {}
    intent_counts = {}
    for item in dataset:
        tier_counts[item["tier"]] = tier_counts.get(item["tier"], 0) + 1
        intent_counts[item["intent"]] = intent_counts.get(item["intent"], 0) + 1

    print("\nTier distribution:")
    for tier in ["trivial", "simple", "moderate", "complex", "expert"]:
        print(f"  {tier}: {tier_counts.get(tier, 0)}")

    print("\nIntent distribution:")
    for intent, count in sorted(intent_counts.items(), key=lambda x: -x[1]):
        print(f"  {intent}: {count}")


if __name__ == "__main__":
    main()
