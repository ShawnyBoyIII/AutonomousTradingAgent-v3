from __future__ import annotations

from datetime import datetime
from typing import Any

from trading_bot.events.types import (
    Event,
    MarketBarEvent,
    OrderFillEvent,
    OrderRejectEvent,
    PortfolioStateEvent,
    PortfolioPnLEvent,
    RiskDecisionEvent,
    StrategySignalEvent,
)


class Cache:
    """State cache for the event-driven trading system.

    Maintains current portfolio state, positions, open orders, and
    recent market data. Updated by event handlers and queried by
    strategy/risk components.
    """

    def __init__(self) -> None:
        self.cash: float = 100_000.0
        self.equity: float = 100_000.0
        self.positions: dict[str, dict[str, Any]] = {}
        self.open_orders: dict[str, list[dict[str, Any]]] = {}
        self.fill_history: list[dict[str, Any]] = []
        self.recent_bars: dict[str, list[dict[str, Any]]] = {}
        self.signal_history: list[dict[str, Any]] = []
        self.risk_decisions: list[dict[str, Any]] = []
        self.realized_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.daily_pnl: float = 0.0
        self.last_update: datetime | None = None

    def update_from_state_event(self, event: PortfolioStateEvent) -> None:
        self.cash = event.cash
        self.equity = event.equity
        self.positions = dict(event.positions)
        self.realized_pnl = event.realized_pnl
        self.unrealized_pnl = event.unrealized_pnl
        self.last_update = event.timestamp

    def update_from_pnl_event(self, event: PortfolioPnLEvent) -> None:
        self.realized_pnl = event.realized_pnl
        self.unrealized_pnl = event.unrealized_pnl
        self.daily_pnl = event.daily_pnl
        self.last_update = event.timestamp

    def update_from_fill(self, event: OrderFillEvent) -> None:
        ticker = event.ticker
        if ticker not in self.positions:
            self.positions[ticker] = {
                "ticker": ticker,
                "quantity": 0,
                "average_cost": 0.0,
                "stop_loss": None,
                "profit_target": None,
            }

        pos = self.positions[ticker]
        old_qty = pos["quantity"]
        old_cost = pos["average_cost"]
        new_qty = old_qty + event.quantity
        if new_qty > 0:
            total_cost = old_qty * old_cost + event.quantity * event.fill_price
            pos["average_cost"] = total_cost / new_qty
        pos["quantity"] = new_qty

        self.fill_history.append({
            "order_id": event.order_id,
            "ticker": ticker,
            "side": event.side,
            "quantity": event.quantity,
            "fill_price": event.fill_price,
            "fees": event.fees,
            "timestamp": event.timestamp,
        })
        self.last_update = event.timestamp

    def update_from_reject(self, event: OrderRejectEvent) -> None:
        self.fill_history.append({
            "order_id": event.order_id,
            "ticker": event.ticker,
            "side": event.side,
            "status": "rejected",
            "reason": event.reason,
            "timestamp": event.timestamp,
        })
        self.last_update = event.timestamp

    def update_from_signal(self, event: StrategySignalEvent) -> None:
        self.signal_history.append({
            "ticker": event.ticker,
            "action": event.action,
            "confidence": event.confidence,
            "strategy_tag": event.strategy_tag,
            "timestamp": event.timestamp,
        })
        self.last_update = event.timestamp

    def update_from_risk_decision(self, event: RiskDecisionEvent) -> None:
        self.risk_decisions.append({
            "ticker": event.ticker,
            "approved": event.approved,
            "position_size": event.position_size,
            "reason": event.reason,
            "timestamp": event.timestamp,
        })
        self.last_update = event.timestamp

    def update_from_bar(self, event: MarketBarEvent) -> None:
        ticker = event.ticker
        if ticker not in self.recent_bars:
            self.recent_bars[ticker] = []
        bar = {
            "open": event.open,
            "high": event.high,
            "low": event.low,
            "close": event.close,
            "volume": event.volume,
            "timeframe": event.timeframe,
            "timestamp": event.timestamp,
        }
        self.recent_bars[ticker].append(bar)
        max_bars = 500
        if len(self.recent_bars[ticker]) > max_bars:
            self.recent_bars[ticker] = self.recent_bars[ticker][-max_bars:]
        self.last_update = event.timestamp

    def get_position(self, ticker: str) -> dict[str, Any] | None:
        return self.positions.get(ticker)

    def get_open_positions(self) -> list[str]:
        return [t for t, p in self.positions.items() if p.get("quantity", 0) > 0]

    def get_equity(self) -> float:
        return self.equity

    def get_cash(self) -> float:
        return self.cash

    def get_exposure(self) -> float:
        return sum(
            p.get("quantity", 0) * p.get("average_cost", 0.0)
            for p in self.positions.values()
        )

    def get_exposure_ratio(self) -> float:
        if self.equity <= 0:
            return 0.0
        return self.get_exposure() / self.equity

    def get_recent_bars(self, ticker: str, n: int = 100) -> list[dict[str, Any]]:
        bars = self.recent_bars.get(ticker, [])
        return bars[-n:]

    def get_recent_signals(self, ticker: str | None = None, n: int = 50) -> list[dict[str, Any]]:
        signals = self.signal_history
        if ticker:
            signals = [s for s in signals if s.get("ticker") == ticker]
        return signals[-n:]

    def get_fill_history(self, ticker: str | None = None, n: int = 100) -> list[dict[str, Any]]:
        fills = self.fill_history
        if ticker:
            fills = [f for f in fills if f.get("ticker") == ticker]
        return fills[-n:]

    def reset(self) -> None:
        self.cash = 100_000.0
        self.equity = 100_000.0
        self.positions.clear()
        self.open_orders.clear()
        self.fill_history.clear()
        self.recent_bars.clear()
        self.signal_history.clear()
        self.risk_decisions.clear()
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.daily_pnl = 0.0
        self.last_update = None
