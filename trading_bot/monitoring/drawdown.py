"""Drawdown monitoring from equity history.

Computes max drawdown, current drawdown, Calmar ratio, and underwater
duration from the equity_history table. Also provides session-scoped
drawdown from an in-memory equity series (for RealTimePnL).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_bot.portfolio.ledger import PortfolioLedger


@dataclass
class DrawdownMetrics:
    """Drawdown metrics derived from equity history."""

    current_drawdown_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    peak_equity: float = 0.0
    trough_equity: float = 0.0
    recovery_equity: float = 0.0
    underwater_bars: int = 0
    max_underwater_bars: int = 0
    calmar_ratio: float = 0.0


def compute_drawdown(equity_series: list[float]) -> DrawdownMetrics:
    """Compute drawdown metrics from a list of equity values.

    Args:
        equity_series: Chronological list of equity values (oldest first).

    Returns:
        DrawdownMetrics with current/max drawdown, peak/trough, and underwater
        duration.

    A drawdown is the percentage decline from the running peak:

        drawdown_pct = (peak - current) / peak * 100
    """
    if not equity_series or len(equity_series) < 2:
        return DrawdownMetrics(
            peak_equity=equity_series[0] if equity_series else 0.0,
            recovery_equity=equity_series[-1] if equity_series else 0.0,
        )

    peak = equity_series[0]
    trough = equity_series[0]
    max_dd = 0.0
    peak_at_max_dd = equity_series[0]
    trough_at_max_dd = equity_series[0]

    underwater = 0
    max_underwater = 0

    for value in equity_series:
        if value > peak:
            peak = value
        if value < peak:
            dd = (peak - value) / peak * 100.0 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
                peak_at_max_dd = peak
                trough_at_max_dd = value
            underwater += 1
            if underwater > max_underwater:
                max_underwater = underwater
        else:
            underwater = 0

    current_value = equity_series[-1]
    current_peak = max(equity_series)
    current_dd = (
        (current_peak - current_value) / current_peak * 100.0
        if current_peak > 0
        else 0.0
    )

    return DrawdownMetrics(
        current_drawdown_pct=current_dd,
        max_drawdown_pct=max_dd,
        peak_equity=current_peak,
        trough_equity=trough_at_max_dd,
        recovery_equity=current_value,
        underwater_bars=underwater,
        max_underwater_bars=max_underwater,
    )


def compute_drawdown_from_ledger(
    ledger: PortfolioLedger,
    limit: int = 500,
) -> DrawdownMetrics:
    """Compute drawdown from the equity_history table in the ledger.

    Args:
        ledger: PortfolioLedger with equity snapshot history.
        limit: Maximum number of recent snapshots to analyze.

    Returns:
        DrawdownMetrics for the equity time series.
    """
    rows = ledger.list_equity_history(limit=limit)
    if not rows:
        return DrawdownMetrics()

    equity_series = [float(row["equity"]) for row in rows]
    metrics = compute_drawdown(equity_series)

    # Compute Calmar ratio (CAGR / max drawdown)
    # Simplified: annualized return / max DD
    if len(equity_series) >= 2 and equity_series[0] > 0:
        total_return = (equity_series[-1] / equity_series[0]) - 1.0
        # Estimate annualization: assume ~252 trading days if daily snapshots
        # Otherwise just use total return as-is
        periods = len(equity_series)
        if periods > 252:
            annualized_return = (1.0 + total_return) ** (252.0 / periods) - 1.0
        else:
            annualized_return = total_return

        if metrics.max_drawdown_pct > 0:
            metrics.calmar_ratio = annualized_return / (metrics.max_drawdown_pct / 100.0)

    return metrics


def compute_session_drawdown(
    equity_history: list[float],
    current_equity: float,
) -> tuple[float, float, float]:
    """Compute session drawdown from a list of equity values.

    Used by RealTimePnL to populate session_high/low/max_drawdown.

    Args:
        equity_history: Previous equity values in the session.
        current_equity: Current equity value.

    Returns:
        Tuple of (session_high_equity, session_low_equity, session_max_drawdown_pct).
    """
    if not equity_history:
        return current_equity, current_equity, 0.0

    all_values = equity_history + [current_equity]
    high = max(all_values)
    low = min(all_values)

    max_dd = 0.0
    running_peak = all_values[0]
    for value in all_values:
        if value > running_peak:
            running_peak = value
        if running_peak > 0:
            dd = (running_peak - value) / running_peak * 100.0
            if dd > max_dd:
                max_dd = dd

    return high, low, max_dd


def format_drawdown_report(metrics: DrawdownMetrics) -> str:
    """Format drawdown metrics as a readable report."""
    if metrics.max_drawdown_pct == 0.0 and metrics.peak_equity == 0.0:
        return "No equity history available for drawdown analysis."

    lines = [
        "Drawdown Analysis:",
        f"  Current Drawdown: {metrics.current_drawdown_pct:.2f}%",
        f"  Max Drawdown: {metrics.max_drawdown_pct:.2f}%",
        f"  Peak Equity: ${metrics.peak_equity:,.2f}",
        f"  Trough Equity: ${metrics.trough_equity:,.2f}",
        f"  Current Equity: ${metrics.recovery_equity:,.2f}",
        f"  Underwater Bars: {metrics.underwater_bars}",
        f"  Max Underwater Bars: {metrics.max_underwater_bars}",
    ]

    if metrics.calmar_ratio > 0:
        lines.append(f"  Calmar Ratio: {metrics.calmar_ratio:.2f}")
    elif metrics.max_drawdown_pct > 0:
        lines.append("  Calmar Ratio: N/A (negative/near-zero return)")

    return "\n".join(lines)
