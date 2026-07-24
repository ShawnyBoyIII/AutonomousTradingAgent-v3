from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EventType(str, Enum):
    MARKET_TICK = "MARKET_TICK"
    MARKET_BAR = "MARKET_BAR"
    MARKET_SNAPSHOT = "MARKET_SNAPSHOT"
    STRATEGY_SIGNAL = "STRATEGY_SIGNAL"
    STRATEGY_REGIME = "STRATEGY_REGIME"
    RISK_DECISION = "RISK_DECISION"
    RISK_HEAT = "RISK_HEAT"
    ORDER_REQUEST = "ORDER_REQUEST"
    ORDER_FILL = "ORDER_FILL"
    ORDER_REJECT = "ORDER_REJECT"
    ORDER_CANCEL = "ORDER_CANCEL"
    ORDER_PARTIAL_FILL = "ORDER_PARTIAL_FILL"
    EXECUTION_FILL = "EXECUTION_FILL"
    EXECUTION_CANCEL = "EXECUTION_CANCEL"
    PORTFOLIO_STATE = "PORTFOLIO_STATE"
    PORTFOLIO_EQUITY = "PORTFOLIO_EQUITY"
    PORTFOLIO_PNL = "PORTFOLIO_PNL"
    MONITORING_LATENCY = "MONITORING_LATENCY"
    COUNTER_THESIS = "COUNTER_THESIS"
    SYSTEM_TICK = "SYSTEM_TICK"
    SYSTEM_HEARTBEAT = "SYSTEM_HEARTBEAT"


def _now() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc)


class Event(BaseModel):
    event_type: str
    timestamp: datetime = Field(default_factory=_now)
    source: str = ""

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Event):
            return NotImplemented
        return self.timestamp < other.timestamp


class MarketTickEvent(Event):
    event_type: str = EventType.MARKET_TICK
    ticker: str = ""
    price: float = 0.0
    volume: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    exchange: str = ""


class MarketBarEvent(Event):
    event_type: str = EventType.MARKET_BAR
    ticker: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    timeframe: str = "1m"
    exchange: str = ""


class MarketSnapshotEvent(Event):
    event_type: str = EventType.MARKET_SNAPSHOT
    ticker: str = ""
    prices: dict[str, float] = Field(default_factory=dict)
    volumes: dict[str, float] = Field(default_factory=dict)


class StrategySignalEvent(Event):
    event_type: str = EventType.STRATEGY_SIGNAL
    ticker: str = ""
    action: str = "HOLD"
    entry_price: float = 0.0
    stop_loss: float = 0.0
    profit_target: float = 0.0
    risk_reward_ratio: float = 0.0
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    strategy_tag: str = ""
    timeframe: str = "intraday"


class StrategyRegimeEvent(Event):
    event_type: str = EventType.STRATEGY_REGIME
    ticker: str = ""
    regime: str = ""
    trend_strength: float = 0.0
    volatility_regime: str = ""


class RiskDecisionEvent(Event):
    event_type: str = EventType.RISK_DECISION
    ticker: str = ""
    approved: bool = False
    position_size: int = 0
    dollar_risk: float = 0.0
    reason: str = ""
    portfolio_heat_pct: float = 0.0
    atr: float | None = None


class RiskHeatEvent(Event):
    event_type: str = EventType.RISK_HEAT
    heat_pct: float = 0.0
    equity: float = 0.0
    unrealized_pnl: float = 0.0
    blocked: bool = False


class OrderRequestEvent(Event):
    event_type: str = EventType.ORDER_REQUEST
    order_id: str = ""
    ticker: str = ""
    side: str = "BUY"
    order_type: str = "market"
    quantity: int = 0
    limit_price: float | None = None
    stop_price: float | None = None
    strategy_tag: str = ""


class OrderFillEvent(Event):
    event_type: str = EventType.ORDER_FILL
    order_id: str = ""
    ticker: str = ""
    quantity: int = 0
    fill_price: float = 0.0
    fees: float = 0.0
    side: str = "BUY"


class OrderRejectEvent(Event):
    event_type: str = EventType.ORDER_REJECT
    order_id: str = ""
    ticker: str = ""
    reason: str = ""
    side: str = "BUY"


class OrderCancelEvent(Event):
    event_type: str = EventType.ORDER_CANCEL
    order_id: str = ""
    ticker: str = ""
    reason: str = ""
    side: str = "SELL"


class OrderPartialFillEvent(Event):
    event_type: str = EventType.ORDER_PARTIAL_FILL
    order_id: str = ""
    ticker: str = ""
    filled_quantity: int = 0
    fill_price: float = 0.0
    remaining_quantity: int = 0
    side: str = "BUY"


class ExecutionFillEvent(Event):
    event_type: str = EventType.EXECUTION_FILL
    execution_id: str = ""
    ticker: str = ""
    quantity: int = 0
    fill_price: float = 0.0
    fees: float = 0.0
    side: str = "BUY"
    venue: str = ""


class ExecutionCancelEvent(Event):
    event_type: str = EventType.EXECUTION_CANCEL
    execution_id: str = ""
    ticker: str = ""
    reason: str = ""
    venue: str = ""


class PortfolioStateEvent(Event):
    event_type: str = EventType.PORTFOLIO_STATE
    cash: float = 0.0
    equity: float = 0.0
    positions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


class PortfolioEquityEvent(Event):
    event_type: str = EventType.PORTFOLIO_EQUITY
    equity: float = 0.0
    cash: float = 0.0
    timestamp: datetime = Field(default_factory=_now)


class PortfolioPnLEvent(Event):
    event_type: str = EventType.PORTFOLIO_PNL
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    daily_pnl: float = 0.0
    total_return_pct: float = 0.0


class MonitoringLatencyEvent(Event):
    event_type: str = EventType.MONITORING_LATENCY
    component: str = ""
    latency_ms: float = 0.0
    threshold_ms: float = 0.0
    exceeded: bool = False


class CounterThesisEvent(Event):
    event_type: str = EventType.COUNTER_THESIS
    ticker: str = ""
    overall_severity: str = ""
    findings: list[str] = Field(default_factory=list)
    block_trade: bool = False
    confidence_multiplier: float = 1.0


class SystemTickEvent(Event):
    event_type: str = EventType.SYSTEM_TICK
    tick: int = 0
    wall_clock: datetime = Field(default_factory=_now)


class SystemHeartbeatEvent(Event):
    event_type: str = EventType.SYSTEM_HEARTBEAT
    uptime_seconds: float = 0.0
    events_processed: int = 0
    queue_depth: int = 0
