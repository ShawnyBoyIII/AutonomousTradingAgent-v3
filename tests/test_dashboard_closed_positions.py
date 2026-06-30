"""Tests for dashboard closed positions rendering and ledger loading."""

from pathlib import Path
from unittest.mock import patch

import pytest


def _tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "state" / "burn_in.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return db


class TestLoadClosedPositions:
    """_load_closed_positions reads paired BUY/SELL orders from ledger."""

    def test_empty_ledger(self, tmp_path: Path):
        from trading_bot.runtime.dashboard import _load_closed_positions
        from trading_bot.config.settings import Settings

        db = _tmp_db(tmp_path)
        settings = Settings(app__state_db_path=str(db))

        with patch("trading_bot.runtime.dashboard.PortfolioLedger") as MockLedger:
            MockLedger.return_value.list_order_rows.return_value = []
            result = _load_closed_positions(settings)
        assert result == []

    def test_ledger_error_returns_empty(self, tmp_path: Path):
        from trading_bot.runtime.dashboard import _load_closed_positions
        from trading_bot.config.settings import Settings

        db = _tmp_db(tmp_path)
        settings = Settings(app__state_db_path=str(db))

        with patch("trading_bot.runtime.dashboard.PortfolioLedger") as MockLedger:
            MockLedger.side_effect = RuntimeError("db gone")
            result = _load_closed_positions(settings)
        assert result == []

    def test_single_buy_sell_pair(self, tmp_path: Path):
        from trading_bot.runtime.dashboard import _load_closed_positions
        from trading_bot.config.settings import Settings

        db = _tmp_db(tmp_path)
        settings = Settings(app__state_db_path=str(db))

        order_rows = [
            {
                "id": "1",
                "ticker": "AAPL",
                "side": "BUY",
                "quantity": 10,
                "fill_price": 150.0,
                "filled_at": "2026-06-29T10:00:00",
                "pnl": 0,
            },
            {
                "id": "2",
                "ticker": "AAPL",
                "side": "SELL",
                "quantity": 10,
                "fill_price": 155.0,
                "filled_at": "2026-06-29T14:00:00",
                "pnl": 50.0,
            },
        ]

        with patch("trading_bot.runtime.dashboard.PortfolioLedger") as MockLedger:
            MockLedger.return_value.list_order_rows.return_value = order_rows
            result = _load_closed_positions(settings)

        assert len(result) == 1
        pos = result[0]
        assert pos["ticker"] == "AAPL"
        assert pos["entry_price"] == 150.0
        assert pos["exit_price"] == 155.0
        assert pos["quantity"] == 10
        assert pos["pnl"] == 50.0
        assert pos["win"] is True
        assert pos["entry_date"] == "2026-06-29T10:00:00"
        assert pos["exit_date"] == "2026-06-29T14:00:00"

    def test_sell_without_buy_ignored(self, tmp_path: Path):
        from trading_bot.runtime.dashboard import _load_closed_positions
        from trading_bot.config.settings import Settings

        db = _tmp_db(tmp_path)
        settings = Settings(app__state_db_path=str(db))

        order_rows = [
            {
                "id": "1",
                "ticker": "AAPL",
                "side": "SELL",
                "quantity": 10,
                "fill_price": 155.0,
                "filled_at": "2026-06-29T14:00:00",
                "pnl": 0,
            },
        ]

        with patch("trading_bot.runtime.dashboard.PortfolioLedger") as MockLedger:
            MockLedger.return_value.list_order_rows.return_value = order_rows
            result = _load_closed_positions(settings)

        assert result == []

    def test_multiple_tickers_sorted_by_exit_date(self, tmp_path: Path):
        from trading_bot.runtime.dashboard import _load_closed_positions
        from trading_bot.config.settings import Settings

        db = _tmp_db(tmp_path)
        settings = Settings(app__state_db_path=str(db))

        order_rows = [
            {"id": "1", "ticker": "AAPL", "side": "BUY", "quantity": 5, "fill_price": 100.0, "filled_at": "2026-06-28T10:00:00", "pnl": 0},
            {"id": "2", "ticker": "AAPL", "side": "SELL", "quantity": 5, "fill_price": 110.0, "filled_at": "2026-06-28T14:00:00", "pnl": 50.0},
            {"id": "3", "ticker": "GOOGL", "side": "BUY", "quantity": 3, "fill_price": 200.0, "filled_at": "2026-06-29T10:00:00", "pnl": 0},
            {"id": "4", "ticker": "GOOGL", "side": "SELL", "quantity": 3, "fill_price": 190.0, "filled_at": "2026-06-29T14:00:00", "pnl": -30.0},
        ]

        with patch("trading_bot.runtime.dashboard.PortfolioLedger") as MockLedger:
            MockLedger.return_value.list_order_rows.return_value = order_rows
            result = _load_closed_positions(settings)

        assert len(result) == 2
        # GOOGL (exit 2026-06-29) should come first (most recent)
        assert result[0]["ticker"] == "GOOGL"
        assert result[1]["ticker"] == "AAPL"

    def test_loss_position_marked(self, tmp_path: Path):
        from trading_bot.runtime.dashboard import _load_closed_positions
        from trading_bot.config.settings import Settings

        db = _tmp_db(tmp_path)
        settings = Settings(app__state_db_path=str(db))

        order_rows = [
            {"id": "1", "ticker": "TSLA", "side": "BUY", "quantity": 5, "fill_price": 200.0, "filled_at": "2026-06-29T10:00:00", "pnl": 0},
            {"id": "2", "ticker": "TSLA", "side": "SELL", "quantity": 5, "fill_price": 180.0, "filled_at": "2026-06-29T14:00:00", "pnl": -100.0},
        ]

        with patch("trading_bot.runtime.dashboard.PortfolioLedger") as MockLedger:
            MockLedger.return_value.list_order_rows.return_value = order_rows
            result = _load_closed_positions(settings)

        assert result[0]["win"] is False
        assert result[0]["pnl"] == -100.0

    def test_uppercase_ticker_key(self, tmp_path: Path):
        """Ticker orders are grouped by uppercase key."""
        from trading_bot.runtime.dashboard import _load_closed_positions
        from trading_bot.config.settings import Settings

        db = _tmp_db(tmp_path)
        settings = Settings(app__state_db_path=str(db))

        order_rows = [
            {"id": "1", "ticker": "aapl", "side": "BUY", "quantity": 5, "fill_price": 100.0, "filled_at": "2026-06-29T10:00:00", "pnl": 0},
            {"id": "2", "ticker": "AAPL", "side": "SELL", "quantity": 5, "fill_price": 110.0, "filled_at": "2026-06-29T14:00:00", "pnl": 50.0},
        ]

        with patch("trading_bot.runtime.dashboard.PortfolioLedger") as MockLedger:
            MockLedger.return_value.list_order_rows.return_value = order_rows
            result = _load_closed_positions(settings)

        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"

    def test_multiple_buys_only_last_matched(self, tmp_path: Path):
        """If there are two buys before a sell, only the last buy is paired."""
        from trading_bot.runtime.dashboard import _load_closed_positions
        from trading_bot.config.settings import Settings

        db = _tmp_db(tmp_path)
        settings = Settings(app__state_db_path=str(db))

        order_rows = [
            {"id": "1", "ticker": "AAPL", "side": "BUY", "quantity": 5, "fill_price": 100.0, "filled_at": "2026-06-29T09:00:00", "pnl": 0},
            {"id": "2", "ticker": "AAPL", "side": "BUY", "quantity": 10, "fill_price": 102.0, "filled_at": "2026-06-29T09:30:00", "pnl": 0},
            {"id": "3", "ticker": "AAPL", "side": "SELL", "quantity": 10, "fill_price": 110.0, "filled_at": "2026-06-29T14:00:00", "pnl": 80.0},
        ]

        with patch("trading_bot.runtime.dashboard.PortfolioLedger") as MockLedger:
            MockLedger.return_value.list_order_rows.return_value = order_rows
            result = _load_closed_positions(settings)

        assert len(result) == 1
        # First buy (100.0) is paired with sell; quantity comes from sell order
        assert result[0]["entry_price"] == 100.0
        assert result[0]["quantity"] == 10

    def test_buy_after_sell_starts_new_pair(self, tmp_path: Path):
        """A BUY after a SELL starts a new potential pair."""
        from trading_bot.runtime.dashboard import _load_closed_positions
        from trading_bot.config.settings import Settings

        db = _tmp_db(tmp_path)
        settings = Settings(app__state_db_path=str(db))

        order_rows = [
            {"id": "1", "ticker": "AAPL", "side": "BUY", "quantity": 5, "fill_price": 100.0, "filled_at": "2026-06-28T10:00:00", "pnl": 0},
            {"id": "2", "ticker": "AAPL", "side": "SELL", "quantity": 5, "fill_price": 110.0, "filled_at": "2026-06-28T14:00:00", "pnl": 50.0},
            {"id": "3", "ticker": "AAPL", "side": "BUY", "quantity": 3, "fill_price": 105.0, "filled_at": "2026-06-29T10:00:00", "pnl": 0},
            {"id": "4", "ticker": "AAPL", "side": "SELL", "quantity": 3, "fill_price": 108.0, "filled_at": "2026-06-29T14:00:00", "pnl": 9.0},
        ]

        with patch("trading_bot.runtime.dashboard.PortfolioLedger") as MockLedger:
            MockLedger.return_value.list_order_rows.return_value = order_rows
            result = _load_closed_positions(settings)

        assert len(result) == 2
        # Most recent first
        assert result[0]["entry_price"] == 105.0
        assert result[1]["entry_price"] == 100.0


