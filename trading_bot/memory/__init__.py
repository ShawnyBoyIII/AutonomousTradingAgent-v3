"""Persistent memory system: FTS5 cross-session learning.

Stores and recalls past research findings, hypothesis results, and trading
insights across sessions. Auto-recalls relevant memories into system context.
"""

from __future__ import annotations

from typing import Any

from .models import MemoryEntry, MemoryQuery, MemoryType
from .retriever import MemoryRetriever
from .store import MemoryStore

__all__ = [
    "MemoryEntry",
    "MemoryQuery",
    "MemoryStore",
    "MemoryType",
    "MemoryRetriever",
]
