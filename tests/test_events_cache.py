"""Tests for the event-driven system state cache."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_bot.events.cache import Cache
from trading_bot.events.types import (
    MarketBarEvent,
    OrderFillEvent,
    OrderRejectEvent,
    PortfolioPnLEvent,
    PortfolioStateEvent,
    RiskDecisionEvent,
    StrategySignalEvent,
)


class TestCacheInitialState:
    """Cache default state."""

    def test_default_cash(self):
        cache = Cache()
        assert cache.cash == 100_000.0

    def test_default_equity(self):
        cache = Cache()
        assert cache.equity == 100_000.0

    def test_default_empty_positions(self):
        cache = Cache()
        assert cache.positions == {}

    def test_default_empty_fill_history(self):
        cache = Cache()
        assert cache.fill_history == []

    def test_default_realized_pnl_zero(self):
        cache = Cache()
        assert cache.realized_pnl == 0.0

    def test_default_unrealized_pnl_zero(self):
        cache = Cache()
        assert cache.unrealized_pnl == 0.0

    def test_default_last_update_none(self):
        cache = Cache()
        assert cache.last_update is None


class TestStateEventUpdates:
    """PortfolioStateEvent handling."""

    def test_update_from_state_event(self):
        cache = Cache()
        now = datetime.now(timezone.utc)
        event = PortfolioStateEvent(
            cash=50_000.0,
            equity=75_000.0,
            positions={"AAPL": {"quantity": 10, "average_cost": 150.0}},
            realized_pnl=500.0,
            unrealized_pnl=-200.0,
            timestamp=now,
        )
        cache.update_from_state_event(event)
        assert cache.cash == 50_000.0
        assert cache.equity == 75_000.0
        assert cache.positions["AAPL"]["quantity"] == 10
        assert cache.realized_pnl == 500.0
        assert cache.unrealized_pnl == -200.0
        assert cache.last_update == now

    def test_state_event_overwrites_positions(self):
        cache = Cache()
        cache.positions = {"AAPL": {"quantity": 5}}
        event = PortfolioStateEvent(
            cash=100_000.0,
            equity=100_000.0,
            positions={"SPY": {"quantity": 20}},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
        )
        cache.update_from_state_event(event)
        assert "AAPL" not in cache.positions
        assert "SPY" in cache.positions


class TestPnLEventUpdates:
    """PortfolioPnLEvent handling."""

    def test_update_from_pnl_event(self):
        cache = Cache()
        now = datetime.now(timezone.utc)
        event = PortfolioPnLEvent(
            realized_pnl=1000.0,
            unrealized_pnl=-500.0,
            daily_pnl=250.0,
            total_return_pct=0.025,
            timestamp=now,
        )
        cache.update_from_pnl_event(event)
        assert cache.realized_pnl == 1000.0
        assert cache.unrealized_pnl == -500.0
        assert cache.daily_pnl == 250.0
        assert cache.last_update == now


class TestFillUpdates:
    """OrderFillEvent handling."""

    def test_fill_creates_new_position(self):
        cache = Cache()
        event = OrderFillEvent(
            order_id="ord-1",
            ticker="AAPL",
            quantity=10,
            fill_price=150.0,
            fees=2.50,
            side="BUY",
        )
        cache.update_from_fill(event)
        pos = cache.get_position("AAPL")
        assert pos is not None
        assert pos["quantity"] == 10
        assert pos["average_cost"] == 150.0

    def test_fill_updates_existing_position(self):
        cache = Cache()
        cache.update_from_fill(OrderFillEvent(
            order_id="ord-1", ticker="AAPL", quantity=10,
            fill_price=150.0, fees=0, side="BUY",
        ))
        cache.update_from_fill(OrderFillEvent(
            order_id="ord-2", ticker="AAPL", quantity=5,
            fill_price=160.0, fees=0, side="BUY",
        ))
        pos = cache.get_position("AAPL")
        assert pos["quantity"] == 15
        expected_cost = (10 * 150.0 + 5 * 160.0) / 15
        assert pos["average_cost"] == pytest.approx(expected_cost)

    def test_fill_recorded_in_history(self):
        cache = Cache()
        now = datetime.now(timezone.utc)
        event = OrderFillEvent(
            order_id="ord-1", ticker="AAPL", quantity=10,
            fill_price=150.0, fees=2.50, side="BUY", timestamp=now,
        )
        cache.update_from_fill(event)
        assert len(cache.fill_history) == 1
        assert cache.fill_history[0]["order_id"] == "ord-1"
        assert cache.fill_history[0]["fill_price"] == 150.0
        assert cache.fill_history[0]["fees"] == 2.50

    def test_fill_with_zero_quantity_no_position(self):
        cache = Cache()
        cache.update_from_fill(OrderFillEvent(
            order_id="ord-1", ticker="AAPL", quantity=0,
            fill_price=150.0, fees=0, side="BUY",
        ))
        pos = cache.get_position("AAPL")
        assert pos is not None
        assert pos["quantity"] == 0


class TestRejectUpdates:
    """OrderRejectEvent handling."""

    def test_reject_recorded_in_history(self):
        cache = Cache()
        now = datetime.now(timezone.utc)
        event = OrderRejectEvent(
            order_id="ord-1", ticker="AAPL",
            reason="insufficient_funds", side="BUY", timestamp=now,
        )
        cache.update_from_reject(event)
        assert len(cache.fill_history) == 1
        assert cache.fill_history[0]["status"] == "rejected"
        assert cache.fill_history[0]["reason"] == "insufficient_funds"


class TestSignalUpdates:
    """StrategySignalEvent handling."""

    def test_signal_recorded_in_history(self):
        cache = Cache()
        now = datetime.now(timezone.utc)
        event = StrategySignalEvent(
            ticker="AAPL", action="BUY", confidence=0.85,
            strategy_tag="v3-trend_following", timestamp=now,
        )
        cache.update_from_signal(event)
        assert len(cache.signal_history) == 1
        assert cache.signal_history[0]["ticker"] == "AAPL"
        assert cache.signal_history[0]["confidence"] == 0.85

    def test_get_recent_signals_for_ticker(self):
        cache = Cache()
        cache.update_from_signal(StrategySignalEvent(ticker="AAPL", action="BUY"))
        cache.update_from_signal(StrategySignalEvent(ticker="SPY", action="HOLD"))
        cache.update_from_signal(StrategySignalEvent(ticker="AAPL", action="SELL"))
        aapl_signals = cache.get_recent_signals(ticker="AAPL", n=50)
        assert len(aapl_signals) == 2
        assert all(s["ticker"] == "AAPL" for s in aapl_signals)

    def test_get_recent_signals_all_when_no_ticker(self):
        cache = Cache()
        cache.update_from_signal(StrategySignalEvent(ticker="AAPL"))
        cache.update_from_signal(StrategySignalEvent(ticker="SPY"))
        signals = cache.get_recent_signals(n=50)
        assert len(signals) == 2


class TestRiskDecisionUpdates:
    """RiskDecisionEvent handling."""

    def test_risk_decision_recorded(self):
        cache = Cache()
        event = RiskDecisionEvent(
            ticker="AAPL", approved=True, position_size=100,
            dollar_risk=500.0, reason="high_confluence",
        )
        cache.update_from_risk_decision(event)
        assert len(cache.risk_decisions) == 1
        assert cache.risk_decisions[0]["approved"] is True
        assert cache.risk_decisions[0]["position_size"] == 100


class TestBarUpdates:
    """MarketBarEvent handling."""

    def test_bar_added_to_recent_bars(self):
        cache = Cache()
        now = datetime.now(timezone.utc)
        event = MarketBarEvent(
            ticker="AAPL", open=150.0, high=152.0, low=149.0,
            close=151.0, volume=1_000_000, timeframe="1d", timestamp=now,
        )
        cache.update_from_bar(event)
        bars = cache.get_recent_bars("AAPL")
        assert len(bars) == 1
        assert bars[0]["close"] == 151.0

    def test_bars_capped_at_500(self):
        cache = Cache()
        for i in range(510):
            cache.update_from_bar(MarketBarEvent(
                ticker="AAPL", open=150.0, high=152.0, low=149.0,
                close=float(150 + i), volume=1_000_000, timeframe="1d",
            ))
        # Check internal storage directly since get_recent_bars defaults to n=100
        assert len(cache.recent_bars["AAPL"]) == 500

    def test_get_recent_bars_limited(self):
        cache = Cache()
        for i in range(100):
            cache.update_from_bar(MarketBarEvent(
                ticker="AAPL", open=150.0, high=152.0, low=149.0,
                close=float(150 + i), volume=1_000_000, timeframe="1d",
            ))
        bars = cache.get_recent_bars("AAPL", n=10)
        assert len(bars) == 10

    def test_bars_for_unknown_ticker_empty(self):
        cache = Cache()
        bars = cache.get_recent_bars("UNKNOWN")
        assert bars == []


class TestQueryMethods:
    """Position and equity query methods."""

    def test_get_position_returns_none_for_missing(self):
        cache = Cache()
        assert cache.get_position("AAPL") is None

    def test_get_open_positions(self):
        cache = Cache()
        cache.positions = {
            "AAPL": {"quantity": 10},
            "SPY": {"quantity": 0},
            "MSFT": {"quantity": 5},
        }
        open_positions = cache.get_open_positions()
        assert set(open_positions) == {"AAPL", "MSFT"}

    def test_get_equity(self):
        cache = Cache()
        cache.equity = 125_000.0
        assert cache.get_equity() == 125_000.0

    def test_get_cash(self):
        cache = Cache()
        cache.cash = 75_000.0
        assert cache.get_cash() == 75_000.0

    def test_get_exposure(self):
        cache = Cache()
        cache.positions = {
            "AAPL": {"quantity": 10, "average_cost": 150.0},
            "SPY": {"quantity": 5, "average_cost": 400.0},
        }
        # 10 * 150 + 5 * 400 = 1500 + 2000 = 3500
        assert cache.get_exposure() == 3500.0

    def test_get_exposure_ratio(self):
        cache = Cache()
        cache.positions = {
            "AAPL": {"quantity": 10, "average_cost": 150.0},
        }
        cache.equity = 100_000.0
        assert cache.get_exposure_ratio() == pytest.approx(0.015)

    def test_get_exposure_ratio_zero_equity(self):
        cache = Cache()
        cache.equity = 0.0
        assert cache.get_exposure_ratio() == 0.0

    def test_get_fill_history_for_ticker(self):
        cache = Cache()
        cache.update_from_fill(OrderFillEvent(ticker="AAPL", quantity=10, fill_price=150.0))
        cache.update_from_fill(OrderFillEvent(ticker="SPY", quantity=5, fill_price=400.0))
        aapl_fills = cache.get_fill_history(ticker="AAPL")
        assert len(aapl_fills) == 1
        assert aapl_fills[0]["ticker"] == "AAPL"


class TestReset:
    """Cache reset functionality."""

    def test_reset_clears_all_state(self):
        cache = Cache()
        cache.cash = 50_000.0
        cache.positions = {"AAPL": {"quantity": 10}}
        cache.fill_history.append({"order_id": "ord-1"})
        cache.signal_history.append({"ticker": "AAPL"})
        cache.risk_decisions.append({"ticker": "AAPL"})
        cache.recent_bars["AAPL"] = [{"close": 150.0}]
        cache.realized_pnl = 500.0
        cache.last_update = datetime.now(timezone.utc)

        cache.reset()

        assert cache.cash == 100_000.0
        assert cache.equity == 100_000.0
        assert cache.positions == {}
        assert cache.open_orders == {}
        assert cache.fill_history == []
        assert cache.recent_bars == {}
        assert cache.signal_history == []
        assert cache.risk_decisions == []
        assert cache.realized_pnl == 0.0
        assert cache.unrealized_pnl == 0.0
        assert cache.daily_pnl == 0.0
        assert cache.last_update is None
