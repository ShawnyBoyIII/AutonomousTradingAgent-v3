"""Tests for equity_history table in PortfolioLedger."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from trading_bot.models.portfolio import PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger


class TestEquityHistoryTable:
    """Tests for the equity_history table and its methods."""

    def test_table_created_on_initialize(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        ledger.initialize()
        rows = ledger.list_equity_history()
        assert rows == []

    def test_record_snapshot(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        ledger.initialize()
        ts = datetime(2026, 6, 20, 10, 0, 0)
        state = PortfolioState(cash=10000, equity=10000)
        ledger.record_equity_snapshot(state, timestamp=ts)

        rows = ledger.list_equity_history()
        assert len(rows) == 1
        assert rows[0]["equity"] == 10000.0
        assert rows[0]["cash"] == 10000.0
        assert rows[0]["timestamp"] == ts.isoformat()

    def test_record_multiple_snapshots(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        ledger.initialize()

        for i in range(5):
            state = PortfolioState(cash=float(10000 + i * 100), equity=float(10000 + i * 100))
            ts = datetime(2026, 6, 20, 10, i, 0)
            ledger.record_equity_snapshot(state, timestamp=ts)

        rows = ledger.list_equity_history()
        assert len(rows) == 5
        assert rows[0]["equity"] == 10000.0
        assert rows[-1]["equity"] == 10400.0

    def test_limit_parameter(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        ledger.initialize()

        for i in range(10):
            state = PortfolioState(cash=float(i * 100), equity=float(i * 100 + 1))
            ledger.record_equity_snapshot(state)

        rows = ledger.list_equity_history(limit=3)
        assert len(rows) == 3
        assert rows[0]["equity"] == 1.0  # oldest first

    def test_uses_default_timestamp_when_none(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        ledger.initialize()
        state = PortfolioState(cash=10000, equity=10000)
        ledger.record_equity_snapshot(state)  # no timestamp

        rows = ledger.list_equity_history()
        assert len(rows) == 1
        # Should have a valid ISO timestamp
        parsed = datetime.fromisoformat(rows[0]["timestamp"])
        assert parsed is not None

    def test_records_realized_and_unrealized_pnl(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        ledger.initialize()
        state = PortfolioState(
            cash=9000,
            equity=10500,
            realized_pnl=500.0,
            unrealized_pnl=1000.0,
        )
        ledger.record_equity_snapshot(state)

        rows = ledger.list_equity_history()
        assert rows[0]["realized_pnl"] == 500.0
        assert rows[0]["unrealized_pnl"] == 1000.0

    def test_persists_across_sessions(self, tmp_path: Path) -> None:
        """Equity history should survive ledger re-initialization."""
        db_path = tmp_path / "persist.db"

        ledger1 = PortfolioLedger(db_path)
        ledger1.initialize()
        state = PortfolioState(cash=10000, equity=10000)
        ledger1.record_equity_snapshot(state)

        # New ledger instance, same db
        ledger2 = PortfolioLedger(db_path)
        rows = ledger2.list_equity_history()
        assert len(rows) == 1
        assert rows[0]["equity"] == 10000.0

    def test_table_migrated_on_old_db(self, tmp_path: Path) -> None:
        """An old DB without the equity_history table should auto-create it."""
        db_path = tmp_path / "old.db"
        ledger = PortfolioLedger(db_path)
        ledger.initialize()

        # Drop the table to simulate an old DB
        with ledger._connect() as conn:
            conn.execute("DROP TABLE IF EXISTS equity_history")

        # Re-initialize — should recreate the table
        ledger.initialize()
        rows = ledger.list_equity_history()
        assert rows == []
