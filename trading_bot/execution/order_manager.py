from __future__ import annotations

from collections.abc import Mapping

from trading_bot.config.settings import RiskSettings
from trading_bot.execution.broker_base import BrokerAdapter
from trading_bot.execution.modes import ExecutionMode, require_paper_mode
from trading_bot.models.order import FillResult, OrderRequest
from trading_bot.models.signal import TradeSignal
from trading_bot.risk.risk_manager import evaluate_signal


def submit_signal_as_order(
    signal: TradeSignal,
    broker: BrokerAdapter,
    account_equity: float,
    open_tickers: set[str],
    risk_settings: RiskSettings | None = None,
    mode: ExecutionMode = ExecutionMode.PAPER,
) -> FillResult | None:
    require_paper_mode(mode)

    decision = evaluate_signal(
        signal=signal,
        account_equity=account_equity,
        open_tickers=open_tickers,
        risk_settings=risk_settings,
    )
    if not decision.approved:
        return None

    order = OrderRequest(
        ticker=signal.ticker,
        side="BUY",
        order_type="market",
        quantity=decision.position_size,
        submitted_at=signal.timestamp,
    )

    broker_cash = _extract_broker_cash(broker)
    estimated_total_cost = (signal.entry_price * decision.position_size) + getattr(
        broker, "fee_per_order", 0.0
    )
    if broker_cash is not None and broker_cash < estimated_total_cost:
        return None

    try:
        return broker.submit_order(order, market_price=signal.entry_price)
    except ValueError:
        return None


def _extract_broker_cash(broker: BrokerAdapter) -> float | None:
    value = getattr(broker, "cash", None)
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None
