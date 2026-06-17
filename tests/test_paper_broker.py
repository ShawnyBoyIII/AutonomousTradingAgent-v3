from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from trading_bot.config.settings import RiskSettings
from trading_bot.execution.order_manager import submit_signal_as_order
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.execution.modes import ExecutionMode
from trading_bot.models.order import FillResult
from trading_bot.models.order import OrderRequest
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.models.signal import TradeSignal
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.portfolio.performance import compute_unrealized_pnl


def test_market_buy_updates_cash_and_position() -> None:
    broker = PaperBroker(starting_cash=10000, fee_per_order=1.0, slippage_bps=0)
    order = OrderRequest(
        ticker="AAPL",
        side="BUY",
        order_type="market",
        quantity=10,
        submitted_at=datetime.now(),
    )

    fill = broker.submit_order(order, market_price=100.0)

    assert fill.fill_price == 100.0
    assert broker.cash == 8999.0
    assert broker.positions["AAPL"] == 10


def test_market_buy_rejects_insufficient_cash_without_mutating_state() -> None:
    broker = PaperBroker(starting_cash=100.0, fee_per_order=1.0, slippage_bps=0)
    order = OrderRequest(
        ticker="AAPL",
        side="BUY",
        order_type="market",
        quantity=2,
        submitted_at=datetime.now(),
    )

    with pytest.raises(ValueError, match="insufficient cash"):
        broker.submit_order(order, market_price=100.0)

    assert broker.cash == 100.0
    assert broker.positions == {}


def test_market_sell_rejects_insufficient_position_without_mutating_state() -> None:
    broker = PaperBroker(starting_cash=1000.0, fee_per_order=1.0, slippage_bps=0)
    order = OrderRequest(
        ticker="AAPL",
        side="SELL",
        order_type="market",
        quantity=1,
        submitted_at=datetime.now(),
    )

    with pytest.raises(ValueError, match="insufficient position"):
        broker.submit_order(order, market_price=100.0)

    assert broker.cash == 1000.0
    assert broker.positions == {}


def test_market_order_rejects_non_positive_fill_price_without_mutating_state() -> None:
    broker = PaperBroker(starting_cash=1000.0, fee_per_order=1.0, slippage_bps=0)
    order = OrderRequest(
        ticker="AAPL",
        side="BUY",
        order_type="market",
        quantity=1,
        submitted_at=datetime.now(),
    )

    with pytest.raises(ValueError, match="fill price must be positive"):
        broker.submit_order(order, market_price=0.0)

    assert broker.cash == 1000.0
    assert broker.positions == {}


def test_market_sell_updates_cash_and_position() -> None:
    broker = PaperBroker(starting_cash=1000.0, fee_per_order=1.0, slippage_bps=0)
    broker.positions["AAPL"] = 5
    order = OrderRequest(
        ticker="AAPL",
        side="SELL",
        order_type="market",
        quantity=2,
        submitted_at=datetime.now(),
    )

    fill = broker.submit_order(order, market_price=100.0)

    assert fill.fill_price == 100.0
    assert broker.cash == 1199.0
    assert broker.positions["AAPL"] == 3


def test_ledger_initializes_sqlite_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(db_path)

    ledger.initialize()

    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(orders)")
        ]

    assert columns == ["id", "ticker", "side", "quantity", "fill_price", "fees", "filled_at"]


def test_ledger_round_trips_portfolio_state(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(db_path)
    state = PortfolioState(
        cash=12_500.0,
        equity=13_000.0,
        positions={
            "AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0),
        },
    )

    ledger.save_portfolio_state(state)

    loaded = ledger.load_portfolio_state()

    assert loaded == state


def test_ledger_ensure_portfolio_state_creates_default_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(db_path)

    state = ledger.ensure_portfolio_state(starting_cash=15_000.0)
    loaded = ledger.load_portfolio_state()

    assert state.cash == 15_000.0
    assert state.equity == 15_000.0
    assert loaded == state


def test_ledger_records_fill_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(db_path)
    fill = FillResult(
        order_id="order-123",
        ticker="AAPL",
        quantity=10,
        fill_price=101.25,
        fees=1.0,
        filled_at=datetime(2026, 6, 13, 10, 0, 0),
    )

    ledger.record_fill(fill, side="BUY")

    rows = ledger.list_order_rows()

    assert rows == [
        {
            "id": "order-123",
            "ticker": "AAPL",
            "side": "BUY",
            "quantity": 10,
            "fill_price": 101.25,
            "fees": 1.0,
            "filled_at": "2026-06-13T10:00:00",
        }
    ]


def test_compute_unrealized_pnl_returns_expected_gain() -> None:
    assert compute_unrealized_pnl(10, 100.0, 112.5) == 125.0


def test_submit_signal_as_order_returns_fill_for_approved_trade() -> None:
    broker = PaperBroker(starting_cash=10001, fee_per_order=1.0, slippage_bps=0)
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=102.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(),
    )

    fill = submit_signal_as_order(
        signal,
        broker,
        account_equity=10000,
        open_tickers=set(),
    )

    assert fill is not None
    assert fill.ticker == "AAPL"
    assert fill.quantity == 100
    assert fill.fill_price == 100.0


def test_submit_signal_as_order_returns_none_for_rejected_signal() -> None:
    broker = PaperBroker(starting_cash=10001, fee_per_order=1.0, slippage_bps=0)
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.5,
        profit_target=100.5,
        risk_reward_ratio=1.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(),
    )

    fill = submit_signal_as_order(
        signal,
        broker,
        account_equity=10000,
        open_tickers=set(),
    )

    assert fill is None


def test_submit_signal_as_order_returns_none_when_broker_cash_is_insufficient() -> None:
    broker = PaperBroker(starting_cash=5000, fee_per_order=1.0, slippage_bps=0)
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=102.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(),
    )

    fill = submit_signal_as_order(
        signal,
        broker,
        account_equity=10000,
        open_tickers=set(),
    )

    assert fill is None
    assert broker.cash == 5000


def test_submit_signal_as_order_enforces_paper_mode() -> None:
    broker = PaperBroker(starting_cash=10001, fee_per_order=1.0, slippage_bps=0)
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=102.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(),
    )

    with pytest.raises(RuntimeError):
        submit_signal_as_order(
            signal,
            broker,
            account_equity=10000,
            open_tickers=set(),
            mode=ExecutionMode.LIVE,
        )


def test_submit_signal_as_order_uses_configured_risk_settings() -> None:
    broker = PaperBroker(starting_cash=20001, fee_per_order=1.0, slippage_bps=0)
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=101.5,
        risk_reward_ratio=1.5,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(),
    )

    fill = submit_signal_as_order(
        signal,
        broker,
        account_equity=10000,
        open_tickers=set(),
        risk_settings=RiskSettings(
            max_risk_per_trade_pct=0.02,
            max_daily_risk_pct=0.03,
            max_ticker_allocation_pct=0.20,
            min_reward_risk_ratio=1.5,
        ),
    )

    assert fill is not None
    assert fill.quantity == 200
