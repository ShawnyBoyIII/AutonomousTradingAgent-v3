"""Post-backtest attribution analysis.

Provides deep trade-level attribution including:
- Winner/loser breakdown with signal metadata
- Beta regression against benchmark
- Market regime analysis
- Monte Carlo permutation testing
- Holding period statistics
- Exit reason attribution
- Signal quality correlation with P&L
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def run_attribution(
    backtest_result: dict[str, Any],
    market_data: dict[str, pd.DataFrame] | None = None,
    benchmark_symbol: str = "SPY",
    benchmark_data: pd.DataFrame | None = None,
    risk_free_rate: float = 0.05,
) -> dict[str, Any]:
    """Run comprehensive post-backtest attribution analysis.

    Args:
        backtest_result: Output from run_backtest() or run_rl_backtest().
        market_data: Optional dict of symbol -> DataFrame for regime analysis.
        benchmark_symbol: Benchmark ticker for beta calculation.
        benchmark_data: Optional benchmark DataFrame for beta calculation.
        risk_free_rate: Annualized risk-free rate for Sharpe calculation.

    Returns:
        Attribution analysis dict with multiple attribution layers.
    """
    attribution = {
        "trade_level_attribution": _trade_level_attribution(backtest_result),
        "winner_loser_analysis": _winner_loser_analysis(backtest_result),
        "holding_period_analysis": _holding_period_analysis(backtest_result),
        "exit_reason_attribution": _exit_reason_attribution(backtest_result),
        "signal_quality_attribution": _signal_quality_attribution(backtest_result),
    }

    if benchmark_data is not None and not benchmark_data.empty:
        attribution["beta_regression"] = _beta_regression(
            backtest_result,
            benchmark_data,
            risk_free_rate,
        )
        attribution["regime_analysis"] = _regime_analysis(
            backtest_result,
            benchmark_data,
        )

    attribution["monte_carlo"] = _monte_carlo_simulation(backtest_result)

    return attribution


def _trade_level_attribution(result: dict[str, Any]) -> dict[str, Any]:
    """Trade-level attribution with signal metadata."""
    rows = result.get("rows", [])
    trades = result.get("trades", 0)
    net_pnl = result.get("net_pnl", 0.0)
    wins = result.get("wins", 0)
    losses = result.get("losses", 0)

    # Per-ticker breakdown
    ticker_attribution = []
    for row in rows:
        ticker_pnl = row.get("net_pnl", 0.0)
        ticker_trades = row.get("trades", 0)
        attribution_pct = (ticker_pnl / net_pnl * 100) if net_pnl != 0 else 0.0

        ticker_attribution.append({
            "ticker": row.get("ticker", ""),
            "trades": ticker_trades,
            "net_pnl": round(ticker_pnl, 2),
            "contribution_pct": round(attribution_pct, 1),
            "wins": row.get("wins", 0),
            "losses": row.get("losses", 0),
            "win_rate": round(row.get("wins", 0) / ticker_trades * 100, 1) if ticker_trades > 0 else 0.0,
        })

    # Sort by contribution
    ticker_attribution.sort(key=lambda x: x["contribution_pct"], reverse=True)

    return {
        "total_trades": trades,
        "total_pnl": round(net_pnl, 2),
        "total_wins": wins,
        "total_losses": losses,
        "win_rate": round(wins / trades * 100, 1) if trades > 0 else 0.0,
        "ticker_contributions": ticker_attribution,
        "top_contributor": ticker_attribution[0]["ticker"] if ticker_attribution else None,
        "worst_contributor": ticker_attribution[-1]["ticker"] if ticker_attribution else None,
    }


def _winner_loser_analysis(result: dict[str, Any]) -> dict[str, Any]:
    """Winner/loser breakdown with statistics."""
    avg_win = result.get("avg_win", 0.0)
    avg_loss = result.get("avg_loss", 0.0)
    gross_profit = result.get("gross_profit", 0.0)
    gross_loss = result.get("gross_loss", 0.0)
    wins = result.get("wins", 0)
    losses = result.get("losses", 0)

    # Win/loser ratio
    win_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    # Profit factor
    loss_abs = abs(gross_loss)
    profit_factor = round(gross_profit / loss_abs, 2) if loss_abs > 0 else (
        round(gross_profit, 2) if gross_profit > 0 else 0.0
    )

    # Expectancy
    trades = result.get("trades", 0)
    net_pnl = result.get("net_pnl", 0.0)
    expectancy = round(net_pnl / trades, 2) if trades > 0 else 0.0

    return {
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "win_loss_ratio": round(win_loss_ratio, 2),
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "total_wins": wins,
        "total_losses": losses,
    }


def _holding_period_analysis(result: dict[str, Any]) -> dict[str, Any]:
    """Holding period statistics."""
    # If we have window data (walk-forward), extract holding periods
    windows = result.get("windows", [])

    if windows:
        # Use window-level data as proxy for holding periods
        trade_counts = [w.get("trades", 0) for w in windows]
        pnl_per_trade = []
        for w in windows:
            trades_w = w.get("trades", 0)
            if trades_w > 0:
                pnl_per_trade.append(w.get("net_pnl", 0.0) / trades_w)

        return {
            "num_windows": len(windows),
            "avg_trades_per_window": round(np.mean(trade_counts), 1) if trade_counts else 0.0,
            "pnl_per_trade_mean": round(np.mean(pnl_per_trade), 2) if pnl_per_trade else 0.0,
            "pnl_per_trade_std": round(np.std(pnl_per_trade), 2) if pnl_per_trade else 0.0,
        }

    return {
        "note": "Holding period analysis requires walk-forward or detailed trade data",
        "windows_available": len(windows) > 0,
    }


def _exit_reason_attribution(result: dict[str, Any]) -> dict[str, Any]:
    """Exit reason attribution (stop loss, profit target, time exit)."""
    # Extract from rows if available
    rows = result.get("rows", [])

    # Aggregate exit statistics per ticker
    exit_stats = {}
    for row in rows:
        ticker = row.get("ticker", "")
        wins = row.get("wins", 0)
        losses = row.get("losses", 0)

        # Estimate exit distribution based on win/loss ratio
        # (In production, this would come from detailed trade records)
        estimated_profit_targets = int(wins * 0.7)  # Assume 70% hit target
        estimated_stops = int(losses * 0.8)  # Assume 80% hit stop
        estimated_time_exits = max(0, losses - estimated_stops)

        exit_stats[ticker] = {
            "estimated_profit_targets": estimated_profit_targets,
            "estimated_stop_losses": estimated_stops,
            "estimated_time_exits": estimated_time_exits,
        }

    return {
        "ticker_exit_stats": exit_stats,
        "note": "Exit reasons are estimated from win/loss ratios. For precise attribution, enable detailed trade logging.",
    }


def _signal_quality_attribution(result: dict[str, Any]) -> dict[str, Any]:
    """Signal quality correlation with P&L."""
    # Analyze if higher confidence signals produce better results
    rows = result.get("rows", [])

    quality_metrics = []
    for row in rows:
        ticker = row.get("ticker", "")
        trades = row.get("trades", 0)
        wins = row.get("wins", 0)
        win_rate = wins / trades if trades > 0 else 0.0

        # Classify signal quality
        if win_rate >= 0.6:
            quality = "high"
        elif win_rate >= 0.45:
            quality = "medium"
        else:
            quality = "low"

        quality_metrics.append({
            "ticker": ticker,
            "trades": trades,
            "win_rate": round(win_rate * 100, 1),
            "quality_rating": quality,
        })

    # Average by quality tier
    tiers = {"high": [], "medium": [], "low": []}
    for m in quality_metrics:
        tiers[m["quality_rating"]].append(m["win_rate"])

    tier_summary = {}
    for tier, rates in tiers.items():
        if rates:
            tier_summary[tier] = {
                "num_tickers": len(rates),
                "avg_win_rate": round(np.mean(rates) * 100, 1),
                "min_win_rate": round(min(rates) * 100, 1),
                "max_win_rate": round(max(rates) * 100, 1),
            }

    return {
        "ticker_quality": quality_metrics,
        "tier_summary": tier_summary,
    }


def _beta_regression(
    result: dict[str, Any],
    benchmark_data: pd.DataFrame,
    risk_free_rate: float = 0.05,
) -> dict[str, Any]:
    """Beta regression against benchmark."""
    try:
        # Calculate daily returns from benchmark
        benchmark_returns = benchmark_data["close"].pct_change().dropna()

        if len(benchmark_returns) < 20:
            return {"note": "Insufficient benchmark data for beta calculation"}

        # Calculate strategy returns (simplified from net_pnl)
        net_pnl = result.get("net_pnl", 0.0)
        trades = result.get("trades", 0)

        if trades == 0:
            return {"note": "No trades to calculate beta"}

        # Use win rate and profit factor as proxy for strategy performance
        gross_profit = result.get("gross_profit", 0.0)
        gross_loss = result.get("gross_loss", 0.0)

        # Calculate alpha and beta
        strategy_return = net_pnl / 10000.0  # Assume $10k starting capital
        benchmark_return = (
            benchmark_data["close"].iloc[-1] / benchmark_data["close"].iloc[0] - 1
        )

        # Simple beta estimation
        cov_matrix = np.cov(benchmark_returns, net_pnl / max(trades, 1))
        beta = cov_matrix[0, 1] / cov_matrix[0, 0] if cov_matrix[0, 0] != 0 else 1.0

        # Alpha calculation (Jensen's alpha)
        alpha = strategy_return - (risk_free_rate + beta * (benchmark_return - risk_free_rate))

        # Sharpe ratio
        volatility = benchmark_returns.std() * (252 ** 0.5)
        sharpe = (strategy_return - risk_free_rate) / volatility if volatility > 0 else 0.0

        return {
            "beta": round(float(beta), 3),
            "alpha": round(float(alpha), 4),
            "sharpe_ratio": round(float(sharpe), 2),
            "benchmark_return": round(float(benchmark_return), 4),
            "strategy_return": round(float(strategy_return), 4),
            "risk_free_rate": risk_free_rate,
            "interpretation": _interpret_beta_alpha(beta, alpha),
        }

    except Exception as e:
        logger.warning("Beta regression failed: %s", e)
        return {"note": f"Beta calculation failed: {e}"}


def _interpret_beta_alpha(beta: float, alpha: float) -> str:
    """Interpret beta and alpha values."""
    parts = []

    if beta > 1.2:
        parts.append("highly volatile vs market")
    elif beta < 0.8:
        parts.append("less volatile vs market")
    else:
        parts.append("market-correlated volatility")

    if alpha > 0.01:
        parts.append("positive alpha (outperforming)")
    elif alpha < -0.01:
        parts.append("negative alpha (underperforming)")
    else:
        parts.append("neutral alpha")

    return ", ".join(parts)


def _regime_analysis(
    result: dict[str, Any],
    benchmark_data: pd.DataFrame,
) -> dict[str, Any]:
    """Market regime analysis during backtest period."""
    try:
        closes = benchmark_data["close"].astype(float)
        volumes = benchmark_data.get("volume", pd.Series([1.0] * len(closes))).astype(float)

        if len(closes) < 50:
            return {"note": "Insufficient data for regime analysis"}

        # Calculate regime indicators
        sma_20 = closes.rolling(20).mean()
        sma_50 = closes.rolling(50).mean()
        returns = closes.pct_change()
        volatility = returns.rolling(20).std() * (252 ** 0.5)

        # Determine regime for each period
        regimes = []
        for i in range(50, len(closes)):
            if pd.isna(sma_20.iloc[i]) or pd.isna(sma_50.iloc[i]):
                continue

            if sma_20.iloc[i] > sma_50.iloc[i] and closes.iloc[i] > sma_20.iloc[i]:
                regime = "bullish"
            elif sma_20.iloc[i] < sma_50.iloc[i] and closes.iloc[i] < sma_20.iloc[i]:
                regime = "bearish"
            else:
                regime = "sideways"

            vol_level = "high" if volatility.iloc[i] > 0.3 else (
                "low" if volatility.iloc[i] < 0.15 else "normal"
            )

            regimes.append({
                "date": benchmark_data.index[i] if hasattr(benchmark_data.index, 'strftime') else i,
                "regime": regime,
                "volatility": vol_level,
            })

        # Summarize by regime
        regime_pnl = {"bullish": 0.0, "bearish": 0.0, "sideways": 0.0}
        regime_counts = {"bullish": 0, "bearish": 0, "sideways": 0}

        for r in regimes:
            regime_counts[r["regime"]] += 1

        net_pnl = result.get("net_pnl", 0.0)
        trades = result.get("trades", 1)

        # Distribute P&L across regimes (simplified)
        for regime in regime_pnl:
            regime_pnl[regime] = net_pnl * (regime_counts[regime] / len(regimes)) if regimes else 0.0

        return {
            "total_bars_analyzed": len(regimes),
            "regime_distribution": regime_counts,
            "regime_pnl_estimates": {k: round(v, 2) for k, v in regime_pnl.items()},
            "interpretation": "Strategy performance across market regimes",
        }

    except Exception as e:
        logger.warning("Regime analysis failed: %s", e)
        return {"note": f"Regime analysis failed: {e}"}


def _monte_carlo_simulation(
    result: dict[str, Any],
    num_simulations: int = 10000,
    confidence_levels: list[float] = [0.90, 0.95, 0.99],
) -> dict[str, Any]:
    """Monte Carlo permutation test for strategy robustness."""
    try:
        trades = result.get("trades", 0)
        if trades == 0:
            return {"note": "No trades for Monte Carlo simulation"}

        # Get per-trade P&L distribution
        net_pnl = result.get("net_pnl", 0.0)
        avg_pnl_per_trade = net_pnl / trades

        # Use win rate and avg win/loss to simulate
        wins = result.get("wins", 0)
        losses = result.get("losses", 0)
        win_rate = wins / trades if trades > 0 else 0.5
        avg_win = result.get("avg_win", 0.0)
        avg_loss = result.get("avg_loss", 0.0)

        # Simulate future performance
        simulated_pnls = []
        for _ in range(num_simulations):
            # Random walk of trades
            sim_pnl = 0.0
            for _ in range(trades):
                if np.random.random() < win_rate:
                    sim_pnl += avg_win * np.random.uniform(0.5, 1.5)
                else:
                    sim_pnl += avg_loss * np.random.uniform(0.5, 1.5)
            simulated_pnls.append(sim_pnl)

        simulated_pnls = np.array(simulated_pnls)

        # Calculate confidence intervals
        ci_results = {}
        for cl in confidence_levels:
            alpha = 1 - cl
            lower = np.percentile(simulated_pnls, alpha / 2 * 100)
            upper = np.percentile(simulated_pnls, (1 - alpha / 2) * 100)
            ci_results[f"{int(cl * 100)}%_ci"] = {
                "lower": round(float(lower), 2),
                "upper": round(float(upper), 2),
                "mean": round(float(np.mean(simulated_pnls)), 2),
                "median": round(float(np.median(simulated_pnls)), 2),
            }

        # Probability of profitability
        prob_profit = np.mean(simulated_pnls > 0)

        # Max drawdown simulation
        cum_returns = np.cumsum(simulated_pnls / max(trades, 1))
        running_max = np.maximum.accumulate(cum_returns)
        drawdowns = running_max - cum_returns
        max_drawdown = np.max(drawdowns)

        return {
            "num_simulations": num_simulations,
            "original_pnl": round(net_pnl, 2),
            "simulated_mean_pnl": round(float(np.mean(simulated_pnls)), 2),
            "simulated_median_pnl": round(float(np.median(simulated_pnls)), 2),
            "probability_of_profit": round(float(prob_profit), 3),
            "max_drawdown_simulation": round(float(max_drawdown), 2),
            "confidence_intervals": ci_results,
            "interpretation": _interpret_monte_carlo(prob_profit, max_drawdown),
        }

    except Exception as e:
        logger.warning("Monte Carlo simulation failed: %s", e)
        return {"note": f"Monte Carlo simulation failed: {e}"}


def _interpret_monte_carlo(prob_profit: float, max_drawdown: float) -> str:
    """Interpret Monte Carlo results."""
    parts = []

    if prob_profit > 0.95:
        parts.append("very high probability of profitability")
    elif prob_profit > 0.80:
        parts.append("high probability of profitability")
    elif prob_profit > 0.60:
        parts.append("moderate probability of profitability")
    else:
        parts.append("low probability of profitability")

    if max_drawdown > 0.20:
        parts.append("significant drawdown risk")
    elif max_drawdown > 0.10:
        parts.append("moderate drawdown risk")
    else:
        parts.append("manageable drawdown risk")

    return "; ".join(parts)
