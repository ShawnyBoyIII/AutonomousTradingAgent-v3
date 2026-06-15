from __future__ import annotations

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
    mode: ExecutionMode = ExecutionMode.PAPER,
) -> FillResult | None:
    require_paper_mode(mode)

    decision = evaluate_signal(
        signal=signal,
        account_equity=account_equity,
        open_tickers=open_tickers,
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
    return broker.submit_order(order, market_price=signal.entry_price)
