"""Memory lifecycle management: decay, consolidation, and pruning.

Prevents infinite memory growth by:
1. Tracking access patterns (access_count, last_accessed)
2. Scoring importance (recency × frequency × source weight)
3. Applying exponential time-based decay to unused memories
4. Periodically consolidating (merging) similar memories
5. Pruning memories that fall below importance threshold
6. Auto-organizing MEMORY.md when it gets too large
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from src.config import MemoryLifecycleConfig

logger = structlog.get_logger()

# Source weights: user-stored memories are more important than auto-extracted ones
_SOURCE_WEIGHTS = {
    "user": 1.5,
    "agent": 1.0,
    "compression": 0.8,
    "system": 1.2,
}


class MemoryLifecycle:
    """Manages the lifecycle of long-term memories: scoring, decay, consolidation, pruning."""

    def __init__(
        self,
        config: MemoryLifecycleConfig,
        longterm_memory: Any = None,
        model_router: Any = None,
    ) -> None:
        self.config = config
        self._longterm = longterm_memory
        self._model = model_router

    def calculate_importance(self, metadata: dict[str, Any]) -> float:
        """Calculate importance score for a memory based on its metadata.

        score = base_importance × recency_factor × frequency_factor × source_weight

        recency_factor = exp(-λ × days_since_access)   [half-life ~69 days at λ=0.01]
        frequency_factor = log(1 + access_count)
        """
        if metadata.get("is_pinned"):
            return 1.0  # Pinned memories never decay

        base = float(metadata.get("importance", 0.5))
        access_count = int(metadata.get("access_count", 0))
        source = metadata.get("source", "agent")

        # Recency factor
        last_accessed_str = metadata.get("last_accessed")
        if last_accessed_str:
            try:
                last_dt = datetime.fromisoformat(last_accessed_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                days_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400
            except (ValueError, TypeError):
                days_since = 30.0  # Default if parsing fails
        else:
            # Use stored_at as fallback
            stored_str = metadata.get("stored_at", "")
            try:
                stored_dt = datetime.fromisoformat(stored_str)
                if stored_dt.tzinfo is None:
                    stored_dt = stored_dt.replace(tzinfo=timezone.utc)
                days_since = (datetime.now(timezone.utc) - stored_dt).total_seconds() / 86400
            except (ValueError, TypeError):
                days_since = 30.0

        recency = math.exp(-self.config.decay_lambda * days_since)

        # Frequency factor (logarithmic buffer)
        frequency = math.log(1 + access_count) if access_count > 0 else 0.5

        # Source weight
        source_weight = _SOURCE_WEIGHTS.get(source, 1.0)

        score = base * recency * frequency * source_weight

        # Clamp to [0, 1]
        return max(0.0, min(1.0, score))

    async def daily_maintenance(self) -> dict[str, Any]:
        """Run daily maintenance: update importance scores for all memories.

        Returns a summary of the maintenance run.
        """
        if not self.config.enabled or not self._longterm:
            return {"status": "skipped", "reason": "disabled or no longterm memory"}

        stats = {"updated": 0, "flagged_for_prune": 0, "errors": 0}

        try:
            all_facts = await self._longterm.get_all_facts(limit=500)
        except Exception as e:
            logger.error("lifecycle_daily_fetch_failed", error=str(e))
            return {"status": "error", "error": str(e)}

        for fact in all_facts:
            metadata = fact.get("metadata", {})
            memory_id = fact.get("id", "")

            if not memory_id:
                continue

            try:
                # Calculate new importance
                new_score = self.calculate_importance(metadata)

                # Update metadata with new score
                updated_meta = dict(metadata)
                updated_meta["importance"] = new_score

                # Re-store with updated metadata
                await self._longterm.store(
                    fact.get("content", ""),
                    metadata=updated_meta,
                    memory_id=memory_id,
                )
                stats["updated"] += 1

                if new_score < self.config.prune_threshold:
                    stats["flagged_for_prune"] += 1

            except Exception as e:
                stats["errors"] += 1
                logger.debug("lifecycle_update_failed", id=memory_id[:8], error=str(e))

        logger.info("lifecycle_daily_done", **stats)
        return {"status": "ok", **stats}

    async def weekly_consolidation(self) -> dict[str, Any]:
        """Run weekly consolidation: merge similar memories and prune low-importance ones.

        Returns a summary of the consolidation run.
        """
        if not self.config.enabled or not self._longterm:
            return {"status": "skipped", "reason": "disabled or no longterm memory"}

        stats = {"merged": 0, "pruned": 0, "errors": 0}

        try:
            all_facts = await self._longterm.get_all_facts(limit=500)
        except Exception as e:
            logger.error("lifecycle_weekly_fetch_failed", error=str(e))
            return {"status": "error", "error": str(e)}

        # --- Phase 1: Prune low-importance memories ---
        for fact in all_facts:
            metadata = fact.get("metadata", {})
            memory_id = fact.get("id", "")

            if not memory_id:
                continue

            # Skip pinned memories
            if metadata.get("is_pinned"):
                continue

            importance = float(metadata.get("importance", 0.5))
            if importance < self.config.prune_threshold:
                try:
                    await self._longterm.delete(memory_id)
                    stats["pruned"] += 1
                    logger.debug(
                        "memory_pruned",
                        id=memory_id[:8],
                        importance=round(importance, 3),
                    )
                except Exception as e:
                    stats["errors"] += 1
                    logger.debug("prune_failed", id=memory_id[:8], error=str(e))

        # --- Phase 2: Find and merge similar memories ---
        # Re-fetch after pruning
        try:
            remaining = await self._longterm.get_all_facts(limit=500)
        except Exception:
            remaining = []

        # Simple O(n²) similarity check — acceptable for <500 memories
        processed_ids: set[str] = set()
        for i, fact_a in enumerate(remaining):
            id_a = fact_a.get("id", "")
            if id_a in processed_ids:
                continue

            content_a = fact_a.get("content", "")
            if not content_a:
                continue

            # Search for similar memories
            try:
                similar = await self._longterm.search(content_a, top_k=3)
            except Exception:
                continue

            for match in similar:
                id_b = match.get("id", "")
                if id_b == id_a or id_b in processed_ids:
                    continue

                distance = match.get("distance", 1.0)
                if distance < self.config.consolidation_distance:
                    # Merge: keep the one with higher importance, delete the other
                    meta_a = fact_a.get("metadata", {})
                    meta_b = match.get("metadata", {})

                    imp_a = float(meta_a.get("importance", 0.5))
                    imp_b = float(meta_b.get("importance", 0.5))

                    if imp_a >= imp_b:
                        # Keep A, delete B
                        try:
                            await self._longterm.delete(id_b)
                            processed_ids.add(id_b)
                            stats["merged"] += 1
                        except Exception:
                            stats["errors"] += 1
                    else:
                        # Keep B, delete A
                        try:
                            await self._longterm.delete(id_a)
                            processed_ids.add(id_a)
                            stats["merged"] += 1
                        except Exception:
                            stats["errors"] += 1
                    break  # Only merge once per memory

        logger.info("lifecycle_weekly_done", **stats)
        return {"status": "ok", **stats}

    async def manage_memory_md(self) -> dict[str, Any]:
        """Auto-organize MEMORY.md when it exceeds the configured line limit.

        Uses an LLM to intelligently reorganize, deduplicate, and
        archive low-frequency items to ChromaDB.
        """
        if not self._longterm:
            return {"status": "skipped", "reason": "no longterm memory"}

        content = self._longterm.read_memory_md()
        lines = content.split("\n")

        if len(lines) <= self.config.memory_md_max_lines:
            return {"status": "ok", "lines": len(lines), "action": "none"}

        logger.info(
            "memory_md_reorganize",
            current_lines=len(lines),
            max_lines=self.config.memory_md_max_lines,
        )

        if self._model:
            organized = await self._llm_organize(content)
        else:
            organized = self._simple_organize(content)

        self._longterm.write_memory_md(organized)
        new_lines = len(organized.split("\n"))

        logger.info("memory_md_reorganized", old_lines=len(lines), new_lines=new_lines)
        return {"status": "ok", "old_lines": len(lines), "new_lines": new_lines}

    async def _llm_organize(self, content: str) -> str:
        """Use LLM to intelligently organize MEMORY.md."""
        prompt = (
            "Reorganize this MEMORY.md file to be more concise and organized:\n"
            "- Remove duplicate or redundant entries\n"
            "- Merge related items\n"
            "- Keep all unique, important information\n"
            "- Maintain section headers (## format)\n"
            "- Output the reorganized markdown directly, no explanation\n\n"
            f"Current MEMORY.md:\n{content}"
        )

        try:
            response = await self._model.complete(
                messages=[{"role": "user", "content": prompt}],
                model=self.config._summarize_model if hasattr(self.config, '_summarize_model') else "gemini/gemini-2.0-flash",
                max_tokens=2000,
                temperature=0.2,
            )
            result = response.content or ""
            if result.strip() and len(result) > 50:
                return result.strip()
        except Exception as e:
            logger.warning("memory_md_llm_organize_failed", error=str(e))

        return self._simple_organize(content)

    def _simple_organize(self, content: str) -> str:
        """Simple deduplication fallback when LLM is unavailable."""
        lines = content.split("\n")
        seen: set[str] = set()
        result: list[str] = []

        for line in lines:
            stripped = line.strip()
            # Always keep headers and blank lines
            if stripped.startswith("#") or not stripped:
                result.append(line)
                continue

            # Deduplicate content lines
            if stripped not in seen:
                seen.add(stripped)
                result.append(line)

        return "\n".join(result)

    async def track_access(self, memory_id: str, metadata: dict[str, Any]) -> None:
        """Update access tracking for a retrieved memory.

        Called when a memory is returned by RAG search.
        """
        if not self._longterm:
            return

        try:
            updated = dict(metadata)
            updated["access_count"] = int(updated.get("access_count", 0)) + 1
            updated["last_accessed"] = datetime.now(timezone.utc).isoformat()

            # We need to re-store to update metadata (ChromaDB upsert)
            # The content won't change, just metadata
            collection = self._longterm._ensure_chroma()
            existing = collection.get(ids=[memory_id], include=["documents"])
            if existing and existing["documents"]:
                content = existing["documents"][0]
                await self._longterm.store(content, metadata=updated, memory_id=memory_id)
        except Exception as e:
            logger.debug("access_track_failed", id=memory_id[:8], error=str(e))
