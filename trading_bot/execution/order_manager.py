from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from trading_bot.config.settings import RiskSettings
from trading_bot.execution.broker_base import BrokerAdapter
from trading_bot.execution.fills import apply_slippage
from trading_bot.execution.modes import ExecutionMode, require_paper_mode
from trading_bot.models.order import FillResult, OrderRequest
from trading_bot.models.signal import TradeSignal
from trading_bot.risk.risk_manager import evaluate_signal

if TYPE_CHECKING:
    from trading_bot.strategy.counter_thesis import CounterThesisResult


def submit_signal_as_order(
    signal: TradeSignal,
    broker: BrokerAdapter,
    account_equity: float,
    open_tickers: set[str],
    portfolio_heat_pct: float = 0.0,
    atr: float | None = None,
    risk_settings: RiskSettings | None = None,
    counter_thesis: "CounterThesisResult | None" = None,
    mode: ExecutionMode = ExecutionMode.PAPER,
    position_size_override: int | None = None,
) -> FillResult | None:
    require_paper_mode(mode)

    decision = evaluate_signal(
        signal=signal,
        account_equity=account_equity,
        open_tickers=open_tickers,
        portfolio_heat_pct=portfolio_heat_pct,
        atr=atr,
        risk_settings=risk_settings,
        counter_thesis=counter_thesis,
    )
    if not decision.approved:
        return None

    quantity = (
        min(position_size_override, decision.position_size)
        if position_size_override is not None
        else decision.position_size
    )
    order = OrderRequest(
        ticker=signal.ticker,
        side="BUY",
        order_type="market",
        quantity=quantity,
        submitted_at=signal.timestamp,
    )

    broker_cash = _extract_broker_cash(broker)
    estimated_fill_price = _estimate_broker_fill_price(
        broker,
        order,
        signal.entry_price,
    )
    estimated_total_cost = (estimated_fill_price * quantity) + getattr(
        broker, "fee_per_order", 0.0
    )
    if broker_cash is not None and broker_cash < estimated_total_cost:
        return None

    try:
        return broker.submit_order(order, market_price=signal.entry_price)
    except ValueError:
        return None


def _estimate_broker_fill_price(
    broker: BrokerAdapter,
    order: OrderRequest,
    market_price: float,
) -> float:
    estimator = getattr(broker, "estimate_fill_price", None)
    if callable(estimator):
        return float(estimator(order, market_price))
    return apply_slippage(
        market_price,
        getattr(broker, "slippage_bps", 0),
        order.side,
    )


def _extract_broker_cash(broker: BrokerAdapter) -> float | None:
    value = getattr(broker, "cash", None)
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None
