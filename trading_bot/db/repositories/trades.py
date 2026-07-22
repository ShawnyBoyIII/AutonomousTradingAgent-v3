from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.db.models import Trade


def upsert_trade(
    session: Session,
    ticker: str,
    side: str,
    order_type: str,
    quantity: int,
    entry_price: float,
    stop_loss: float | None = None,
    profit_target: float | None = None,
    fees: float = 0.0,
    strategy_tag: str | None = None,
    signal_quality: str | None = None,
    market_regime: str | None = None,
    supermodel_decision: str | None = None,
    consensus: str | None = None,
    entry_volume_ratio: float | None = None,
    entry_range_ratio: float | None = None,
    adaptive_rr: float | None = None,
    status: str = "FILLED",
) -> Trade:
    trade = Trade(
        ticker=ticker,
        side=side,
        order_type=order_type,
        quantity=quantity,
        entry_price=entry_price,
        stop_loss=stop_loss,
        profit_target=profit_target,
        fees=fees,
        filled_at=datetime.now(timezone.utc),
        strategy_tag=strategy_tag,
        signal_quality=signal_quality,
        market_regime=market_regime,
        supermodel_decision=supermodel_decision,
        consensus=consensus,
        entry_volume_ratio=entry_volume_ratio,
        entry_range_ratio=entry_range_ratio,
        adaptive_rr=adaptive_rr,
        status=status,
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def update_trade_exit(
    session: Session,
    trade_id: int,
    exit_price: float,
    exit_fees: float = 0.0,
    pnl: float | None = None,
    exit_rsi: float | None = None,
    exit_atr: float | None = None,
    hold_duration_minutes: float | None = None,
    exit_regime: str | None = None,
    exit_strategy: str | None = None,
    exit_reason: str | None = None,
) -> Trade:
    trade = session.get(Trade, trade_id)
    if trade is None:
        raise ValueError(f"Trade {trade_id} not found")
    trade.exit_price = exit_price
    trade.exit_fees = exit_fees
    trade.exited_at = datetime.now(timezone.utc)
    accumulated = float(getattr(trade, "partial_pnl_accumulated", 0.0) or 0.0)
    if accumulated and pnl is not None:
        trade.pnl = round(float(pnl) + accumulated, 2)
    else:
        trade.pnl = pnl
    trade.exit_rsi = exit_rsi
    trade.exit_atr = exit_atr
    trade.hold_duration_minutes = hold_duration_minutes
    trade.exit_regime = exit_regime
    trade.exit_strategy = exit_strategy
    trade.exit_reason = exit_reason
    trade.status = "CLOSED"
    session.commit()
    session.refresh(trade)
    return trade


def get_open_trades(session: Session) -> list[Trade]:
    return session.execute(
        select(Trade).where(Trade.status == "FILLED")
    ).scalars().all()


def accumulate_partial_exit(
    session: Session,
    trade_id: int,
    partial_pnl: float,
) -> Trade:
    """Add ``partial_pnl`` to the open trade's accumulated partial P&L.

    The trade stays open (``status = FILLED``) because the position
    has not been fully closed. ``partial_exit_count`` increments so
    reports can distinguish partial exits from full exits.
    """
    trade = session.get(Trade, trade_id)
    if trade is None:
        raise ValueError(f"Trade {trade_id} not found")
    existing = float(getattr(trade, "partial_pnl_accumulated", 0.0) or 0.0)
    trade.partial_pnl_accumulated = round(existing + float(partial_pnl), 2)
    trade.partial_exit_count = int(getattr(trade, "partial_exit_count", 0) or 0) + 1
    session.commit()
    session.refresh(trade)
    return trade


def get_trades(
    session: Session,
    ticker: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[Trade]:
    query = select(Trade)
    if ticker:
        query = query.where(Trade.ticker == ticker)
    if since:
        query = query.where(Trade.filled_at >= since)
    query = query.order_by(Trade.filled_at.desc())
    if limit:
        query = query.limit(limit)
    return session.execute(query).scalars().all()
