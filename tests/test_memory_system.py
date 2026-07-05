"""Tests for persistent memory system."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from trading_bot.memory.models import (
    MemoryEntry,
    MemoryQuery,
    MemoryStats,
    MemoryType,
)
from trading_bot.memory.retriever import MemoryRetriever
from trading_bot.memory.store import MemoryStore


class TestMemoryStore:
    """Tests for MemoryStore."""

    @pytest.fixture
    def store(self, tmp_path):
        db_path = str(tmp_path / "memory.db")
        return MemoryStore(db_path)

    def test_save_and_get_memory(self, store):
        entry = MemoryEntry(
            memory_type=MemoryType.RESEARCH_FINDING,
            title="Test memory",
            content="Test content",
            tags=["test"],
        )
        row_id = store.save_memory(entry)
        retrieved = store.get_memory(row_id)
        assert retrieved is not None
        assert retrieved.title == "Test memory"
        assert retrieved.content == "Test content"
        assert retrieved.tags == ["test"]

    def test_update_memory(self, store):
        entry = MemoryEntry(
            memory_type=MemoryType.CUSTOM,
            title="Original",
            content="Original content",
        )
        row_id = store.save_memory(entry)

        entry.id = row_id
        entry.title = "Updated"
        entry.content = "Updated content"
        store.save_memory(entry)

        retrieved = store.get_memory(row_id)
        assert retrieved.title == "Updated"
        assert retrieved.content == "Updated content"

    def test_delete_memory(self, store):
        entry = MemoryEntry(
            memory_type=MemoryType.CUSTOM,
            title="To delete",
            content="Content",
        )
        row_id = store.save_memory(entry)
        assert store.delete_memory(row_id) is True
        assert store.get_memory(row_id) is None

    def test_fts5_search(self, store):
        for i in range(5):
            entry = MemoryEntry(
                memory_type=MemoryType.RESEARCH_FINDING,
                title=f"Momentum strategy {i}",
                content=f"Testing momentum with parameters {i}",
                tags=["momentum", "strategy"],
            )
            store.save_memory(entry)

        query = MemoryQuery(search_text="momentum", limit=10)
        results = store.query_memories(query)
        assert len(results) == 5

    def test_tag_filter(self, store):
        for i in range(3):
            entry = MemoryEntry(
                memory_type=MemoryType.CUSTOM,
                title=f"Tagged memory {i}",
                content=f"Content {i}",
                tags=["alpha", "factor"],
            )
            store.save_memory(entry)

        query = MemoryQuery(tags=["alpha"], limit=10)
        results = store.query_memories(query)
        assert len(results) == 3

    def test_type_filter(self, store):
        for i in range(3):
            entry = MemoryEntry(
                memory_type=MemoryType.RESEARCH_FINDING,
                title=f"Finding {i}",
                content=f"Content {i}",
            )
            store.save_memory(entry)

        for i in range(2):
            entry = MemoryEntry(
                memory_type=MemoryType.TRADING_INSIGHT,
                title=f"Insight {i}",
                content=f"Content {i}",
            )
            store.save_memory(entry)

        query = MemoryQuery(memory_type=MemoryType.RESEARCH_FINDING, limit=10)
        results = store.query_memories(query)
        assert len(results) == 3

    def test_get_memories_by_tags(self, store):
        for i in range(3):
            entry = MemoryEntry(
                memory_type=MemoryType.CUSTOM,
                title=f"Memory {i}",
                content=f"Content {i}",
                tags=["test", "memory"],
            )
            store.save_memory(entry)

        results = store.get_memories_by_tags(["test"])
        assert len(results) == 3

    def test_get_stats(self, store):
        for i in range(5):
            entry = MemoryEntry(
                memory_type=MemoryType.RESEARCH_FINDING,
                title=f"Finding {i}",
                content=f"Content {i}",
            )
            store.save_memory(entry)

        stats = store.get_stats()
        assert stats.total_memories == 5
        assert stats.by_type.get("research_finding", 0) == 5

    def test_batch_save(self, store):
        entries = [
            MemoryEntry(
                memory_type=MemoryType.CUSTOM,
                title=f"Batch {i}",
                content=f"Content {i}",
            )
            for i in range(5)
        ]
        ids = store.batch_save(entries)
        assert len(ids) == 5
        assert all(id > 0 for id in ids)

    def test_clear_all(self, store):
        for i in range(3):
            entry = MemoryEntry(
                memory_type=MemoryType.CUSTOM,
                title=f"Memory {i}",
                content=f"Content {i}",
            )
            store.save_memory(entry)

        count = store.clear_all()
        assert count == 3
        assert store.get_stats().total_memories == 0


class TestMemoryRetriever:
    """Tests for MemoryRetriever."""

    @pytest.fixture
    def retriever(self, tmp_path):
        db_path = str(tmp_path / "memory.db")
        store = MemoryStore(db_path)
        return MemoryRetriever(store)

    def test_recall_for_context(self, retriever):
        # Store some memories
        for i in range(3):
            entry = MemoryEntry(
                memory_type=MemoryType.RESEARCH_FINDING,
                title=f"Momentum strategy {i}",
                content=f"Testing momentum with parameters {i}",
                tags=["momentum"],
            )
            retriever.store.save_memory(entry)

        memories = retriever.recall_for_context(
            context="momentum strategy",
            max_results=5,
        )
        assert len(memories) == 3

    def test_recall_with_symbols(self, retriever):
        # Store symbol-specific memories
        for i in range(3):
            entry = MemoryEntry(
                memory_type=MemoryType.TRADING_INSIGHT,
                title=f"AAPL pattern {i}",
                content=f"AAPL showing pattern {i}",
                tags=["AAPL"],
            )
            retriever.store.save_memory(entry)

        memories = retriever.recall_for_context(
            context="AAPL",
            symbols=["AAPL"],
            max_results=5,
        )
        assert len(memories) == 3

    def test_build_context_prompt(self, retriever):
        memories = [
            MemoryEntry(
                id=1,
                memory_type=MemoryType.RESEARCH_FINDING,
                title="Test finding",
                content="Test content",
                relevance_score=0.8,
                tags=["test"],
            )
        ]
        prompt = retriever.build_context_prompt(memories)
        assert "Relevant Past Research" in prompt
        assert "Test finding" in prompt
        assert "0.80" in prompt

    def test_build_context_prompt_empty(self, retriever):
        prompt = retriever.build_context_prompt([])
        assert prompt == ""

    def test_store_research_finding(self, retriever):
        entry = retriever.store_research_finding(
            title="Test finding",
            content="Test content",
            tags=["test"],
        )
        assert entry.memory_type == MemoryType.RESEARCH_FINDING
        assert entry.title == "Test finding"
        assert entry.id is not None

    def test_store_hypothesis_result(self, retriever):
        entry = retriever.store_hypothesis_result(
            hypothesis_id="hyp_123",
            result="Test result",
            success=True,
        )
        assert entry.memory_type == MemoryType.HYPOTHESIS_RESULT
        assert "PASSED" in entry.title
        assert entry.metadata["success"] is True

    def test_store_trading_insight(self, retriever):
        entry = retriever.store_trading_insight(
            title="Test insight",
            content="Test content",
            symbols=["AAPL", "GOOGL"],
        )
        assert entry.memory_type == MemoryType.TRADING_INSIGHT
        assert "AAPL" in entry.tags
        assert "GOOGL" in entry.tags

    def test_get_stats(self, retriever):
        for i in range(5):
            entry = MemoryEntry(
                memory_type=MemoryType.CUSTOM,
                title=f"Memory {i}",
                content=f"Content {i}",
            )
            retriever.store.save_memory(entry)

        stats = retriever.get_stats()
        assert stats.total_memories == 5

    def test_list_memories_by_type(self, retriever):
        for i in range(3):
            entry = MemoryEntry(
                memory_type=MemoryType.RESEARCH_FINDING,
                title=f"Finding {i}",
                content=f"Content {i}",
            )
            retriever.store.save_memory(entry)

        memories = retriever.list_memories(memory_type=MemoryType.RESEARCH_FINDING)
        assert len(memories) == 3


class TestMemoryEntry:
    """Tests for MemoryEntry model."""

    def test_default_values(self):
        entry = MemoryEntry(title="Test", content="Content")
        assert entry.memory_type == MemoryType.CUSTOM
        assert entry.tags == []
        assert entry.relevance_score == 0.5

    def test_update_method(self):
        entry = MemoryEntry(title="Test", content="Original")
        entry.update(content="Updated", tags=["new_tag"], relevance=0.8)
        assert entry.content == "Updated"
        assert entry.tags == ["new_tag"]
        assert entry.relevance_score == 0.8

    def test_update_partial(self):
        entry = MemoryEntry(title="Test", content="Original", tags=["tag1"])
        entry.update(content="Updated")
        assert entry.content == "Updated"
        assert entry.tags == ["tag1"]  # Unchanged


class TestMemoryQuery:
    """Tests for MemoryQuery model."""

    def test_default_values(self):
        query = MemoryQuery()
        assert query.search_text == ""
        assert query.memory_type is None
        assert query.tags == []
        assert query.min_relevance == 0.0
        assert query.limit == 20
        assert query.sort_by == "relevance"