class TestClosedPositionsTableFromLedger:
    """_closed_positions_table_from_ledger renders HTML table."""

    def test_empty_list(self):
        from trading_bot.runtime.dashboard import _closed_positions_table_from_ledger
        result = _closed_positions_table_from_ledger([])
        assert "No closed positions yet" in result

    def test_single_position(self):
        from trading_bot.runtime.dashboard import _closed_positions_table_from_ledger
        positions = [
            {
                "ticker": "AAPL",
                "entry_date": "2026-06-29T10:00:00",
                "exit_date": "2026-06-29T14:00:00",
                "entry_price": 150.0,
                "exit_price": 155.0,
                "quantity": 10,
                "pnl": 50.0,
                "win": True,
            }
        ]
        result = _closed_positions_table_from_ledger(positions)
        assert "AAPL" in result
        assert "$150.00" in result
        assert "$155.00" in result
        assert "2026-06-29" in result
        assert "$50.00" in result
        assert "WIN" in result
        assert "<thead>" in result
        assert "<tbody>" in result

    def test_loss_position(self):
        from trading_bot.runtime.dashboard import _closed_positions_table_from_ledger
        positions = [
            {
                "ticker": "TSLA",
                "entry_date": "2026-06-29T10:00:00",
                "exit_date": "2026-06-29T14:00:00",
                "entry_price": 200.0,
                "exit_price": 180.0,
                "quantity": 5,
                "pnl": -100.0,
                "win": False,
            }
        ]
        result = _closed_positions_table_from_ledger(positions)
        assert "TSLA" in result
        assert "LOSS" in result
        assert "$-100.00" in result

    def test_missing_dates(self):
        from trading_bot.runtime.dashboard import _closed_positions_table_from_ledger
        positions = [
            {
                "ticker": "AAPL",
                "entry_price": 150.0,
                "exit_price": 155.0,
                "quantity": 10,
                "pnl": 50.0,
                "win": True,
            }
        ]
        result = _closed_positions_table_from_ledger(positions)
        assert "—" in result

    def test_headers(self):
        from trading_bot.runtime.dashboard import _closed_positions_table_from_ledger
        positions = [
            {
                "ticker": "AAPL",
                "entry_date": "2026-06-29T10:00:00",
                "exit_date": "2026-06-29T14:00:00",
                "entry_price": 150.0,
                "exit_price": 155.0,
                "quantity": 10,
                "pnl": 50.0,
                "win": True,
            }
        ]
        result = _closed_positions_table_from_ledger(positions)
        for h in ["Ticker", "Entry Date", "Exit Date", "Entry $", "Exit $", "Qty", "P&L", "Result"]:
            assert h in result

    def test_html_escaping(self):
        from trading_bot.runtime.dashboard import _closed_positions_table_from_ledger
        positions = [
            {
                "ticker": "A<A>PL",
                "entry_date": "2026-06-29T10:00:00",
                "exit_date": "2026-06-29T14:00:00",
                "entry_price": 150.0,
                "exit_price": 155.0,
                "quantity": 10,
                "pnl": 50.0,
                "win": True,
            }
        ]
        result = _closed_positions_table_from_ledger(positions)
        assert "A&lt;A&gt;PL" in result

    def test_zero_pnl_not_win(self):
        from trading_bot.runtime.dashboard import _closed_positions_table_from_ledger
        positions = [
            {
                "ticker": "AAPL",
                "entry_date": "2026-06-29T10:00:00",
                "exit_date": "2026-06-29T14:00:00",
                "entry_price": 150.0,
                "exit_price": 150.0,
                "quantity": 10,
                "pnl": 0.0,
                "win": False,
            }
        ]
        result = _closed_positions_table_from_ledger(positions)
        assert "LOSS" in result
