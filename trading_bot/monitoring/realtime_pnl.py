"""Real-time P&L tracking and monitoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_bot.portfolio.ledger import PortfolioLedger


@dataclass
class RealTimePnL:
    """Real-time P&L snapshot."""

    timestamp: datetime
    total_equity: float = 0.0
    cash: float = 0.0
    invested: float = 0.0

    # P&L metrics
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0

    # Trade metrics
    today_trades: int = 0
    today_pnl: float = 0.0

    # Position metrics
    open_positions: int = 0
    positions_heat: float = 0.0  # Total unrealized loss as % of equity

    # Session metrics
    session_high_equity: float = 0.0
    session_low_equity: float = 0.0
    session_max_drawdown: float = 0.0

    # Alerts
    alerts: list[str] = field(default_factory=list)


def calculate_realtime_pnl(
    ledger: PortfolioLedger,
    current_prices: dict[str, float],
) -> RealTimePnL:
    """Calculate real-time P&L with current market prices.

    Args:
        ledger: Portfolio ledger with positions
        current_prices: Dictionary of ticker -> current price

    Returns:
        RealTimePnL snapshot with current metrics
    """
    now = datetime.now(timezone.utc)

    # Get portfolio state
    portfolio = ledger.ensure_portfolio_state()
    equity = portfolio.total_equity
    cash = portfolio.cash

    # Calculate invested amount
    invested = sum(
        pos.quantity * current_prices.get(ticker, pos.average_cost)
        for ticker, pos in portfolio.positions.items()
    )

    # Calculate realized P&L from closed trades today
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_orders = [
        row for row in ledger.list_order_rows()
        if datetime.fromisoformat(row["filled_at"]) >= today_start
    ]

    today_trades = len(today_orders)
    today_pnl = sum(float(row.get("pnl", 0.0)) for row in today_orders)

    # Calculate total realized P&L
    all_orders = ledger.list_order_rows()
    realized_pnl = sum(
        float(row.get("pnl", 0.0))
        for row in all_orders
        if row["side"] == "SELL"
    )

    # Calculate unrealized P&L
    unrealized_pnl = 0.0
    positions_heat = 0.0

    for ticker, position in portfolio.positions.items():
        if position.quantity > 0:
            current_price = current_prices.get(ticker)
            if current_price:
                pos_pnl = position.quantity * (current_price - position.average_cost)
                unrealized_pnl += pos_pnl
                if pos_pnl < 0:
                    positions_heat += abs(pos_pnl)

    # Calculate heat as percentage
    positions_heat_pct = (positions_heat / equity * 100) if equity > 0 else 0

    # Compute session drawdown from equity history
    equity_rows = ledger.list_equity_history(limit=500)
    equity_series = [float(row["equity"]) for row in equity_rows]
    from trading_bot.monitoring.drawdown import compute_session_drawdown
    session_high, session_low, session_max_dd = compute_session_drawdown(
        equity_series, equity
    )

    # Check alerts
    alerts = []
    if positions_heat_pct > 3.0:
        alerts.append(f"HIGH_HEAT: Portfolio heat {positions_heat_pct:.1f}% exceeds 3% limit")
    if today_pnl < -1000:
        alerts.append(f"DAILY_LOSS: Today P&L {today_pnl:.2f} below -$1000")
    if session_max_dd > 5.0:
        alerts.append(f"MAX_DRAWDOWN: Session drawdown {session_max_dd:.1f}% exceeds 5%")

    return RealTimePnL(
        timestamp=now,
        total_equity=equity,
        cash=cash,
        invested=invested,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        total_pnl=realized_pnl + unrealized_pnl,
        today_trades=today_trades,
        today_pnl=today_pnl,
        open_positions=len(portfolio.positions),
        positions_heat=positions_heat_pct,
        session_high_equity=session_high,
        session_low_equity=session_low,
        session_max_drawdown=session_max_dd,
        alerts=alerts,
    )


def format_pnl_snapshot(snapshot: RealTimePnL) -> dict[str, object]:
    """Format P&L snapshot as dictionary for JSON serialization."""
    return {
        "timestamp": snapshot.timestamp.isoformat(),
        "equity": {
            "total": round(snapshot.total_equity, 2),
            "cash": round(snapshot.cash, 2),
            "invested": round(snapshot.invested, 2),
        },
        "pnl": {
            "realized": round(snapshot.realized_pnl, 2),
            "unrealized": round(snapshot.unrealized_pnl, 2),
            "total": round(snapshot.total_pnl, 2),
            "today": round(snapshot.today_pnl, 2),
        },
        "trading": {
            "today_trades": snapshot.today_trades,
            "open_positions": snapshot.open_positions,
            "positions_heat_pct": round(snapshot.positions_heat, 2),
        },
        "alerts": snapshot.alerts,
    }


@dataclass
class PnLAlertThresholds:
    """Thresholds for P&L alerts."""

    max_daily_loss: float = -1000.0
    max_positions_heat_pct: float = 3.0
    max_drawdown_pct: float = 5.0
    min_equity_buffer_pct: float = 10.0


def check_pnl_alerts(
    snapshot: RealTimePnL,
    thresholds: PnLAlertThresholds | None = None,
) -> list[dict]:
    """Check P&L snapshot against alert thresholds.

    Returns:
        List of alert dictionaries with level, type, and message
    """
    thresholds = thresholds or PnLAlertThresholds()
    alerts = []

    # Daily loss alert
    if snapshot.today_pnl < thresholds.max_daily_loss:
        alerts.append({
            "level": "critical",
            "type": "daily_loss_limit",
            "message": f"Daily loss ${snapshot.today_pnl:.2f} exceeds limit ${thresholds.max_daily_loss:.2f}",
            "value": snapshot.today_pnl,
            "threshold": thresholds.max_daily_loss,
        })

    # Portfolio heat alert
    if snapshot.positions_heat > thresholds.max_positions_heat_pct:
        alerts.append({
            "level": "warning",
            "type": "high_portfolio_heat",
            "message": f"Portfolio heat {snapshot.positions_heat:.1f}% exceeds {thresholds.max_positions_heat_pct}%",
            "value": snapshot.positions_heat,
            "threshold": thresholds.max_positions_heat_pct,
        })

    # Drawdown alert
    if snapshot.session_max_drawdown > thresholds.max_drawdown_pct:
        alerts.append({
            "level": "critical",
            "type": "max_drawdown",
            "message": f"Session drawdown {snapshot.session_max_drawdown:.1f}% exceeds {thresholds.max_drawdown_pct}%",
            "value": snapshot.session_max_drawdown,
            "threshold": thresholds.max_drawdown_pct,
        })

    # Cash buffer alert
    cash_pct = (snapshot.cash / snapshot.total_equity * 100) if snapshot.total_equity > 0 else 0
    if cash_pct < thresholds.min_equity_buffer_pct:
        alerts.append({
            "level": "warning",
            "type": "low_cash_buffer",
            "message": f"Cash buffer {cash_pct:.1f}% below minimum {thresholds.min_equity_buffer_pct}%",
            "value": cash_pct,
            "threshold": thresholds.min_equity_buffer_pct,
        })

    return alerts


def calculate_pnl_change(
    current: RealTimePnL,
    previous: RealTimePnL | None,
) -> dict[str, float]:
    """Calculate P&L change between two snapshots.

    Returns:
        Dictionary with absolute and percentage changes
    """
    if not previous:
        return {
            "equity_change": 0.0,
            "equity_change_pct": 0.0,
            "pnl_change": 0.0,
            "time_delta_seconds": 0.0,
        }

    time_delta = (current.timestamp - previous.timestamp).total_seconds()
    equity_change = current.total_equity - previous.total_equity
    equity_change_pct = (
        (equity_change / previous.total_equity * 100)
        if previous.total_equity > 0
        else 0.0
    )
    pnl_change = current.total_pnl - previous.total_pnl

    return {
        "equity_change": round(equity_change, 2),
        "equity_change_pct": round(equity_change_pct, 4),
        "pnl_change": round(pnl_change, 2),
        "time_delta_seconds": round(time_delta, 1),
    }
