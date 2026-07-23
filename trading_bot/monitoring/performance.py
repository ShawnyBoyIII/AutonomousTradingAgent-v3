from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_bot.portfolio.ledger import PortfolioLedger


@dataclass
class PerformanceMetrics:
    """Performance metrics for a trading period."""

    period: str
    start_date: datetime | None
    end_date: datetime | None

    # Trade counts
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0

    # Win/loss metrics
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    # P&L metrics
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    total_fees: float = 0.0

    # Risk-adjusted metrics
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0

    # Consecutive metrics
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0


def calculate_performance_metrics(
    ledger: PortfolioLedger,
    days: int = 30,
) -> PerformanceMetrics:
    """Calculate comprehensive performance metrics from trade history.

    Args:
        ledger: Portfolio ledger with order history
        days: Number of days to analyze (default 30)

    Returns:
        PerformanceMetrics with calculated statistics
    """
    rows = ledger.list_order_rows()
    if not rows:
        return PerformanceMetrics(period=f"last_{days}_days", start_date=None, end_date=None)

    # Filter to date range. Legacy rows may be naive America/New_York
    # wall time; new fills persist aware UTC. Normalize to naive UTC so
    # the comparison against the naive cutoff stays valid.
    from trading_bot.analytics.evaluation_windows import (
        normalize_timestamp,
    )

    cutoff = datetime.now() - timedelta(days=days)
    recent_rows = []
    for row in rows:
        parsed = normalize_timestamp(row["filled_at"], naive_timezone=None)
        if parsed is None:
            continue
        if parsed.replace(tzinfo=None) > cutoff:
            recent_rows.append(row)

    if not recent_rows:
        return PerformanceMetrics(period=f"last_{days}_days", start_date=None, end_date=None)

    # Match trades (BUY -> SELL pairs)
    trades = _match_trades(recent_rows)

    if not trades:
        return PerformanceMetrics(period=f"last_{days}_days", start_date=None, end_date=None)

    # Calculate metrics
    metrics = _calculate_trade_metrics(trades)
    metrics.period = f"last_{days}_days"
    metrics.start_date = min(t["entry_time"] for t in trades)
    metrics.end_date = max(t["exit_time"] for t in trades)

    return metrics


def calculate_daily_metrics(
    ledger: PortfolioLedger,
    lookback_days: int = 7,
) -> list[dict]:
    """Calculate daily performance metrics.

    Args:
        ledger: Portfolio ledger with order history
        lookback_days: Number of days to look back

    Returns:
        List of daily metrics dictionaries
    """
    rows = ledger.list_order_rows()
    if not rows:
        return []

    # Group by day
    daily_trades: dict[str, list] = {}
    for row in rows:
        date = datetime.fromisoformat(row["filled_at"]).date().isoformat()
        if date not in daily_trades:
            daily_trades[date] = []
        daily_trades[date].append(row)

    # Calculate metrics for each day
    results = []
    cutoff = (datetime.now() - timedelta(days=lookback_days)).date().isoformat()

    for date in sorted(daily_trades.keys()):
        if date < cutoff:
            continue

        day_rows = daily_trades[date]
        trades = _match_trades(day_rows)

        if trades:
            metrics = _calculate_trade_metrics(trades)
            results.append({
                "date": date,
                "trades": metrics.total_trades,
                "wins": metrics.winning_trades,
                "losses": metrics.losing_trades,
                "net_pnl": round(metrics.net_pnl, 2),
                "win_rate": round(metrics.win_rate, 2),
            })

    return results


def _match_trades(rows: list[dict]) -> list[dict]:
    """Match BUY and SELL orders into completed trades.

    Returns list of trades with:
    - ticker
    - entry_price
    - exit_price
    - quantity
    - pnl (gross, before fees)
    - fees
    - entry_time
    - exit_time
    """
    # Group by ticker
    by_ticker: dict[str, list] = {}
    for row in rows:
        ticker = row["ticker"]
        if ticker not in by_ticker:
            by_ticker[ticker] = []
        by_ticker[ticker].append(row)

    trades = []
    for ticker, ticker_rows in by_ticker.items():
        # Sort by time
        sorted_rows = sorted(ticker_rows, key=lambda x: x["filled_at"])

        # Match buys with sells (FIFO)
        position = 0
        entry_cost = 0.0
        entry_fees = 0.0
        entry_time = None

        for row in sorted_rows:
            qty = row["quantity"]
            price = row["fill_price"]
            fees = row["fees"]

            if row["side"] == "BUY":
                if position == 0:
                    entry_time = row["filled_at"]
                position += qty
                entry_cost += qty * price
                entry_fees += fees

            elif row["side"] == "SELL":
                if position > 0:
                    # Calculate trade P&L
                    exit_cost = qty * price
                    gross_pnl = exit_cost - (entry_cost * qty / position)
                    total_fees = entry_fees + fees

                    trades.append({
                        "ticker": ticker,
                        "entry_price": entry_cost / position if position > 0 else 0,
                        "exit_price": price,
                        "quantity": qty,
                        "pnl": gross_pnl,
                        "fees": total_fees,
                        "net_pnl": gross_pnl - total_fees,
                        "entry_time": datetime.fromisoformat(entry_time),
                        "exit_time": datetime.fromisoformat(row["filled_at"]),
                    })

                    # Reduce position
                    entry_cost -= entry_cost * qty / position
                    position -= qty
                    entry_fees = 0  # Reset fees for this trade

    return trades


