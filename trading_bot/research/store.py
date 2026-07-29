"""Research autopilot SQLite storage layer."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.research.models import (
    ExperimentResult,
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    ResearchCycle,
)


class ResearchStore:
    """SQLite-backed storage for research hypotheses and results."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = "state/research.db"
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initialize research database tables."""
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS hypotheses (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    category TEXT,
                    status TEXT DEFAULT 'pending',
                    parameters TEXT,
                    expected_outcome TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    parent_hypothesis_id TEXT,
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS experiment_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hypothesis_id TEXT NOT NULL,
                    backtest_start TEXT,
                    backtest_end TEXT,
                    symbols TEXT,
                    total_return REAL,
                    win_rate REAL,
                    sharpe_ratio REAL,
                    max_drawdown REAL,
                    total_trades INTEGER,
                    profit_factor REAL,
                    avg_trade_pnl REAL,
                    metrics TEXT,
                    completed_at TEXT,
                    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(id)
                );

                CREATE TABLE IF NOT EXISTS research_cycles (
                    id TEXT PRIMARY KEY,
                    hypothesis_id TEXT,
                    experiment_result_id INTEGER,
                    evaluation TEXT,
                    next_hypothesis_id TEXT,
                    completed_at TEXT,
                    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(id),
                    FOREIGN KEY (experiment_result_id) REFERENCES experiment_results(id)
                );

                CREATE INDEX IF NOT EXISTS idx_hypotheses_status ON hypotheses(status);
                CREATE INDEX IF NOT EXISTS idx_hypotheses_category ON hypotheses(category);
                CREATE INDEX IF NOT EXISTS idx_experiment_hypothesis ON experiment_results(hypothesis_id);
            """)
        import os
        try:
            if Path(self.db_path).exists():
                os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    # --- Hypothesis CRUD ---

    def save_hypothesis(self, hypothesis: Hypothesis) -> None:
        """Save or update a hypothesis."""
        with self._get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO hypotheses
                (id, title, description, category, status, parameters,
                 expected_outcome, created_at, updated_at, parent_hypothesis_id, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    hypothesis.id,
                    hypothesis.title,
                    hypothesis.description,
                    hypothesis.category.value,
                    hypothesis.status.value,
                    json.dumps(hypothesis.parameters),
                    hypothesis.expected_outcome,
                    hypothesis.created_at.isoformat(),
                    hypothesis.updated_at.isoformat(),
                    hypothesis.parent_hypothesis_id,
                    hypothesis.notes,
                ),
            )

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        """Get a hypothesis by ID."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM hypotheses WHERE id = ?", (hypothesis_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_hypothesis(dict(row))

    def list_hypotheses(
        self,
        status: HypothesisStatus | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[Hypothesis]:
        """List hypotheses with optional filters."""
        query = "SELECT * FROM hypotheses WHERE 1=1"
        params: list[Any] = []

        if status:
            query += " AND status = ?"
            params.append(status.value)
        if category:
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_hypothesis(dict(r)) for r in rows]

    def update_hypothesis_status(
        self, hypothesis_id: str, status: HypothesisStatus, notes: str = ""
    ) -> None:
        """Update hypothesis status and notes."""
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE hypotheses
                SET status = ?, notes = ?, updated_at = ?
                WHERE id = ?""",
                (
                    status.value,
                    notes,
                    datetime.now(timezone.utc).isoformat(),
                    hypothesis_id,
                ),
            )

    # --- Experiment Results ---

    def save_experiment_result(
        self, result: ExperimentResult
    ) -> int:
        """Save an experiment result. Returns the row ID."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO experiment_results
                (hypothesis_id, backtest_start, backtest_end, symbols,
                 total_return, win_rate, sharpe_ratio, max_drawdown,
                 total_trades, profit_factor, avg_trade_pnl, metrics, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.hypothesis_id,
                    result.backtest_start,
                    result.backtest_end,
                    json.dumps(result.symbols),
                    result.total_return,
                    result.win_rate,
                    result.sharpe_ratio,
                    result.max_drawdown,
                    result.total_trades,
                    result.profit_factor,
                    result.avg_trade_pnl,
                    json.dumps(result.metrics),
                    result.completed_at.isoformat(),
                ),
            )
            result.id = cursor.lastrowid
            return cursor.lastrowid

    def get_experiment_results(
        self, hypothesis_id: str
    ) -> list[ExperimentResult]:
        """Get all experiment results for a hypothesis."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM experiment_results WHERE hypothesis_id = ?",
                (hypothesis_id,),
            ).fetchall()
            return [self._row_to_experiment_result(dict(r)) for r in rows]

    # --- Research Cycles ---

    def save_cycle(self, cycle: ResearchCycle) -> None:
        """Save a research cycle."""
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO research_cycles
                (id, hypothesis_id, experiment_result_id, evaluation,
                 next_hypothesis_id, completed_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    cycle.cycle_id,
                    cycle.hypothesis.id if cycle.hypothesis else None,
                    cycle.experiment_result.id if cycle.experiment_result else None,
                    cycle.evaluation,
                    cycle.next_hypothesis.id if cycle.next_hypothesis else None,
                    cycle.completed_at.isoformat(),
                ),
            )

    def list_cycles(self, limit: int = 20) -> list[ResearchCycle]:
        """List research cycles."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM research_cycles ORDER BY completed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_cycle(dict(r)) for r in rows]

    # --- Stats ---

    def get_stats(self) -> dict[str, Any]:
        """Get research statistics."""
        with self._get_conn() as conn:
            stats: dict[str, Any] = {}

            total = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
            stats["total_hypotheses"] = total

            # Initialize all status counts to 0
            for status in HypothesisStatus:
                stats[f"{status.value}_count"] = 0

            # Get counts for all statuses in a single query
            status_counts = conn.execute(
                "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
            ).fetchall()
            for row in status_counts:
                status_val = row[0]
                count = row[1]
                stats[f"{status_val}_count"] = count

            stats["total_experiments"] = (
                conn.execute("SELECT COUNT(*) FROM experiment_results")
                .fetchone()[0]
            )
            stats["total_cycles"] = (
                conn.execute("SELECT COUNT(*) FROM research_cycles")
                .fetchone()[0]
            )

            # Average win rate across all experiments
            row = conn.execute(
                "SELECT AVG(win_rate) FROM experiment_results"
            ).fetchone()
            avg_wr = row[0] if row[0] is not None else 0.0
            stats["avg_win_rate"] = round(avg_wr, 4)

            # Average sharpe
            row = conn.execute(
                "SELECT AVG(sharpe_ratio) FROM experiment_results"
            ).fetchone()
            avg_sharpe = row[0] if row[0] is not None else 0.0
            stats["avg_sharpe_ratio"] = round(avg_sharpe, 4)

            return stats

    # --- Converters ---

    def _row_to_hypothesis(self, row: dict[str, Any]) -> Hypothesis:
        """Convert a database row to a Hypothesis."""
        return Hypothesis(
            id=row["id"],
            title=row["title"],
            description=row["description"] or "",
            category=HypothesisCategory(row["category"]),
            status=HypothesisStatus(row["status"]),
            parameters=json.loads(row["parameters"]) if row["parameters"] else {},
            expected_outcome=row["expected_outcome"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            parent_hypothesis_id=row["parent_hypothesis_id"],
            notes=row["notes"] or "",
        )

    def _row_to_experiment_result(self, row: dict[str, Any]) -> ExperimentResult:
        """Convert a database row to an ExperimentResult."""
        return ExperimentResult(
            hypothesis_id=row["hypothesis_id"],
            backtest_start=row["backtest_start"],
            backtest_end=row["backtest_end"],
            symbols=json.loads(row["symbols"]),
            total_return=row["total_return"] or 0.0,
            win_rate=row["win_rate"] or 0.0,
            sharpe_ratio=row["sharpe_ratio"] or 0.0,
            max_drawdown=row["max_drawdown"] or 0.0,
            total_trades=row["total_trades"] or 0,
            profit_factor=row["profit_factor"] or 0.0,
            avg_trade_pnl=row["avg_trade_pnl"] or 0.0,
            metrics=json.loads(row["metrics"]) if row["metrics"] else {},
            completed_at=datetime.fromisoformat(row["completed_at"]),
        )

    def _row_to_cycle(self, row: dict[str, Any]) -> ResearchCycle:
        """Convert a database row to a ResearchCycle."""
        hypothesis_id = row["hypothesis_id"]
        hypothesis = (
            self.get_hypothesis(hypothesis_id) if hypothesis_id else None
        )

        experiment_result = None
        if row["experiment_result_id"]:
            with self._get_conn() as conn:
                exp_row = conn.execute(
                    "SELECT * FROM experiment_results WHERE id = ?",
                    (row["experiment_result_id"],),
                ).fetchone()
                if exp_row:
                    experiment_result = self._row_to_experiment_result(
                        dict(exp_row)
                    )

        next_hypothesis = None
        if row["next_hypothesis_id"]:
            next_hypothesis = self.get_hypothesis(row["next_hypothesis_id"])

        return ResearchCycle(
            cycle_id=row["id"],
            hypothesis=hypothesis,
            experiment_result=experiment_result,
            evaluation=row["evaluation"] or "",
            next_hypothesis=next_hypothesis,
            completed_at=datetime.fromisoformat(row["completed_at"]),
        )
