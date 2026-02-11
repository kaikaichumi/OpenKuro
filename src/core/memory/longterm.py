"""Long-term memory: ChromaDB vector store + Markdown files.

Hybrid storage:
- ChromaDB: semantic vector search for relevant fact retrieval (RAG)
- MEMORY.md: human-editable markdown file for preferences and facts

The user can directly edit MEMORY.md in a text editor.
ChromaDB is used for efficient semantic search across all stored facts.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

from src.config import get_kuro_home

logger = structlog.get_logger()


class LongTermMemory:
    """Semantic memory using ChromaDB + human-editable Markdown files."""

    def __init__(self, data_dir: str | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir else get_kuro_home() / "memory"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._vector_dir = self._data_dir / "vector_store"
        self._vector_dir.mkdir(parents=True, exist_ok=True)

        self._memory_md = self._data_dir / "MEMORY.md"
        self._facts_dir = self._data_dir / "facts"
        self._facts_dir.mkdir(parents=True, exist_ok=True)

        self._collection = None
        self._client = None

    def _ensure_chroma(self) -> Any:
        """Lazily initialize ChromaDB client and collection."""
        if self._collection is not None:
            return self._collection

        try:
            import chromadb

            self._client = chromadb.PersistentClient(path=str(self._vector_dir))
            self._collection = self._client.get_or_create_collection(
                name="kuro_memory",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("chromadb_initialized", path=str(self._vector_dir))
            return self._collection
        except Exception as e:
            logger.error("chromadb_init_failed", error=str(e))
            raise

    async def store(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
        memory_id: str | None = None,
    ) -> str:
        """Store a fact/memory in the vector store.

        Args:
            content: The text content to store
            metadata: Optional metadata (tags, source, etc.)
            memory_id: Optional custom ID (auto-generated if not provided)

        Returns:
            The ID of the stored memory.
        """
        collection = self._ensure_chroma()
        mid = memory_id or str(uuid4())

        meta = metadata or {}
        meta["stored_at"] = datetime.now(timezone.utc).isoformat()

        collection.upsert(
            ids=[mid],
            documents=[content],
            metadatas=[meta],
        )

        logger.info("memory_stored", id=mid[:8], size=len(content))
        return mid

    async def search(
        self,
        query: str,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for relevant memories using semantic similarity.

        Args:
            query: The search query text
            top_k: Number of results to return
            where: Optional ChromaDB where filter

        Returns:
            List of matching memories with content, metadata, and distance.
        """
        collection = self._ensure_chroma()

        kwargs: dict[str, Any] = {
            "query_texts": [query],
            "n_results": min(top_k, collection.count() or 1),
        }
        if where:
            kwargs["where"] = where

        try:
            results = collection.query(**kwargs)
        except Exception as e:
            logger.error("memory_search_failed", error=str(e))
            return []

        memories = []
        if results and results["documents"]:
            for i, doc in enumerate(results["documents"][0]):
                memory = {
                    "id": results["ids"][0][i] if results["ids"] else "",
                    "content": doc,
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "distance": results["distances"][0][i] if results["distances"] else 0,
                }
                memories.append(memory)

        return memories

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory by ID."""
        collection = self._ensure_chroma()
        try:
            collection.delete(ids=[memory_id])
            return True
        except Exception:
            return False

    async def count(self) -> int:
        """Get the total number of stored memories."""
        collection = self._ensure_chroma()
        return collection.count()

    def read_memory_md(self) -> str:
        """Read the contents of MEMORY.md (user-editable preferences)."""
        if self._memory_md.exists():
            return self._memory_md.read_text(encoding="utf-8")
        return ""

    def write_memory_md(self, content: str) -> None:
        """Write to MEMORY.md."""
        self._memory_md.write_text(content, encoding="utf-8")

    def append_to_memory_md(self, section: str, fact: str) -> None:
        """Append a fact under a section in MEMORY.md.

        If the section doesn't exist, it's created.
        """
        content = self.read_memory_md()
        section_header = f"## {section}"

        if section_header in content:
            # Append after the section header
            lines = content.split("\n")
            new_lines = []
            inserted = False
            for line in lines:
                new_lines.append(line)
                if not inserted and line.strip() == section_header:
                    # Find the next non-empty line or next section
                    new_lines.append(f"- {fact}")
                    inserted = True
            content = "\n".join(new_lines)
        else:
            # Add new section at the end
            content = content.rstrip() + f"\n\n{section_header}\n\n- {fact}\n"

        self.write_memory_md(content)

    async def get_all_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get all stored facts from the vector store."""
        collection = self._ensure_chroma()
        count = collection.count()
        if count == 0:
            return []

        results = collection.get(
            limit=min(limit, count),
            include=["documents", "metadatas"],
        )

        facts = []
        if results and results["documents"]:
            for i, doc in enumerate(results["documents"]):
                facts.append({
                    "id": results["ids"][i],
                    "content": doc,
                    "metadata": results["metadatas"][i] if results["metadatas"] else {},
                })

        return facts
