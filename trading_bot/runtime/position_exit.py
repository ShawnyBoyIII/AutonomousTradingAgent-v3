from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from trading_bot.models.order import OrderRequest
from trading_bot.models.portfolio import PortfolioState

if TYPE_CHECKING:
    from trading_bot.learning.experiments.runtime_canary import (
        RuntimeCanaryContext,
    )

logger = logging.getLogger(__name__)


def fill_sell_position(
    ticker: str,
    position,
    reason: str,
    submitted_at: datetime,
    last_price: float,
    broker,
    ledger,
    state: PortfolioState,
    log_path,
    quantity: int | None = None,
    close_db_position: bool = True,
    mark_exit_timestamp: bool = True,
    exit_rsi: float | None = None,
    exit_atr: float | None = None,
    hold_duration_minutes: float | None = None,
    exit_regime: str | None = None,
    exit_strategy: str | None = None,
    exit_reason: str | None = None,
    settings=None,
    runtime_canary: "RuntimeCanaryContext | None" = None,
) -> tuple[PortfolioState, dict[str, object], str]:
    """Submit a market SELL order, record the fill, and update portfolio state."""
    fill_quantity = quantity if quantity is not None else position.quantity
    fill = broker.submit_order(
        OrderRequest(
            ticker=ticker,
            side="SELL",
            order_type="market",
            quantity=fill_quantity,
            submitted_at=submitted_at,
        ),
        market_price=last_price,
    )
    entry_fee_share = (
        position.entry_fees * fill.quantity / position.quantity
        if position.quantity > 0
        else 0.0
    )
    realized_pnl = (
        (fill.fill_price - position.average_cost) * fill.quantity
        - fill.fees
        - entry_fee_share
    )
    from trading_bot.runtime.fill_transaction import (
        FillTransaction,
        FillTransactionError,
    )

    def _sell_sql_persist(**ctx):
        from trading_bot.db.models import Trade
        from trading_bot.db.repositories import close_position, update_trade_exit
        from trading_bot.db.session import get_session, init_db, make_session_factory
        from sqlalchemy import select

        nonlocal_settings = settings
        if nonlocal_settings is None:
            from trading_bot.cli.app import load_settings

            nonlocal_settings = load_settings()
        engine = init_db(nonlocal_settings)
        session_factory = make_session_factory(engine)
        session = get_session(session_factory)
        try:
            trade = session.execute(
                select(Trade).where(
                    Trade.ticker == ticker.upper(),
                    Trade.status == "FILLED",
                ).order_by(Trade.filled_at.desc())
            ).scalars().first()
            if trade and close_db_position:
                try:
                    update_trade_exit(
                        session=session,
                        trade_id=trade.id,
                        exit_price=fill.fill_price,
                        exit_fees=fill.fees,
                        pnl=realized_pnl,
                        exit_rsi=exit_rsi,
                        exit_atr=exit_atr,
                        hold_duration_minutes=hold_duration_minutes,
                        exit_regime=exit_regime,
                        exit_strategy=exit_strategy,
                        exit_reason=exit_reason,
                    )
                except ValueError:
                    logger.warning("update_trade_exit skipped: trade_id=%s not found", trade.id)
            if close_db_position:
                close_position(session, ticker.upper())
        finally:
            session.close()
            engine.dispose()

    sell_tx = FillTransaction()
    sell_tx.register(
        lambda fill, side, **ctx: ledger.record_fill(
            fill,
            side="SELL",
            realized_pnl=realized_pnl,
            strategy_tag=position.strategy_tag,
        )
    )
    sell_tx.register(_sell_sql_persist)
    try:
        sell_tx.run(fill=fill, side="SELL")
    except FillTransactionError as exc:
        logger.exception("SELL fill transaction failed for %s", ticker)

    if runtime_canary is not None:
        from trading_bot.learning.experiments.runtime_canary import (
            RuntimeCanaryContext,
        )

        if isinstance(runtime_canary, RuntimeCanaryContext):
            runtime_canary.record_exit(
                ticker=fill.ticker,
                candidate_quantity=fill.quantity,
                fill_price=fill.fill_price,
                fees=fill.fees,
                session_date=(
                    fill.filled_at.date().isoformat()
                    if getattr(fill.filled_at, "date", None)
                    else None
                ),
            )

    new_state = portfolio_state_after_sell(
        previous_state=state,
        ticker=ticker,
        sold_quantity=fill.quantity,
        fill_price=fill.fill_price,
        fill_fees=fill.fees,
        broker=broker,
    )
    if mark_exit_timestamp:
        new_state.last_exited_at = dict(state.last_exited_at)
        new_state.last_exited_at[ticker] = fill.filled_at.isoformat()
    from trading_bot.runtime.mark_to_market import mark_to_market
    new_state = mark_to_market(new_state, prices={ticker: fill.fill_price})
    ledger.save_portfolio_state(new_state)
    ledger.record_equity_snapshot(new_state, timestamp=fill.filled_at)

    event = {
        "command": "manage-positions",
        "ticker": ticker,
        "status": "FILLED",
        "reason": reason,
        "quantity": fill.quantity,
        "fill_price": fill.fill_price,
        "cash": new_state.cash,
    }
    line = (
        f"{ticker} FILLED reason={reason} qty={fill.quantity} "
        f"price={fill.fill_price:.2f} cash={new_state.cash:.2f}"
    )

    strategy_tag = getattr(position, "strategy_tag", "")
    if strategy_tag:
        from trading_bot.strategy.strategy_tracker import record_exit as _rec_exit

        _rec_exit(
            log_path.parent,
            strategy_tag,
            ticker,
            position.average_cost,
            fill.fill_price,
            fill.quantity,
            fill.fees,
            realized_pnl,
            reason,
            submitted_at,
        )

    return new_state, event, line


