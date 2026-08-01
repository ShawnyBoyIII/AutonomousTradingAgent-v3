"""Persistent memory SQLite storage layer with FTS5 search."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from trading_bot.memory.models import MemoryEntry, MemoryQuery, MemoryStats, MemoryType


class MemoryStore:
    """SQLite-backed memory store with FTS5 full-text search."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = "state/memory.db"
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initialize memory database with FTS5."""
        with self._get_conn() as conn:
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT,
                    relevance_score REAL DEFAULT 0.5,
                    created_at TEXT,
                    updated_at TEXT,
                    session_id TEXT,
                    metadata TEXT
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    title,
                    content,
                    tags,
                    content='memories',
                    content_rowid='id'
                );

                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, title, content, tags)
                    VALUES (new.id, new.title, new.content, new.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags)
                    VALUES('delete', old.id, old.title, old.content, old.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags)
                    VALUES('delete', old.id, old.title, old.content, old.tags);
                    INSERT INTO memories_fts(rowid, title, content, tags)
                    VALUES (new.id, new.title, new.content, new.tags);
                END;

                CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
                CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
                CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id);
                CREATE INDEX IF NOT EXISTS idx_memories_relevance ON memories(relevance_score);
            """)

    # --- CRUD ---

    def save_memory(self, entry: MemoryEntry) -> int:
        """Save or update a memory entry. Returns the ID."""
        with self._get_conn() as conn:
            if entry.id:
                conn.execute(
                    """UPDATE memories
                    SET memory_type = ?, title = ?, content = ?, tags = ?,
                        relevance_score = ?, updated_at = ?, session_id = ?, metadata = ?
                    WHERE id = ?""",
                    (
                        entry.memory_type.value,
                        entry.title,
                        entry.content,
                        json.dumps(entry.tags),
                        entry.relevance_score,
                        entry.updated_at.isoformat(),
                        entry.session_id,
                        json.dumps(entry.metadata),
                        entry.id,
                    ),
                )
                row_id = entry.id
            else:
                cursor = conn.execute(
                    """INSERT INTO memories
                    (memory_type, title, content, tags, relevance_score,
                     created_at, updated_at, session_id, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.memory_type.value,
                        entry.title,
                        entry.content,
                        json.dumps(entry.tags),
                        entry.relevance_score,
                        entry.created_at.isoformat(),
                        entry.updated_at.isoformat(),
                        entry.session_id,
                        json.dumps(entry.metadata),
                    ),
                )
                row_id = cursor.lastrowid

            return row_id

    def get_memory(self, memory_id: int) -> MemoryEntry | None:
        """Get a memory by ID."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            return self._row_to_entry(dict(row)) if row else None

    def delete_memory(self, memory_id: int) -> bool:
        """Delete a memory by ID."""
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cursor.rowcount > 0

    # --- Query ---

    def query_memories(self, query: MemoryQuery) -> list[MemoryEntry]:
        """Query memories with FTS5 search and filters."""
        with self._get_conn() as conn:
            conditions = []
            params: list[Any] = []

            # FTS5 search
            if query.search_text:
                conditions.append("memories_fts MATCH ?")
                params.append(query.search_text)

            # Type filter
            if query.memory_type:
                conditions.append("m.memory_type = ?")
                params.append(query.memory_type.value)

            # Tag filter
            if query.tags:
                for tag in query.tags:
                    conditions.append("m.tags LIKE ?")
                    params.append(f'%"{tag}"%')

            # Relevance filter
            if query.min_relevance > 0:
                conditions.append("m.relevance_score >= ?")
                params.append(query.min_relevance)

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            # Build ORDER BY
            if query.sort_by == "relevance":
                order = "m.relevance_score DESC, m.created_at DESC"
            elif query.sort_by == "date":
                order = "m.created_at DESC"
            else:
                order = "m.relevance_score DESC, m.created_at DESC"

            sql = f"""
                SELECT m.*, memories_fts.rank
                FROM memories m
                JOIN memories_fts ON memories_fts.rowid = m.id
                WHERE {where_clause}
                ORDER BY {order}
                LIMIT ?
            """
            params.append(query.limit)

            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_entry(dict(r)) for r in rows]

    def get_memories_by_type(self, memory_type: MemoryType, limit: int = 50) -> list[MemoryEntry]:
        """Get memories filtered by type."""
        query = MemoryQuery(
            memory_type=memory_type,
            limit=limit,
            sort_by="date",
        )
        return self.query_memories(query)

    def get_memories_by_session(self, session_id: str) -> list[MemoryEntry]:
        """Get memories from a specific session."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
            return [self._row_to_entry(dict(r)) for r in rows]

    def get_memories_by_tags(self, tags: list[str], limit: int = 50) -> list[MemoryEntry]:
        """Get memories matching any of the given tags."""
        query = MemoryQuery(tags=tags, limit=limit, sort_by="relevance")
        return self.query_memories(query)

    # --- Stats ---

    def get_stats(self) -> MemoryStats:
        """Get memory statistics."""
        with self._get_conn() as conn:
            stats = MemoryStats()
            stats.total_memories = conn.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()[0]

            # By type
            rows = conn.execute(
                "SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type"
            ).fetchall()
            stats.by_type = {row[0]: row[1] for row in rows}

            # Recent counts
            now = datetime.now(timezone.utc)
            seven_days_ago = (now - timedelta(days=7)).isoformat()
            thirty_days_ago = (now - timedelta(days=30)).isoformat()

            stats.recent_count_7d = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE created_at >= ?",
                (seven_days_ago,),
            ).fetchone()[0]

            stats.recent_count_30d = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE created_at >= ?",
                (thirty_days_ago,),
            ).fetchone()[0]

            # Average relevance
            row = conn.execute(
                "SELECT AVG(relevance_score) FROM memories"
            ).fetchone()
            stats.avg_relevance = round(row[0] or 0.0, 4)

            # Unique tags
            rows = conn.execute(
                "SELECT DISTINCT json_each.value FROM memories, json_each(memories.tags)"
            ).fetchall()
            stats.tag_count = len(rows)

            return stats

    # --- Bulk operations ---

    def batch_save(self, entries: list[MemoryEntry]) -> list[int]:
        """Save multiple memories in a single transaction."""
        ids = []
        with self._get_conn() as conn:
            for entry in entries:
                row_id = self.save_memory(entry)
                ids.append(row_id)
        return ids

    def clear_all(self) -> int:
        """Delete all memories. Returns count deleted."""
        with self._get_conn() as conn:
            count = conn.execute("DELETE FROM memories").rowcount
            conn.execute("DELETE FROM memories_fts")
            return count

    # --- Converter ---

    def _row_to_entry(self, row: dict[str, Any]) -> MemoryEntry:
        """Convert a database row to a MemoryEntry."""
        return MemoryEntry(
            id=row["id"],
            memory_type=MemoryType(row["memory_type"]),
            title=row["title"],
            content=row["content"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            relevance_score=row["relevance_score"] or 0.5,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            session_id=row["session_id"] or "",
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )
