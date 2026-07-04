"""Memory retrieval system for auto-recalling relevant memories.

Provides intelligent memory recall based on context, symbols, and research
topics. Integrates with the research autopilot and scanner systems.
"""

from __future__ import annotations

import logging
from typing import Any

from trading_bot.memory.models import MemoryEntry, MemoryQuery, MemoryStats, MemoryType
from trading_bot.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """Intelligent memory retrieval system.

    Recalls relevant memories based on context, symbols, and topics.
    Auto-recalls into system prompt for trading decisions.
    """

    def __init__(self, store: MemoryStore | None = None):
        self.store = store or MemoryStore()

    def recall_for_context(
        self,
        context: str,
        symbols: list[str] | None = None,
        max_results: int = 10,
    ) -> list[MemoryEntry]:
        """Recall memories relevant to the given context.

        Args:
            context: Current trading context or query.
            symbols: Symbols to prioritize in recall.
            max_results: Maximum memories to return.

        Returns:
            List of relevant MemoryEntry objects.
        """
        memories = []

        # Search by context
        context_query = MemoryQuery(
            search_text=context,
            limit=max_results,
            sort_by="relevance",
        )
        memories.extend(self.store.query_memories(context_query))

        # If symbols provided, search for symbol-specific memories
        if symbols:
            symbol_query = MemoryQuery(
                search_text=" ".join(symbols),
                limit=max_results // 2 if memories else max_results,
                sort_by="relevance",
            )
            symbol_memories = self.store.query_memories(symbol_query)
            # Avoid duplicates
            existing_ids = {m.id for m in memories}
            for m in symbol_memories:
                if m.id not in existing_ids:
                    memories.append(m)
                    existing_ids.add(m.id)

        # Sort by relevance score
        memories.sort(key=lambda m: m.relevance_score, reverse=True)
        return memories[:max_results]

    def recall_for_research(self, hypothesis: Any, max_results: int = 5) -> list[MemoryEntry]:
        """Recall memories relevant to a research hypothesis.

        Args:
            hypothesis: Hypothesis object with title, description, parameters.
            max_results: Maximum memories to return.

        Returns:
            List of relevant MemoryEntry objects.
        """
        context = f"{hypothesis.title} {hypothesis.description}"
        if hasattr(hypothesis, "parameters"):
            params = hypothesis.parameters or {}
            if "factor_name" in params:
                context += f" {params['factor_name']}"
            if "category" in params:
                context += f" {params['category']}"

        return self.recall_for_context(context, max_results=max_results)

    def recall_for_scan(self, symbols: list[str], regime: str = "") -> list[MemoryEntry]:
        """Recall memories relevant to current scan.

        Args:
            symbols: Symbols being scanned.
            regime: Current market regime.

        Returns:
            List of relevant MemoryEntry objects.
        """
        context_parts = list(symbols)
        if regime:
            context_parts.append(regime)

        context = " ".join(context_parts)
        return self.recall_for_context(context, symbols=symbols, max_results=5)

    def build_context_prompt(self, memories: list[MemoryEntry]) -> str:
        """Build a context prompt from recalled memories.

        Args:
            memories: List of recalled MemoryEntry objects.

        Returns:
            Formatted prompt string for system context.
        """
        if not memories:
            return ""

        lines = ["\n## Relevant Past Research"]
        for i, mem in enumerate(memories[:5], 1):
            lines.append(f"\n{i}. [{mem.memory_type.value}] {mem.title}")
            lines.append(f"   Relevance: {mem.relevance_score:.2f}")
            lines.append(f"   Content: {mem.content[:200]}")
            if mem.tags:
                lines.append(f"   Tags: {', '.join(mem.tags)}")

        lines.append("\nConsider these past findings when making decisions.")
        return "\n".join(lines)

    def store_research_finding(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        relevance: float | None = None,
        session_id: str = "",
    ) -> MemoryEntry:
        """Store a research finding as a memory.

        Args:
            title: Finding title.
            content: Finding content.
            tags: Optional tags.
            relevance: Optional relevance score (0-1).
            session_id: Optional session ID.

        Returns:
            Created MemoryEntry.
        """
        entry = MemoryEntry(
            memory_type=MemoryType.RESEARCH_FINDING,
            title=title,
            content=content,
            tags=tags or [],
            relevance_score=relevance or 0.5,
            session_id=session_id,
        )
        row_id = self.store.save_memory(entry)
        entry.id = row_id
        logger.info("Stored research finding: %s", title)
        return entry

    def store_hypothesis_result(
        self,
        hypothesis_id: str,
        result: str,
        success: bool,
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        """Store a hypothesis test result as a memory.

        Args:
            hypothesis_id: ID of the tested hypothesis.
            result: Test result summary.
            success: Whether the hypothesis was supported.
            tags: Optional tags.

        Returns:
            Created MemoryEntry.
        """
        entry = MemoryEntry(
            memory_type=MemoryType.HYPOTHESIS_RESULT,
            title=f"Hypothesis {hypothesis_id}: {'PASSED' if success else 'FAILED'}",
            content=result,
            tags=tags or [],
            relevance_score=0.8 if success else 0.6,
            metadata={"hypothesis_id": hypothesis_id, "success": success},
        )
        row_id = self.store.save_memory(entry)
        entry.id = row_id
        logger.info("Stored hypothesis result: %s", entry.title)
        return entry

    def store_trading_insight(
        self,
        title: str,
        content: str,
        symbols: list[str],
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        """Store a trading insight as a memory.

        Args:
            title: Insight title.
            content: Insight content.
            symbols: Related symbols.
            tags: Optional tags.

        Returns:
            Created MemoryEntry.
        """
        entry = MemoryEntry(
            memory_type=MemoryType.TRADING_INSIGHT,
            title=title,
            content=content,
            tags=(tags or []) + symbols,
            relevance_score=0.7,
            metadata={"symbols": symbols},
        )
        row_id = self.store.save_memory(entry)
        entry.id = row_id
        logger.info("Stored trading insight: %s", title)
        return entry

    def get_stats(self) -> MemoryStats:
        """Get memory statistics."""
        return self.store.get_stats()

    def list_memories(
        self,
        memory_type: MemoryType | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]:
        """List memories with optional type filter."""
        if memory_type:
            return self.store.get_memories_by_type(memory_type, limit=limit)
        query = MemoryQuery(limit=limit, sort_by="date")
        return self.store.query_memories(query)
