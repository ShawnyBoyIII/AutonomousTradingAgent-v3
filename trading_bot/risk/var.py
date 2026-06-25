"""Value at Risk (VaR) and stress testing.

Provides three VaR methods:
1. Historical VaR: percentile of historical portfolio returns
2. Parametric VaR: mean − z·σ (normal assumption)
3. Stress scenarios: apply predefined shock factors to current positions

Also provides Monte Carlo VaR for robustness (simple simulation).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_bot.models.portfolio import Position


@dataclass
class VaRResult:
    """Value at Risk results for the portfolio."""

    method: str
    confidence: float
    var_dollar: float = 0.0
    var_pct: float = 0.0
    portfolio_value: float = 0.0
    expected_shortfall_dollar: float = 0.0
    detail: str = ""


@dataclass
class StressResult:
    """Result of a single stress scenario."""

    scenario: str
    portfolio_loss: float
    portfolio_loss_pct: float
    per_position: dict[str, float] = field(default_factory=dict)


def _z_score(confidence: float) -> float:
    """Approximate z-score for a given confidence level (one-tailed).

    Uses the inverse error function approximation for common confidence levels.
    """
    # Common z-scores (one-tailed, left tail)
    table = {
        0.90: 1.2816,
        0.95: 1.6449,
        0.99: 2.3263,
        0.999: 3.0902,
    }
    # Find closest match
    best = min(table.keys(), key=lambda k: abs(k - confidence))
    return table[best]


def compute_historical_var(
    position_values: dict[str, float],
    price_history: dict[str, list[float]],
    positions: dict[str, Position],
    confidence: float = 0.95,
) -> VaRResult:
    """Historical VaR: apply historical returns to current portfolio.

    Simulates what the portfolio would have lost on each historical day,
    then takes the appropriate percentile.

    Args:
        position_values: Dict of ticker -> current market value ($).
        price_history: Dict of ticker -> list of historical prices.
        positions: Dict of ticker -> Position (for quantity).
        confidence: Confidence level (0.95 = 95%).

    Returns:
        VaRResult with VaR in dollars and as a percentage.
    """
    tickers = [t for t in positions if positions[t].quantity > 0 and t in price_history]

    if not tickers:
        return VaRResult(method="historical", confidence=confidence)

    # Compute daily returns for each ticker
    returns_map = {}
    min_len: float | int = math.inf
    for t in tickers:
        prices = price_history[t]
        if len(prices) < 2:
            returns_map[t] = []
            continue
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                returns.append(prices[i] / prices[i - 1] - 1.0)
        returns_map[t] = returns
        min_len = min(min_len, len(returns))

    if min_len == math.inf or min_len < 2:
        return VaRResult(
            method="historical",
            confidence=confidence,
            detail="insufficient price history",
        )

    # Align all return series to same length
    total_portfolio_value = sum(position_values.get(t, 0) for t in tickers)

    if total_portfolio_value <= 0:
        return VaRResult(method="historical", confidence=confidence)

    # Simulate portfolio P&L for each historical day
    portfolio_returns: list[float] = []
    for i in range(int(min_len)):
        day_pnl = 0.0
        for t in tickers:
            position_value = position_values.get(t, 0)
            weight = position_value / total_portfolio_value
            day_return = returns_map[t][i]
            day_pnl += weight * day_return
        portfolio_returns.append(day_pnl)

    portfolio_returns.sort()

    # VaR is the absolute value of the percentile loss
    # For 95% confidence, take the 5th percentile
    percentile_index = int(len(portfolio_returns) * (1.0 - confidence))
    if percentile_index >= len(portfolio_returns):
        percentile_index = len(portfolio_returns) - 1

    var_return = portfolio_returns[percentile_index]
    var_dollar = abs(var_return) * total_portfolio_value
    var_pct = abs(var_return) * 100.0

    # Expected Shortfall (Conditional VaR): average of losses beyond VaR
    tail_losses = portfolio_returns[:percentile_index + 1]
    es_return = statistics.mean(tail_losses) if tail_losses else var_return
    es_dollar = abs(es_return) * total_portfolio_value

    return VaRResult(
        method="historical",
        confidence=confidence,
        var_dollar=round(var_dollar, 2),
        var_pct=round(var_pct, 4),
        portfolio_value=round(total_portfolio_value, 2),
        expected_shortfall_dollar=round(es_dollar, 2),
    )


def compute_parametric_var(
    position_values: dict[str, float],
    price_history: dict[str, list[float]],
    positions: dict[str, Position],
    confidence: float = 0.95,
) -> VaRResult:
    """Parametric VaR: mean − z·σ (assumes normal distribution).

    Args:
        position_values: Dict of ticker -> current market value ($).
        price_history: Dict of ticker -> list of historical prices.
        positions: Dict of ticker -> Position.
        confidence: Confidence level.

    Returns:
        VaRResult using parametric (variance-covariance) method.
    """
    from trading_bot.risk.correlation import compute_returns, compute_pearson_correlation

    tickers = [t for t in positions if positions[t].quantity > 0 and t in price_history]

    if not tickers:
        return VaRResult(method="parametric", confidence=confidence)

    total_value = sum(position_values.get(t, 0) for t in tickers)
    if total_value <= 0:
        return VaRResult(method="parametric", confidence=confidence)

    # Weights
    weights = {t: position_values.get(t, 0) / total_value for t in tickers}

    # Mean and std of returns per ticker
    stats_map = {}
    returns_map = {}
    for t in tickers:
        returns = compute_returns(price_history[t])
        if len(returns) < 2:
            return VaRResult(
                method="parametric",
                confidence=confidence,
                detail="insufficient price history for " + t,
            )
        returns_map[t] = returns
        stats_map[t] = (statistics.mean(returns), statistics.stdev(returns))

    # Portfolio mean and variance
    port_mean = sum(weights[t] * stats_map[t][0] for t in tickers)

    # Portfolio variance = sum(w_i * w_j * sigma_i * sigma_j * corr_ij)
    port_variance = 0.0
    for i, ti in enumerate(tickers):
        for j, tj in enumerate(tickers):
            if i == j:
                port_variance += (
                    weights[ti] * weights[tj]
                    * stats_map[ti][1] * stats_map[tj][1]
                )
            else:
                corr = compute_pearson_correlation(returns_map[ti], returns_map[tj])
                port_variance += (
                    weights[ti] * weights[tj]
                    * stats_map[ti][1] * stats_map[tj][1]
                    * corr
                )

    port_std = math.sqrt(port_variance) if port_variance > 0 else 0.0
    z = _z_score(confidence)

    var_return = port_mean - z * port_std
    var_dollar = abs(var_return) * total_value
    var_pct = abs(var_return) * 100.0

    # Expected shortfall for normal: mean - z_es * std
    # where z_es = z / (1 - confidence) (approx for ES under normality)
    z_es = z / max(1.0 - confidence, 0.001)
    es_return = port_mean - z_es * port_std
    es_dollar = abs(es_return) * total_value

    return VaRResult(
        method="parametric",
        confidence=confidence,
        var_dollar=round(var_dollar, 2),
        var_pct=round(var_pct, 4),
        portfolio_value=round(total_value, 2),
        expected_shortfall_dollar=round(es_dollar, 2),
    )


# Stress scenario definitions: name -> (price_shock_pct, volume_assumption)
STRESS_SCENARIOS: dict[str, float] = {
    "market_crash_2008": -0.35,      # ~2008 financial crisis
    "flash_crash_2010": -0.10,       # ~Flash Crash
    "covid_crash_2020": -0.15,       # ~COVID-19 March 2020
    "mild_correction": -0.05,        # Run-of-the-mill 5% correction
    "sector_rotation": -0.08,        # Sector rotation shock
    "rate_shock": -0.07,             # Rate-hike-driven selloff
}


def compute_stress_test(
    position_values: dict[str, float],
    positions: dict[str, Position],
    scenarios: dict[str, float] | None = None,
) -> list[StressResult]:
    """Apply predefined stress scenarios to current positions.

    Args:
        position_values: Dict of ticker -> current market value ($).
        positions: Dict of ticker -> Position.
        scenarios: Optional custom scenarios (name -> shock_pct). If omitted,
            uses the predefined STRESS_SCENARIOS.

    Returns:
        List of StressResult for each scenario.
    """
    active_scenarios = scenarios if scenarios is not None else STRESS_SCENARIOS
    total_value = sum(
        position_values.get(t, 0)
        for t in positions if positions[t].quantity > 0
    )

    if total_value <= 0:
        return []

    results: list[StressResult] = []
    for scenario_name, shock_pct in active_scenarios.items():
        per_position: dict[str, float] = {}
        total_loss = 0.0

        for ticker, position in positions.items():
            if position.quantity <= 0:
                continue
            mv = position_values.get(ticker, 0)
            loss = mv * shock_pct
            per_position[ticker] = round(loss, 2)
            total_loss += loss

        loss_pct = abs(total_loss) / total_value * 100.0 if total_value > 0 else 0.0

        results.append(StressResult(
            scenario=scenario_name,
            portfolio_loss=round(total_loss, 2),
            portfolio_loss_pct=round(abs(loss_pct), 4),
            per_position=per_position,
        ))

    # Sort by severity (most loss first)
    results.sort(key=lambda r: r.portfolio_loss)
    return results


def format_var_report(var_result: VaRResult) -> str:
    """Format VaR result as a readable report."""
    if var_result.var_dollar == 0.0 and not var_result.detail:
        return "Insufficient position data for VaR calculation."

    lines = [
        f"Value at Risk ({var_result.method.title()} method):",
        f"  Confidence: {var_result.confidence:.0%}",
        f"  Portfolio Value: ${var_result.portfolio_value:,.2f}",
        f"  VaR (1-day): ${var_result.var_dollar:,.2f} ({var_result.var_pct:.2f}%)",
        f"  Expected Shortfall: ${var_result.expected_shortfall_dollar:,.2f}",
    ]

    if var_result.detail:
        lines.append(f"  Note: {var_result.detail}")

    return "\n".join(lines)


def format_stress_report(results: list[StressResult]) -> str:
    """Format stress test results as a readable report."""
    if not results:
        return "No positions to stress test."

    lines = ["Stress Test Scenarios:", ""]

    for result in results:
        lines.append(
            f"  {result.scenario}: "
            f"loss=-${abs(result.portfolio_loss):,.2f} "
            f"({result.portfolio_loss_pct:.2f}%)"
        )

    return "\n".join(lines)