def fill_partial_take_profit_position(
    ticker: str,
    position,
    submitted_at: datetime,
    last_price: float,
    broker,
    ledger,
        state: PortfolioState,
        log_path,
        fraction: float = 0.5,
        settings=None,
        runtime_canary: "RuntimeCanaryContext | None" = None,
) -> tuple[PortfolioState, dict[str, object], str]:
    """Scale out part of a winning position and protect the remainder."""
    partial_qty = max(1, int(position.quantity * fraction))
    if partial_qty >= position.quantity:
        partial_qty = max(1, position.quantity - 1)

    new_state, event, line = fill_sell_position(
        ticker=ticker,
        position=position,
        reason="target_partial",
        submitted_at=submitted_at,
        last_price=last_price,
        broker=broker,
        ledger=ledger,
        state=state,
        log_path=log_path,
        quantity=partial_qty,
        runtime_canary=runtime_canary,
        close_db_position=False,
        mark_exit_timestamp=False,
        settings=settings,
    )

    try:
        from trading_bot.db.repositories import accumulate_partial_exit
        from trading_bot.db.session import get_session, init_db, make_session_factory
        from trading_bot.db.models import Trade
        from sqlalchemy import select

        if settings is None:
            from trading_bot.cli.app import load_settings

            settings = load_settings()
        engine = init_db(settings)
        session_factory = make_session_factory(engine)
        session = get_session(session_factory)
        try:
            trade = session.execute(
                select(Trade).where(
                    Trade.ticker == ticker.upper(),
                    Trade.status == "FILLED",
                ).order_by(Trade.filled_at.desc())
            ).scalars().first()
            if trade is not None:
                accumulate_partial_exit(
                    session=session,
                    trade_id=trade.id,
                    partial_pnl=float(event.get("realized_pnl", 0.0) or 0.0),
                )
        finally:
            session.close()
            engine.dispose()
    except Exception:
        logger.exception("Failed to accumulate partial exit for %s", ticker)

    remaining = new_state.positions.get(ticker)
    if remaining is not None:
        break_even_stop = max(remaining.average_cost, remaining.stop_loss or 0.0)
        new_state.positions[ticker] = remaining.model_copy(
            update={
                "stop_loss": break_even_stop,
                "profit_target": None,
                "partial_profit_taken": True,
            }
        )
        ledger.save_portfolio_state(new_state)

    event["remaining_quantity"] = new_state.positions.get(ticker).quantity if ticker in new_state.positions else 0
    line = f"{line} remaining={event['remaining_quantity']}"
    return new_state, event, line


def portfolio_state_after_sell(
    previous_state: PortfolioState,
    ticker: str,
    sold_quantity: int,
    fill_price: float,
    fill_fees: float,
    broker,
) -> PortfolioState:
    exited_position = previous_state.positions[ticker]
    positions = {}
    for symbol, quantity in broker.positions.items():
        if quantity <= 0 or symbol not in previous_state.positions:
            continue
        position = previous_state.positions[symbol]
        update = {"quantity": quantity}
        if symbol == ticker and exited_position.quantity > 0:
            sold_fee_share = (
                exited_position.entry_fees * sold_quantity / exited_position.quantity
            )
            update["entry_fees"] = max(
                0.0, exited_position.entry_fees - sold_fee_share
            )
        positions[symbol] = position.model_copy(update=update)
    realized_delta = (
        (fill_price - exited_position.average_cost) * sold_quantity
    ) - fill_fees
    equity = broker.cash + sum(
        position.quantity * position.average_cost for position in positions.values()
    )
    return PortfolioState(
        cash=round(broker.cash, 2),
        equity=round(equity, 2),
        positions=positions,
        realized_pnl=round(previous_state.realized_pnl + realized_delta, 2),
        unrealized_pnl=0.0,
    )
