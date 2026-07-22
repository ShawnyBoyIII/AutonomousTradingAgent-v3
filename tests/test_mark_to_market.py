"""TDD: MarkToMarket helper for PortfolioState equity and unrealized P&L."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.runtime.mark_to_market import mark_to_market


def _pos(ticker: str, qty: int, avg: float) -> Position:
    return Position(
        ticker=ticker,
        quantity=qty,
        average_cost=avg,
        stop_loss=avg * 0.95,
        profit_target=avg * 1.05,
        entry_at=datetime(2026, 7, 21, 13, 0, tzinfo=timezone.utc),
    )


def test_no_positions_keeps_cash_as_equity() -> None:
    state = PortfolioState(cash=100_000.0, equity=100_000.0)
    out = mark_to_market(state, prices={})
    assert out.equity == 100_000.0
    assert out.unrealized_pnl == 0.0
    assert out.cash == 100_000.0


def test_position_values_at_last_close() -> None:
    state = PortfolioState(
        cash=80_000.0,
        equity=100_000.0,
        positions={"AAPL": _pos("AAPL", 10, 200.0)},
    )
    out = mark_to_market(state, prices={"AAPL": 210.0})
    assert out.equity == 80_000.0 + 10 * 210.0
    assert out.unrealized_pnl == 10 * (210.0 - 200.0)


def test_negative_unrealized_when_price_below_cost() -> None:
    state = PortfolioState(
        cash=80_000.0,
        equity=100_000.0,
        positions={"AAPL": _pos("AAPL", 10, 200.0)},
    )
    out = mark_to_market(state, prices={"AAPL": 190.0})
    assert out.unrealized_pnl == -100.0
    assert out.equity == 80_000.0 + 10 * 190.0


def test_missing_price_falls_back_to_average_cost() -> None:
    state = PortfolioState(
        cash=50_000.0,
        equity=100_000.0,
        positions={"AAPL": _pos("AAPL", 10, 200.0), "MSFT": _pos("MSFT", 5, 300.0)},
    )
    out = mark_to_market(state, prices={"AAPL": 210.0})
    assert out.equity == 50_000.0 + 10 * 210.0 + 5 * 300.0
    assert out.unrealized_pnl == 10 * 10.0


def test_zero_quantity_position_is_skipped() -> None:
    state = PortfolioState(
        cash=50_000.0,
        equity=100_000.0,
        positions={"AAPL": _pos("AAPL", 0, 200.0)},
    )
    out = mark_to_market(state, prices={"AAPL": 250.0})
    assert out.unrealized_pnl == 0.0


def test_cash_and_realized_pnl_preserved() -> None:
    state = PortfolioState(
        cash=12_345.67,
        equity=12_345.67,
        realized_pnl=-123.45,
        positions={},
    )
    out = mark_to_market(state, prices={})
    assert out.cash == 12_345.67
    assert out.realized_pnl == -123.45
