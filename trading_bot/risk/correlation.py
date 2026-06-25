"""Portfolio correlation monitoring.

Computes pairwise correlation of open positions from daily returns.
Warns when average pairwise correlation is high, indicating
insufficient diversification.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_bot.models.portfolio import Position


@dataclass
class CorrelationResult:
    """Result of portfolio correlation analysis."""

    avg_correlation: float = 0.0
    max_correlation: float = 0.0
    max_pair: tuple[str, str] | None = None
    pair_count: int = 0
    correlation_matrix: list[dict[str, object]] = field(default_factory=list)
    warning: str | None = None


def compute_returns(prices: list[float]) -> list[float]:
    """Compute log returns from a price series (stdlib only, no numpy)."""
    if len(prices) < 2:
        return []
    returns = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] > 0:
            returns.append(prices[i] / prices[i - 1] - 1.0)
    return returns


def compute_pearson_correlation(x: list[float], y: list[float]) -> float:
    """Compute Pearson correlation coefficient using stdlib only.

    Returns 0.0 if either list has < 2 elements or std is zero.
    """
    n = min(len(x), len(y))
    if n < 2:
        return 0.0

    x = x[:n]
    y = y[:n]
    mean_x = statistics.mean(x)
    mean_y = statistics.mean(y)

    numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    sx = sum((xi - mean_x) ** 2 for xi in x)
    sy = sum((yi - mean_y) ** 2 for yi in y)

    sxsy = sx * sy
    if sxsy <= 0:
        return 0.0

    import math
    return numerator / math.sqrt(sxsy)


def compute_portfolio_correlation(
    positions: dict[str, Position],
    price_history: dict[str, list[float]],
    max_avg_correlation: float = 0.6,
) -> CorrelationResult:
    """Compute pairwise correlation across open positions.

    Args:
        positions: Dict of ticker -> Position (only tickers with quantity > 0).
        price_history: Dict of ticker -> list of daily prices (chronological).
        max_avg_correlation: Threshold for the warning (default 0.6).

    Returns:
        CorrelationResult with average/max correlation and warning if too high.
    """
    tickers = [
        ticker for ticker, pos in positions.items()
        if pos.quantity > 0 and ticker in price_history
    ]

    if len(tickers) < 2:
        return CorrelationResult(
            pair_count=0,
            warning=None,
        )

    # Pre-compute return series for each ticker
    returns_map: dict[str, list[float]] = {}
    for ticker in tickers:
        returns_map[ticker] = compute_returns(price_history[ticker])

    correlations: list[float] = []
    pairs: list[dict[str, object]] = []
    max_corr = 0.0
    max_pair: tuple[str, str] | None = None

    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            t1, t2 = tickers[i], tickers[j]
            corr = compute_pearson_correlation(
                returns_map[t1], returns_map[t2]
            )
            correlations.append(corr)
            pairs.append({
                "ticker_a": t1,
                "ticker_b": t2,
                "correlation": round(corr, 4),
            })
            if abs(corr) > abs(max_corr):
                max_corr = corr
                max_pair = (t1, t2)

    avg_corr = statistics.mean(correlations) if correlations else 0.0

    warning = None
    if avg_corr > max_avg_correlation and len(correlations) > 0:
        warning = (
            f"Average pairwise correlation {avg_corr:.2f} exceeds threshold "
            f"{max_avg_correlation:.2f} — consider reducing position concentration"
        )

    return CorrelationResult(
        avg_correlation=round(avg_corr, 4),
        max_correlation=round(max_corr, 4),
        max_pair=max_pair,
        pair_count=len(correlations),
        correlation_matrix=pairs,
        warning=warning,
    )


def format_correlation_report(result: CorrelationResult) -> str:
    """Format correlation analysis as a readable report."""
    if result.pair_count == 0:
        return "Need 2+ open positions with price history to compute correlation."

    lines = [
        "Portfolio Correlation Analysis:",
        f"  Average Correlation: {result.avg_correlation:.4f}",
        f"  Max Correlation: {result.max_correlation:.4f}",
    ]

    if result.max_pair:
        lines.append(f"  Max Pair: {result.max_pair[0]} / {result.max_pair[1]}")

    lines += [
        f"  Pairs Analyzed: {result.pair_count}",
        "",
        "Correlation Matrix:",
    ]

    # Sort pairs by absolute correlation, descending
    sorted_pairs = sorted(
        result.correlation_matrix,
        key=lambda p: abs(p["correlation"]),
        reverse=True,
    )

    for pair in sorted_pairs[:15]:
        corr = pair["correlation"]
        indicator = "🔴" if abs(corr) > 0.7 else "🟡" if abs(corr) > 0.4 else "🟢"
        lines.append(
            f"  {indicator} {pair['ticker_a']} / {pair['ticker_b']}: {corr:+.4f}"
        )

    if result.warning:
        lines += ["", f"  ⚠️  {result.warning}"]

    return "\n".join(lines)
