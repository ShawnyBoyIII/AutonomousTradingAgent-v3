from __future__ import annotations

from datetime import datetime

from trading_bot.models.portfolio import PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger


def test_recent_equity_history_returns_newest_window_in_chronological_order(tmp_path) -> None:
    ledger = PortfolioLedger(tmp_path / "state.db")
    for index in range(5):
        ledger.record_equity_snapshot(
            PortfolioState(cash=1000.0 + index, equity=1000.0 + index),
            timestamp=datetime(2026, 7, 11, 9, index),
        )

    rows = ledger.list_recent_equity_history(limit=2)

    assert [row["equity"] for row in rows] == [1003.0, 1004.0]
    assert rows[-1]["timestamp"] == "2026-07-11T09:04:00"


def test_recent_equity_history_limit_floor_is_one(tmp_path) -> None:
    ledger = PortfolioLedger(tmp_path / "state.db")
    ledger.record_equity_snapshot(PortfolioState(cash=1.0, equity=1.0))
    ledger.record_equity_snapshot(PortfolioState(cash=2.0, equity=2.0))

    rows = ledger.list_recent_equity_history(limit=0)

    assert len(rows) == 1
    assert rows[0]["equity"] == 2.0
