"""Regression test for the find #4 / #5 audit issue: fill persistence
swallowing exceptions silently. After the fix, fill_sell_position must
raise FillTransactionError if ledger.record_fill or _sell_sql_persist
fail, rather than continuing to mutate broker/portfolio state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_bot.config.settings import AppSettings, Settings
from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.runtime.fill_transaction import FillTransactionError
from trading_bot.runtime.position_exit import fill_sell_position


class _BrokerStub:
    def __init__(self, fill_price: float, fill_quantity: int, cash: float, positions: dict[str, int]) -> None:
        self._fill_price = fill_price
        self._fill_quantity = fill_quantity
        self.cash = cash
        self.positions = positions

    def submit_order(self, order, market_price: float) -> FillResult:
        return FillResult(
            order_id=f"sell-{order.ticker.lower()}",
            ticker=order.ticker,
            quantity=self._fill_quantity,
            fill_price=self._fill_price,
            fees=1.0,
            filled_at=datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc),
        )


class _LedgerFailing:
    def record_fill(self, fill, side, realized_pnl=0.0, strategy_tag="") -> None:
        raise RuntimeError("simulated DB failure")

    def save_portfolio_state(self, state) -> None:
        raise AssertionError("save_portfolio_state must not be called when record_fill fails")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app=AppSettings(
            log_dir=str(tmp_path / "logs"),
            state_db_path=str(tmp_path / "state.db"),
        )
    )


def _position() -> Position:
    return Position(
        ticker="AAPL",
        quantity=10,
        average_cost=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        entry_at=datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc),
        strategy_tag="v3-trend_following",
    )


def test_fill_sell_position_raises_when_ledger_record_fill_fails(tmp_path: Path) -> None:
    """When ledger.record_fill fails, fill_sell_position must raise
    FillTransactionError rather than continuing to mutate portfolio state.
    """
    settings = _settings(tmp_path)
    ledger = _LedgerFailing()
    broker = _BrokerStub(
        fill_price=110.0,
        fill_quantity=10,
        cash=1099.0,
        positions={},
    )
    state = PortfolioState(cash=0.0, equity=1000.0, positions={"AAPL": _position()})

    with pytest.raises(FillTransactionError):
        fill_sell_position(
            ticker="AAPL",
            position=_position(),
            reason="profit_target",
            submitted_at=datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc),
            last_price=110.0,
            broker=broker,
            ledger=ledger,
            state=state,
            log_path=tmp_path / "logs" / "decision-log.jsonl",
            settings=settings,
        )


class _LedgerSpy:
    def record_fill(self, fill, side, realized_pnl=0.0, strategy_tag="") -> None:
        pass

    def save_portfolio_state(self, state) -> None:
        pass

    def record_equity_snapshot(self, state, timestamp) -> None:
        pass
