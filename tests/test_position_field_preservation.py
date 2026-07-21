"""Regression tests: Position field preservation across manage_positions.

Bug fix: continuous_loop previously reconstructed `Position` with explicit
field lists, dropping new fields like `entry_fees` and `partial_profit_taken`
back to their defaults. This caused SELL realised P&L to omit the BUY fee on
positions that had been round-tripped through stop-widening or highest_high
updates.

These tests pin the contract: any new Position field added must survive a
manage_positions round-trip without being silently reset to its default.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pandas as pd


def _make_frame(close: float, high: float, low: float) -> pd.DataFrame:
    return pd.DataFrame(
        {"close": [close], "high": [high], "low": [low], "volume": [1000]},
        index=pd.DatetimeIndex([datetime.now(tz=timezone.utc)]),
    )


def _patch_safety(monkeypatch) -> None:
    monkeypatch.setattr(
        "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
        lambda ledger: (True, ""),
    )
    monkeypatch.setattr(
        "trading_bot.safety.circuit_breaker.check_circuit_breakers",
        lambda ledger, settings: (True, ""),
    )


def test_stop_widening_preserves_entry_fees(monkeypatch, tmp_path):
    import trading_bot.runtime.continuous_loop as continuous_loop
    from trading_bot.config.settings import Settings
    from trading_bot.models.portfolio import PortfolioState, Position
    from trading_bot.portfolio.ledger import PortfolioLedger

    settings = Settings(
        app={"state_db_path": str(tmp_path / "state.db"), "log_dir": str(tmp_path)}
    )
    settings.session.eod_enabled = False
    settings.risk.min_stop_distance_pct = 3.0
    settings.risk.use_atr_sizing = False
    ledger = PortfolioLedger(tmp_path / "state.db")
    ledger.save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=99.0,
                    entry_fees=0.85,
                )
            },
        )
    )

    monkeypatch.setattr(
        continuous_loop.market_data,
        "fetch_and_validate_bars",
        lambda *a, **k: (_make_frame(105.0, 105.0, 104.0), types.SimpleNamespace(valid=True, reason="")),
    )
    _patch_safety(monkeypatch)

    continuous_loop._run_manage_positions_once(settings, ledger)

    position = ledger.load_portfolio_state().positions["AAPL"]
    # Pre-fix bug: entry_fees silently reset to 0.0 by reconstruction.
    assert position.entry_fees == 0.85, (
        f"entry_fees must survive stop-widening, got {position.entry_fees}"
    )
    assert position.stop_loss == 97.0


def test_highest_high_update_preserves_entry_fees_and_partial_flag(monkeypatch, tmp_path):
    import trading_bot.runtime.continuous_loop as continuous_loop
    from trading_bot.config.settings import Settings
    from trading_bot.models.portfolio import PortfolioState, Position
    from trading_bot.portfolio.ledger import PortfolioLedger

    settings = Settings(
        app={"state_db_path": str(tmp_path / "state.db"), "log_dir": str(tmp_path)}
    )
    settings.session.eod_enabled = False
    ledger = PortfolioLedger(tmp_path / "state.db")
    ledger.save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=98.0,
                    profit_target=110.0,
                    entry_fees=1.25,
                    partial_profit_taken=False,
                )
            },
        )
    )

    monkeypatch.setattr(
        continuous_loop.market_data,
        "fetch_and_validate_bars",
        lambda *a, **k: (_make_frame(108.0, 108.0, 107.0), types.SimpleNamespace(valid=True, reason="")),
    )
    _patch_safety(monkeypatch)

    continuous_loop._run_manage_positions_once(settings, ledger)

    position = ledger.load_portfolio_state().positions["AAPL"]
    assert position.entry_fees == 1.25, (
        f"entry_fees must survive highest_high update, got {position.entry_fees}"
    )
    assert position.partial_profit_taken is False
    assert position.highest_high == 108.0