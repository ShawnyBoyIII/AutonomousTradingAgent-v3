"""Tests for _load_open_positions_from_ledger and ledger fallback in snapshot."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.config.settings import AppSettings, Settings
from trading_bot.models.portfolio import PortfolioState, Position


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    app = AppSettings(
        state_db_path=str(tmp_path / "test.db"),
        portfolio_summary_path=str(tmp_path / "portfolio.json"),
        scan_results_path=str(tmp_path / "scan.json"),
        dashboard_summary_path=str(tmp_path / "dashboard.json"),
        backtest_summary_path=str(tmp_path / "backtest.json"),
        log_dir=str(tmp_path / "logs"),
        watchlist_path=str(tmp_path / "watchlist.json"),
    )
    return Settings(app=app)


@pytest.fixture()
def ledger_with_positions(settings: Settings) -> None:
    """Create a state DB with open positions."""
    import sqlite3

    db_path = Path(settings.app.state_db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE portfolio_state (
            id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL
        )
        """
    )
    state = PortfolioState(
        cash=8500.0,
        equity=9200.0,
        positions={
            "TNXP": Position(
                ticker="TNXP",
                quantity=152,
                average_cost=12.90,
                stop_loss=12.67,
                profit_target=13.36,
                entry_at=None,
                strategy_tag="v3-trend_following",
            ),
            "SOFI": Position(
                ticker="SOFI",
                quantity=107,
                average_cost=18.23,
                stop_loss=18.20,
                profit_target=18.29,
                entry_at=None,
                strategy_tag="v3-trend_following",
            ),
        },
        realized_pnl=-178.41,
        unrealized_pnl=0.0,
    )
    conn.execute(
        "INSERT INTO portfolio_state (id, payload) VALUES (1, ?)",
        (state.model_dump_json(),),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def empty_portfolio_json(settings: Settings) -> None:
    """Create a portfolio_summary.json with no positions."""
    p = Path(settings.app.portfolio_summary_path)
    p.write_text(json.dumps({"summary": {"positions": 0}, "positions": []}))


def test_load_open_positions_from_ledger(
    settings: Settings, ledger_with_positions: None
):
    """_load_open_positions_from_ledger returns positions from the state DB."""
    from trading_bot.runtime.dashboard import _load_open_positions_from_ledger

    positions = _load_open_positions_from_ledger(settings)
    assert len(positions) == 2

    tickers = {p["ticker"] for p in positions}
    assert tickers == {"TNXP", "SOFI"}

    tnxp = next(p for p in positions if p["ticker"] == "TNXP")
    assert tnxp["quantity"] == 152
    assert tnxp["average_cost"] == 12.90
    assert tnxp["stop_loss"] == 12.67
    assert tnxp["profit_target"] == 13.36
    assert tnxp["strategy_tag"] == "v3-trend_following"

    sofi = next(p for p in positions if p["ticker"] == "SOFI")
    assert sofi["quantity"] == 107
    assert sofi["average_cost"] == 18.23


def test_load_open_positions_from_ledger_empty_db(settings: Settings):
    """_load_open_positions_from_ledger returns [] when no portfolio_state exists."""
    from trading_bot.runtime.dashboard import _load_open_positions_from_ledger

    # DB file doesn't exist yet
    positions = _load_open_positions_from_ledger(settings)
    assert positions == []


def test_load_open_positions_from_ledger_no_positions(settings: Settings):
    """_load_open_positions_from_ledger returns [] when portfolio_state has no positions."""
    import sqlite3

    from trading_bot.runtime.dashboard import _load_open_positions_from_ledger

    db_path = Path(settings.app.state_db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE portfolio_state (
            id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL
        )
        """
    )
    from trading_bot.models.portfolio import PortfolioState

    state = PortfolioState(cash=10000.0, equity=10000.0, positions={})
    conn.execute(
        "INSERT INTO portfolio_state (id, payload) VALUES (1, ?)",
        (state.model_dump_json(),),
    )
    conn.commit()
    conn.close()

    positions = _load_open_positions_from_ledger(settings)
    assert positions == []


def test_snapshot_falls_back_to_ledger_open_positions(
    settings: Settings,
    ledger_with_positions: None,
    empty_portfolio_json: None,
):
    """Dashboard snapshot falls back to ledger when portfolio_summary has no positions."""
    import pathlib

    from trading_bot.runtime.dashboard import DashboardServer

    server = DashboardServer(settings)

    with patch.object(server, "_resolve_optional_deps") as mock_deps:
        from trading_bot.portfolio.ledger import PortfolioLedger
        from trading_bot.safety.kill_switch import is_trading_halted

        mock_deps.return_value = {
            "PortfolioLedger": PortfolioLedger,
            "is_trading_halted": is_trading_halted,
            "_pathlib": pathlib,
        }

        snapshot = server.snapshot()

    portfolio = snapshot["portfolio"]

    # Should have TNXP and SOFI from ledger fallback
    positions = portfolio.get("positions", [])
    assert isinstance(positions, list)
    assert len(positions) >= 2

    tickers = {p.get("ticker") for p in positions if isinstance(p, dict)}
    assert "TNXP" in tickers
    assert "SOFI" in tickers

    # Cash and equity should come from ledger
    assert portfolio.get("cash") == 8500.0
    assert portfolio.get("equity") == 9200.0
    assert portfolio.get("realized_pnl") == -178.41


def test_snapshot_does_not_override_existing_positions(
    settings: Settings,
    ledger_with_positions: None,
):
    """Dashboard snapshot does NOT fall back to ledger when portfolio_summary already has positions."""
    import pathlib

    from trading_bot.runtime.dashboard import DashboardServer

    # Write a portfolio_summary with QQQ and SPY
    p = Path(settings.app.portfolio_summary_path)
    p.write_text(
        json.dumps({
            "summary": {"positions": 2},
            "positions": [
                {
                    "ticker": "QQQ",
                    "quantity": 2,
                    "average_cost": 717.88,
                },
                {
                    "ticker": "SPY",
                    "quantity": 2,
                    "average_cost": 735.5,
                },
            ],
        })
    )

    server = DashboardServer(settings)

    with patch.object(server, "_resolve_optional_deps") as mock_deps:
        from trading_bot.portfolio.ledger import PortfolioLedger
        from trading_bot.safety.kill_switch import is_trading_halted

        mock_deps.return_value = {
            "PortfolioLedger": PortfolioLedger,
            "is_trading_halted": is_trading_halted,
            "_pathlib": pathlib,
        }

        snapshot = server.snapshot()

    portfolio = snapshot["portfolio"]
    positions = portfolio.get("positions", [])

    # Should have QQQ and SPY from portfolio_summary, NOT TNXP/SOFI from ledger
    tickers = {p.get("ticker") for p in positions if isinstance(p, dict)}
    assert "QQQ" in tickers
    assert "SPY" in tickers
    assert "TNXP" not in tickers
    assert "SOFI" not in tickers
