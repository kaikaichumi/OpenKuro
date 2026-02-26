"""Memory manager: orchestrates all memory tiers and builds LLM context.

Coordinates:
- Working memory (current conversation sliding window)
- Conversation history (SQLite persistence)
- Long-term memory (ChromaDB RAG + MEMORY.md)
- Context compression (auto-summarize when approaching token limits)
- Memory lifecycle (decay, consolidation, pruning)
- Experience learning (lessons learned from past actions)
"""

from __future__ import annotations

from typing import Any

import structlog

from src.config import KuroConfig, get_kuro_home
from src.core.memory.history import ConversationHistory
from src.core.memory.longterm import LongTermMemory
from src.core.memory.working import WorkingMemory
from src.core.types import Message, Role, Session

logger = structlog.get_logger()


class MemoryManager:
    """Orchestrates all memory tiers for context building."""

    def __init__(
        self,
        working: WorkingMemory | None = None,
        history: ConversationHistory | None = None,
        longterm: LongTermMemory | None = None,
        config: KuroConfig | None = None,
    ) -> None:
        self.working = working or WorkingMemory()
        self.history = history or ConversationHistory()
        self.longterm = longterm or LongTermMemory()
        self._config = config

        # Lazy-initialized components (set up by engine after model_router is ready)
        self.compressor: Any = None  # ContextCompressor
        self.lifecycle: Any = None  # MemoryLifecycle
        self.learning: Any = None  # LearningEngine

    def setup_advanced_features(self, model_router: Any = None) -> None:
        """Initialize compression, lifecycle, and learning components.

        Called after model_router is available (from build_engine).
        """
        if not self._config:
            return

        # Context compression
        from src.core.memory.compressor import ContextCompressor
        self.compressor = ContextCompressor(
            config=self._config.context_compression,
            model_router=model_router,
            longterm_memory=self.longterm,
        )

        # Memory lifecycle
        from src.core.memory.lifecycle import MemoryLifecycle
        self.lifecycle = MemoryLifecycle(
            config=self._config.memory_lifecycle,
            longterm_memory=self.longterm,
            model_router=model_router,
        )

        # Experience learning
        from src.core.learning import LearningEngine
        self.learning = LearningEngine(config=self._config.learning)

    async def build_context(
        self,
        session: Session,
        system_prompt: str,
        core_prompt: str = "",
        active_skills: list | None = None,
    ) -> list[Message]:
        """Build the full context for an LLM call.

        Combines:
        0. Core prompt (encrypted, mandatory base layer — if non-empty)
        1. System prompt (user-configurable supplement)
        2. Active skills (on-demand, injected between system prompt and memory)
        3. MEMORY.md preferences (injected as system context)
        4. Relevant long-term memories via RAG search
        4.5. Lessons learned (from experience learning engine)
        5. Recent conversation messages (working memory window)
        6. Context compression (if approaching token budget)
        """
        context: list[Message] = []

        # 0. Core prompt — always first, never overridden
        if core_prompt:
            context.append(Message(role=Role.SYSTEM, content=core_prompt))

        # 1. System prompt (user-configurable supplement)
        context.append(Message(role=Role.SYSTEM, content=system_prompt))

        # 1.5 Personality (user-defined character & style)
        try:
            personality_path = get_kuro_home() / "personality.md"
            if personality_path.exists():
                personality_text = personality_path.read_text(encoding="utf-8").strip()
                if personality_text:
                    context.append(Message(
                        role=Role.SYSTEM,
                        content=f"[Personality & Style Guide]\n{personality_text}",
                    ))
        except Exception as e:
            logger.debug("personality_load_failed", error=str(e))

        # 2. Active skills (instructions injected on-demand)
        if active_skills:
            for skill in active_skills:
                context.append(Message(
                    role=Role.SYSTEM,
                    content=f"[Skill: {skill.name}]\n{skill.content}",
                ))

        # 3. Load MEMORY.md preferences
        memory_md = self.longterm.read_memory_md()
        if memory_md.strip():
            context.append(Message(
                role=Role.SYSTEM,
                content=f"[User Preferences & Memory]\n{memory_md}",
            ))

        # 4. RAG: search for relevant long-term memories
        recent_user_msgs = [
            m.content for m in session.messages
            if m.role == Role.USER and m.content
        ]
        if recent_user_msgs:
            query = recent_user_msgs[-1]  # Use the latest user message
            try:
                relevant = await self.longterm.search(query, top_k=3)
                if relevant:
                    facts = "\n".join(
                        f"- {m['content']}" for m in relevant
                        if m.get("distance", 1.0) < 0.8  # Only include close matches
                    )
                    if facts:
                        context.append(Message(
                            role=Role.SYSTEM,
                            content=f"[Relevant Memories]\n{facts}",
                        ))

                    # Track access for lifecycle management
                    if self.lifecycle:
                        for m in relevant:
                            if m.get("distance", 1.0) < 0.8 and m.get("id"):
                                try:
                                    await self.lifecycle.track_access(
                                        m["id"], m.get("metadata", {})
                                    )
                                except Exception:
                                    pass
            except Exception as e:
                logger.debug("rag_search_failed", error=str(e))

        # 4.5. Inject lessons learned from experience
        if self.learning and self.learning.config.enabled:
            try:
                query_text = recent_user_msgs[-1] if recent_user_msgs else ""
                lessons = self.learning.get_relevant_lessons(
                    context=query_text if isinstance(query_text, str) else "",
                )
                if lessons:
                    lessons_text = "\n".join(f"- {l}" for l in lessons)
                    context.append(Message(
                        role=Role.SYSTEM,
                        content=f"[Lessons Learned]\n{lessons_text}",
                    ))
            except Exception as e:
                logger.debug("lessons_inject_failed", error=str(e))

        # 5. Working memory: trim conversation to fit
        conversation = [
            m for m in session.messages
            if m.role != Role.SYSTEM
        ]
        trimmed = self.working.trim(conversation)
        context.extend(trimmed)

        # 6. Context compression: if approaching token budget, compress
        if self.compressor:
            try:
                context = await self.compressor.compress_if_needed(context)
            except Exception as e:
                logger.warning("context_compression_failed", error=str(e))

        return context

    async def save_session(self, session: Session) -> None:
        """Persist the current session to history."""
        await self.history.save_session(session)

    async def load_session(self, session_id: str) -> Session | None:
        """Load a session from history."""
        return await self.history.load_session(session_id)

    async def store_fact(
        self,
        content: str,
        tags: list[str] | None = None,
        also_write_md: bool = False,
        md_section: str = "Facts",
    ) -> str:
        """Store a fact in long-term memory.

        Args:
            content: The fact text
            tags: Optional tags for categorization
            also_write_md: If True, also append to MEMORY.md
            md_section: Section in MEMORY.md to append to

        Returns:
            The memory ID.
        """
        metadata: dict[str, Any] = {}
        if tags:
            metadata["tags"] = ",".join(tags)

        memory_id = await self.longterm.store(content, metadata)

        if also_write_md:
            self.longterm.append_to_memory_md(md_section, content)

        return memory_id

    async def search_memories(
        self,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Search long-term memories."""
        return await self.longterm.search(query, top_k)

    async def get_stats(self) -> dict[str, Any]:
        """Get memory system statistics."""
        try:
            fact_count = await self.longterm.count()
        except Exception:
            fact_count = 0

        sessions = await self.history.list_sessions(limit=1)

        return {
            "facts": fact_count,
            "memory_md_size": len(self.longterm.read_memory_md()),
            "sessions": len(sessions),
        }