def _calculate_trade_metrics(trades: list[dict]) -> PerformanceMetrics:
    """Calculate metrics from a list of matched trades."""
    metrics = PerformanceMetrics(period="", start_date=None, end_date=None)

    if not trades:
        return metrics

    # Basic counts
    metrics.total_trades = len(trades)

    # Calculate P&L for each trade
    pnls = [t["net_pnl"] for t in trades]
    gross_pnls = [t["pnl"] for t in trades]
    fees = [t["fees"] for t in trades]

    # Win/loss counts
    for pnl in pnls:
        if pnl > 0:
            metrics.winning_trades += 1
            metrics.gross_profit += pnl
            metrics.avg_win += pnl
            metrics.largest_win = max(metrics.largest_win, pnl)
        elif pnl < 0:
            metrics.losing_trades += 1
            metrics.gross_loss += abs(pnl)
            metrics.avg_loss += abs(pnl)
            metrics.largest_loss = min(metrics.largest_loss, pnl)
        else:
            metrics.breakeven_trades += 1

    # Win rate
    if metrics.total_trades > 0:
        metrics.win_rate = metrics.winning_trades / metrics.total_trades

    # Averages
    if metrics.winning_trades > 0:
        metrics.avg_win = metrics.avg_win / metrics.winning_trades
    if metrics.losing_trades > 0:
        metrics.avg_loss = metrics.avg_loss / metrics.losing_trades

    # P&L
    metrics.net_pnl = sum(pnls)
    metrics.total_fees = sum(fees)

    # Profit factor
    if metrics.gross_loss > 0:
        metrics.profit_factor = metrics.gross_profit / metrics.gross_loss
    elif metrics.gross_profit > 0:
        metrics.profit_factor = float("inf")

    # Sharpe ratio (simplified, assuming risk-free rate = 0)
    if len(pnls) > 1:
        import statistics
        try:
            avg_return = statistics.mean(pnls)
            std_return = statistics.stdev(pnls)
            if std_return > 0:
                # Annualized Sharpe (assuming daily trades)
                metrics.sharpe_ratio = (avg_return / std_return) * (252 ** 0.5)
        except statistics.StatisticsError:
            pass

    # Consecutive wins/losses
    current_wins = 0
    current_losses = 0
    for pnl in pnls:
        if pnl > 0:
            current_wins += 1
            current_losses = 0
            metrics.max_consecutive_wins = max(metrics.max_consecutive_wins, current_wins)
        elif pnl < 0:
            current_losses += 1
            current_wins = 0
            metrics.max_consecutive_losses = max(metrics.max_consecutive_losses, current_losses)

    return metrics


def format_performance_report(metrics: PerformanceMetrics) -> str:
    """Format metrics as a readable report."""
    if metrics.total_trades == 0:
        return "No trades found for the specified period."

    lines = [
        f"Performance Report: {metrics.period}",
        f"Period: {metrics.start_date:%Y-%m-%d} to {metrics.end_date:%Y-%m-%d}",
        "",
        "Trade Statistics:",
        f"  Total Trades: {metrics.total_trades}",
        f"  Wins: {metrics.winning_trades} ({metrics.win_rate:.1%})",
        f"  Losses: {metrics.losing_trades}",
        f"  Breakeven: {metrics.breakeven_trades}",
        "",
        "P&L Metrics:",
        f"  Net P&L: ${metrics.net_pnl:,.2f}",
        f"  Gross Profit: ${metrics.gross_profit:,.2f}",
        f"  Gross Loss: ${metrics.gross_loss:,.2f}",
        f"  Total Fees: ${metrics.total_fees:,.2f}",
        f"  Profit Factor: {metrics.profit_factor:.2f}",
        "",
        "Win/Loss Metrics:",
        f"  Average Win: ${metrics.avg_win:,.2f}",
        f"  Average Loss: ${metrics.avg_loss:,.2f}",
        f"  Largest Win: ${metrics.largest_win:,.2f}",
        f"  Largest Loss: ${metrics.largest_loss:,.2f}",
        f"  Max Consecutive Wins: {metrics.max_consecutive_wins}",
        f"  Max Consecutive Losses: {metrics.max_consecutive_losses}",
        "",
        "Risk Metrics:",
        f"  Sharpe Ratio: {metrics.sharpe_ratio:.2f}",
    ]

    return "\n".join(lines)
