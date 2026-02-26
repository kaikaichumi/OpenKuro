"""Context compressor: intelligent context summarization when approaching token limits.

Instead of hard-truncating old messages, this module:
1. Detects when the context is approaching the token budget.
2. Partitions messages into system, old, and recent.
3. Summarizes the old messages using a cheap/fast model.
4. Extracts key facts and stores them in long-term memory.
5. Replaces old messages with a compact summary message.

This allows conversations to continue indefinitely without losing
important context from earlier turns.
"""

from __future__ import annotations

from typing import Any

import structlog

from src.config import ContextCompressionConfig
from src.core.types import Message, Role

logger = structlog.get_logger()

# Heuristic: ~4 characters per token (works for most models)
_CHARS_PER_TOKEN = 4


class ContextCompressor:
    """Three-tier context compression: recent (verbatim) → old (summary) → facts (long-term)."""

    def __init__(
        self,
        config: ContextCompressionConfig,
        model_router: Any = None,
        longterm_memory: Any = None,
    ) -> None:
        self.config = config
        self._model = model_router
        self._longterm = longterm_memory
        self._last_summary: str | None = None  # Cache for debugging

    def estimate_tokens(self, messages: list[Message]) -> int:
        """Estimate the total token count for a list of messages."""
        total = 0
        for m in messages:
            if isinstance(m.content, str):
                total += len(m.content)
            elif isinstance(m.content, list):
                for part in m.content:
                    if isinstance(part, dict):
                        text = part.get("text", "")
                        total += len(text)
                        # Images cost ~1000 tokens
                        if part.get("type") == "image_url":
                            total += 4000
        return total // _CHARS_PER_TOKEN

    async def compress_if_needed(
        self, messages: list[Message]
    ) -> list[Message]:
        """Compress context if approaching the token budget.

        Returns the (potentially compressed) list of messages.
        If compression is not needed, returns the original list unchanged.
        """
        if not self.config.enabled:
            return messages

        current_tokens = self.estimate_tokens(messages)
        threshold = int(self.config.token_budget * self.config.trigger_threshold)

        if current_tokens < threshold:
            return messages

        logger.info(
            "context_compression_triggered",
            current_tokens=current_tokens,
            threshold=threshold,
            budget=self.config.token_budget,
        )

        # Partition: system messages | old conversation | recent conversation
        system_msgs, old_msgs, recent_msgs = self._partition(messages)

        if not old_msgs:
            # Nothing to compress — all messages are either system or recent
            return messages

        # Summarize old messages
        summary_text = await self._summarize(old_msgs)
        self._last_summary = summary_text

        # Extract key facts and store in long-term memory
        if self.config.extract_facts and self._longterm:
            await self._extract_and_store_facts(old_msgs)

        # Build the compressed summary message
        summary_msg = Message(
            role=Role.SYSTEM,
            content=(
                "[Compressed Conversation History]\n"
                "The following is a summary of earlier conversation turns "
                "that have been compressed to save context space:\n\n"
                f"{summary_text}"
            ),
        )

        compressed = system_msgs + [summary_msg] + recent_msgs
        new_tokens = self.estimate_tokens(compressed)

        logger.info(
            "context_compressed",
            old_tokens=current_tokens,
            new_tokens=new_tokens,
            saved_tokens=current_tokens - new_tokens,
            old_messages=len(old_msgs),
            kept_recent=len(recent_msgs),
        )

        return compressed

    def _partition(
        self, messages: list[Message]
    ) -> tuple[list[Message], list[Message], list[Message]]:
        """Split messages into system, old, and recent partitions.

        - system: all SYSTEM role messages (always kept verbatim)
        - old: non-system messages to be compressed
        - recent: most recent N turns to keep verbatim
        """
        system_msgs: list[Message] = []
        non_system: list[Message] = []

        for m in messages:
            if m.role == Role.SYSTEM:
                system_msgs.append(m)
            else:
                non_system.append(m)

        # Count "turns" — a turn = one user message + subsequent responses/tool results
        # We keep the last N turns verbatim
        keep_count = self.config.keep_recent_turns * 3  # ~3 messages per turn (user + assistant + tool)
        keep_count = max(keep_count, 6)  # Keep at least 6 messages

        if len(non_system) <= keep_count:
            # Not enough to compress
            return system_msgs, [], non_system

        old = non_system[:-keep_count]
        recent = non_system[-keep_count:]

        return system_msgs, old, recent

    async def _summarize(self, messages: list[Message]) -> str:
        """Summarize a batch of old messages using a cheap/fast model."""
        if not self._model:
            return self._fallback_summarize(messages)

        # Build a transcript of the old messages for summarization
        transcript_lines: list[str] = []
        for m in messages:
            role_name = m.role.value.upper()
            content = m.content if isinstance(m.content, str) else str(m.content)
            # Truncate very long messages to avoid blowing up the summarization prompt
            if len(content) > 2000:
                content = content[:2000] + "... (truncated)"
            if m.name:
                transcript_lines.append(f"[{role_name}: {m.name}] {content}")
            else:
                transcript_lines.append(f"[{role_name}] {content}")

        transcript = "\n".join(transcript_lines)

        # Cap transcript to avoid exceeding summarization model limits
        if len(transcript) > 30000:
            transcript = transcript[-30000:]

        prompt = (
            "Summarize this conversation concisely, preserving:\n"
            "- Decisions made and their rationale\n"
            "- File paths, code changes, and commands executed\n"
            "- Errors encountered and how they were resolved\n"
            "- Pending tasks or open questions\n"
            "- User preferences or important context\n\n"
            "Be concise but comprehensive. Use bullet points.\n\n"
            f"CONVERSATION:\n{transcript}"
        )

        try:
            from src.core.types import ModelResponse
            response = await self._model.complete(
                messages=[{"role": "user", "content": prompt}],
                model=self.config.summarize_model,
                max_tokens=self.config.max_summary_tokens,
                temperature=0.3,
            )
            summary = response.content or ""
            if summary.strip():
                return summary.strip()
        except Exception as e:
            logger.warning("compression_summarize_failed", error=str(e))

        # Fallback if model summarization fails
        return self._fallback_summarize(messages)

    def _fallback_summarize(self, messages: list[Message]) -> str:
        """Simple extraction-based fallback when LLM summarization is unavailable."""
        lines: list[str] = []
        for m in messages:
            if m.role == Role.USER:
                content = m.content if isinstance(m.content, str) else ""
                if content.strip():
                    # Keep first 100 chars of each user message
                    preview = content.strip()[:100]
                    lines.append(f"- User asked: {preview}")
            elif m.role == Role.ASSISTANT and not m.tool_calls:
                content = m.content if isinstance(m.content, str) else ""
                if content.strip():
                    preview = content.strip()[:100]
                    lines.append(f"- Assistant: {preview}")

        if not lines:
            return "(No significant conversation to summarize)"

        # Keep it concise
        return "\n".join(lines[:20])

    async def _extract_and_store_facts(self, messages: list[Message]) -> None:
        """Extract key facts from old messages and store in long-term memory."""
        if not self._longterm:
            return

        # Collect user messages that might contain important facts
        user_contents: list[str] = []
        for m in messages:
            if m.role == Role.USER and isinstance(m.content, str) and m.content.strip():
                user_contents.append(m.content.strip())

        if not user_contents:
            return

        # For each substantial user message, store a condensed version
        for content in user_contents:
            # Only store if the message is substantial (>50 chars, likely contains info)
            if len(content) > 50:
                try:
                    # Truncate to reasonable size for storage
                    fact = content[:500] if len(content) > 500 else content
                    await self._longterm.store(
                        fact,
                        metadata={
                            "source": "compression",
                            "access_count": 0,
                            "importance": 0.5,
                        },
                    )
                except Exception as e:
                    logger.debug("fact_extraction_store_failed", error=str(e))
