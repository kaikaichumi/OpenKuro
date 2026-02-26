"""Experience learning engine: analyze action logs and learn from patterns.

Capabilities:
1. Error pattern recognition — identify recurring tool failures
2. Tool usage optimization — find slow or redundant tool sequences
3. Model performance tracking — track which models work best for which tasks
4. Lesson generation — create actionable "lessons learned" for context injection
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from src.config import LearningConfig, get_kuro_home

logger = structlog.get_logger()


class LearningEngine:
    """Learns from action logs and provides context-aware lessons."""

    def __init__(self, config: LearningConfig) -> None:
        self.config = config
        self._lessons_path = get_kuro_home() / "memory" / "lessons.json"
        self._model_stats_path = get_kuro_home() / "memory" / "model_stats.json"
        self._lessons: list[dict[str, Any]] = []
        self._model_stats: dict[str, Any] = {}

        self._load()

    def _load(self) -> None:
        """Load lessons and model stats from disk."""
        if self._lessons_path.exists():
            try:
                data = json.loads(self._lessons_path.read_text(encoding="utf-8"))
                self._lessons = data.get("lessons", [])
            except Exception:
                self._lessons = []

        if self._model_stats_path.exists():
            try:
                self._model_stats = json.loads(
                    self._model_stats_path.read_text(encoding="utf-8")
                )
            except Exception:
                self._model_stats = {}

    def _save(self) -> None:
        """Persist lessons and model stats to disk."""
        try:
            self._lessons_path.parent.mkdir(parents=True, exist_ok=True)

            self._lessons_path.write_text(
                json.dumps(
                    {"lessons": self._lessons, "updated_at": datetime.now().isoformat()},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            if self._model_stats:
                self._model_stats_path.write_text(
                    json.dumps(self._model_stats, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception as e:
            logger.error("learning_save_failed", error=str(e))

    def add_lesson(self, lesson: str, category: str = "general", source: str = "analysis") -> None:
        """Add a new lesson learned.

        Args:
            lesson: The lesson text
            category: Category (error_pattern, tool_efficiency, model_performance, general)
            source: How this lesson was discovered
        """
        # Check for duplicates
        for existing in self._lessons:
            if existing["lesson"] == lesson:
                existing["hit_count"] = existing.get("hit_count", 1) + 1
                existing["last_seen"] = datetime.now().isoformat()
                self._save()
                return

        entry = {
            "lesson": lesson,
            "category": category,
            "source": source,
            "created_at": datetime.now().isoformat(),
            "last_seen": datetime.now().isoformat(),
            "hit_count": 1,
        }

        self._lessons.append(entry)

        # Enforce max lessons limit
        if len(self._lessons) > self.config.max_lessons:
            # Remove oldest, lowest hit-count lessons
            self._lessons.sort(key=lambda x: (x.get("hit_count", 0), x.get("last_seen", "")))
            self._lessons = self._lessons[-self.config.max_lessons:]

        self._save()
        logger.info("lesson_added", category=category, total=len(self._lessons))

    def get_relevant_lessons(self, context: str = "", top_k: int | None = None) -> list[str]:
        """Get the most relevant lessons for the current context.

        Uses simple keyword matching. Returns formatted lesson strings.
        """
        if not self._lessons:
            return []

        k = top_k or self.config.inject_top_k

        if not context:
            # No context — return highest hit-count lessons
            sorted_lessons = sorted(
                self._lessons,
                key=lambda x: x.get("hit_count", 0),
                reverse=True,
            )
            return [l["lesson"] for l in sorted_lessons[:k]]

        # Score each lesson by keyword overlap with context
        context_words = set(context.lower().split())
        scored: list[tuple[float, dict]] = []

        for lesson in self._lessons:
            lesson_words = set(lesson["lesson"].lower().split())
            overlap = len(context_words & lesson_words)
            # Boost by hit count
            score = overlap + (lesson.get("hit_count", 0) * 0.5)
            scored.append((score, lesson))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1]["lesson"] for item in scored[:k] if item[0] > 0]

    async def daily_analysis(self) -> dict[str, Any]:
        """Analyze today's action logs and generate new lessons.

        Returns a summary of what was learned.
        """
        if not self.config.enabled:
            return {"status": "skipped", "reason": "disabled"}

        stats = {"new_lessons": 0, "errors_analyzed": 0, "slow_tools": 0}

        # Read today's action logs
        logs = self._read_today_logs()
        if not logs:
            return {"status": "ok", "message": "no logs today"}

        # --- 1. Error pattern analysis ---
        error_groups = self._group_errors(logs)
        for pattern, count in error_groups.items():
            if count >= self.config.error_threshold:
                lesson = f"Recurring error ({count}x today): {pattern}"
                self.add_lesson(lesson, category="error_pattern", source="daily_analysis")
                stats["new_lessons"] += 1
                stats["errors_analyzed"] += count

        # --- 2. Slow tool detection ---
        slow_tools = self._find_slow_tools(logs, threshold_ms=5000)
        for tool_name, avg_ms in slow_tools:
            lesson = f"Tool '{tool_name}' is slow (avg {avg_ms}ms). Consider alternatives or caching."
            self.add_lesson(lesson, category="tool_efficiency", source="daily_analysis")
            stats["new_lessons"] += 1
            stats["slow_tools"] += 1

        # --- 3. Model performance tracking ---
        if self.config.track_model_performance:
            self._update_model_stats(logs)

        logger.info("learning_daily_done", **stats)
        return {"status": "ok", **stats}

    def _read_today_logs(self) -> list[dict[str, Any]]:
        """Read action log entries from today."""
        log_dir = get_kuro_home() / "action_logs"
        if not log_dir.exists():
            return []

        today = datetime.now().strftime("%Y-%m-%d")
        logs: list[dict[str, Any]] = []

        for log_file in log_dir.glob("*.jsonl"):
            try:
                with open(log_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            ts = entry.get("timestamp", "")
                            if ts.startswith(today):
                                logs.append(entry)
                        except json.JSONDecodeError:
                            continue
            except Exception:
                continue

        return logs

    def _group_errors(self, logs: list[dict[str, Any]]) -> dict[str, int]:
        """Group error log entries by pattern.

        Returns a dict of pattern -> count.
        """
        errors: Counter[str] = Counter()

        for entry in logs:
            if entry.get("status") == "error" or entry.get("error"):
                tool = entry.get("tool_name", "unknown")
                error_msg = entry.get("error", "unknown error")
                # Normalize error message (remove specific values)
                normalized = f"{tool}: {error_msg[:100]}"
                errors[normalized] += 1

        return dict(errors)

    def _find_slow_tools(
        self, logs: list[dict[str, Any]], threshold_ms: int = 5000
    ) -> list[tuple[str, int]]:
        """Find tools that are consistently slow.

        Returns list of (tool_name, avg_ms) pairs.
        """
        durations: defaultdict[str, list[int]] = defaultdict(list)

        for entry in logs:
            tool = entry.get("tool_name")
            duration = entry.get("duration_ms")
            if tool and duration and entry.get("status") == "ok":
                durations[tool].append(duration)

        slow: list[tuple[str, int]] = []
        for tool, times in durations.items():
            if len(times) >= 3:  # Need at least 3 data points
                avg = sum(times) // len(times)
                if avg > threshold_ms:
                    slow.append((tool, avg))

        return sorted(slow, key=lambda x: x[1], reverse=True)

    def _update_model_stats(self, logs: list[dict[str, Any]]) -> None:
        """Update model performance statistics from audit logs."""
        # This reads from audit log token usage entries
        audit_dir = get_kuro_home() / "logs"
        if not audit_dir.exists():
            return

        # Simple aggregation: model -> {calls, total_tokens, errors}
        for entry in logs:
            model = entry.get("model", "")
            if not model:
                continue

            if model not in self._model_stats:
                self._model_stats[model] = {
                    "calls": 0,
                    "errors": 0,
                    "total_tokens": 0,
                }

            self._model_stats[model]["calls"] += 1
            if entry.get("status") == "error":
                self._model_stats[model]["errors"] += 1

        self._save()

    def get_model_stats(self) -> dict[str, Any]:
        """Get model performance statistics."""
        return dict(self._model_stats)

    def get_all_lessons(self) -> list[dict[str, Any]]:
        """Get all stored lessons."""
        return list(self._lessons)

    def remove_lesson(self, index: int) -> bool:
        """Remove a lesson by index."""
        if 0 <= index < len(self._lessons):
            self._lessons.pop(index)
            self._save()
            return True
        return False

    def clear_lessons(self) -> None:
        """Clear all lessons."""
        self._lessons.clear()
        self._save()
