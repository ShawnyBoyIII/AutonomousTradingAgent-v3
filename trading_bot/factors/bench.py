"""Alpha factor benching and comparison utilities.

Provides:
- IC (Information Coefficient) calculation
- IR (Information Ratio) calculation
- IC-positive ratio
- Alive/reversed/dead factor categorization
- Head-to-head alpha comparison
- Random control benchmarking
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from trading_bot.factors import AlphaFactor, AlphaFactorRegistry, AlphaZoo

logger = logging.getLogger(__name__)


def bench_alpha(
    factor: AlphaFactor,
    frame: pd.DataFrame,
    forward_returns: pd.Series | None = None,
    lookback: int = 60,
) -> dict[str, Any]:
    """Benchmark a single alpha factor.

    Args:
        factor: Alpha factor to benchmark.
        frame: OHLCV DataFrame with timestamps.
        forward_returns: Optional pre-computed forward returns.
        lookback: Lookback period for IC calculation.

    Returns:
        Benchmark results with IC, IR, and categorization.
    """
    try:
        # Compute factor values
        factor_values = _compute_factor_series(factor, frame, lookback)

        if forward_returns is None:
            # Compute forward returns from price data
            forward_returns = _compute_forward_returns(frame)

        # Calculate IC (correlation between factor and returns)
        common_index = factor_values.dropna().index.intersection(
            forward_returns.dropna().index
        )
        if len(common_index) < 20:
            return {"note": "Insufficient data for IC calculation"}

        ic_series = pd.Series(
            index=common_index,
            dtype=float,
        )
        for i in range(lookback, len(common_index)):
            idx = common_index[i]
            prev_idx = common_index[i - lookback]

            factor_window = factor_values[prev_idx:idx].dropna()
            return_window = forward_returns[prev_idx:idx].dropna()

            if len(factor_window) < 10 or len(return_window) < 10:
                continue

            ic = factor_window.corr(return_window)
            if not np.isnan(ic):
                ic_series[idx] = ic

        # Calculate statistics
        ic_mean = float(ic_series.mean()) if not ic_series.dropna().empty else 0.0
        ic_std = float(ic_series.std()) if ic_series.dropna().shape[0] > 1 else 0.0
        ic_ir = ic_mean / ic_std if ic_std > 0 else 0.0
        ic_positive_ratio = float((ic_series.dropna() > 0).mean()) if not ic_series.dropna().empty else 0.0

        # Categorize factor
        categorization = _categorize_factor(ic_mean, ic_ir, ic_positive_ratio)

        return {
            "factor_name": factor.__class__.__name__,
            "zoo": factor.zoo.value,
            "category": factor.category.value,
            "description": factor.description,
            "ic_mean": round(float(ic_mean), 4),
            "ic_std": round(float(ic_std), 4),
            "ic_ir": round(float(ic_ir), 4),
            "ic_positive_ratio": round(float(ic_positive_ratio), 4),
            "n_observations": len(ic_series),
            "categorization": categorization,
        }

    except Exception as e:
        logger.warning("Benching failed for %s: %s", factor.__class__.__name__, e)
        return {"note": f"Benching failed: {e}"}


def compare_alphas(
    factor_names: list[str],
    frame: pd.DataFrame,
    forward_returns: pd.Series | None = None,
    lookback: int = 60,
    sort_by: str = "ic_ir",
) -> dict[str, Any]:
    """Compare multiple alpha factors head-to-head.

    Args:
        factor_names: List of factor names to compare.
        frame: OHLCV DataFrame.
        forward_returns: Optional pre-computed forward returns.
        lookback: Lookback period for IC calculation.
        sort_by: Metric to sort by (ic_ir, ic_mean, ic_positive_ratio).

    Returns:
        Comparison results ranked by specified metric.
    """
    results = []

    for name in factor_names:
        factor = AlphaFactorRegistry.get(name)
        if factor is None:
            logger.warning("Factor '%s' not found, skipping", name)
            continue

        result = bench_alpha(factor, frame, forward_returns, lookback)
        if "note" not in result:
            results.append(result)

    # Sort by specified metric
    if results and sort_by in results[0]:
        results.sort(key=lambda x: x[sort_by], reverse=True)

    # Add gap to leader
    if results:
        leader_value = results[0].get(sort_by, 0)
        for r in results:
            value = r.get(sort_by, 0)
            r["gap_to_leader"] = round(value - leader_value, 4)

    return {
        "factors_compared": len(results),
        "sort_by": sort_by,
        "results": results,
    }


def bench_zoo(
    zoo: AlphaZoo | str,
    frame: pd.DataFrame,
    forward_returns: pd.Series | None = None,
    lookback: int = 60,
) -> dict[str, Any]:
    """Benchmark all factors in a zoo.

    Args:
        zoo: Zoo to benchmark.
        frame: OHLCV DataFrame.
        forward_returns: Optional pre-computed forward returns.
        lookback: Lookback period for IC calculation.

    Returns:
        Zoo benchmark results with per-factor and aggregate statistics.
    """
    if isinstance(zoo, str):
        try:
            zoo = AlphaZoo(zoo)
        except ValueError:
            return {"note": f"Unknown zoo: '{zoo}'. Valid zoos: {[z.value for z in AlphaZoo]}"}

    factors = AlphaFactorRegistry.get_by_zoo(zoo)
    if not factors:
        return {"note": f"No factors found for zoo '{zoo.value}'"}

    results = []
    for factor in factors:
        result = bench_alpha(factor, frame, forward_returns, lookback)
        if "note" not in result:
            results.append(result)

    # Aggregate statistics
    if results:
        ic_means = [r["ic_mean"] for r in results]
        ic_irs = [r["ic_ir"] for r in results]
        ic_positive_ratios = [r["ic_positive_ratio"] for r in results]

        aggregate = {
            "n_factors": len(results),
            "avg_ic_mean": round(float(np.mean(ic_means)), 4),
            "avg_ic_ir": round(float(np.mean(ic_irs)), 4),
            "avg_ic_positive_ratio": round(float(np.mean(ic_positive_ratios)), 4),
            "best_ic_ir": max(results, key=lambda x: x["ic_ir"])["ic_ir"],
            "worst_ic_ir": min(results, key=lambda x: x["ic_ir"])["ic_ir"],
        }
    else:
        aggregate = {"n_factors": 0}

    return {
        "zoo": zoo.value,
        "aggregate": aggregate,
        "factors": results,
    }


def bench_strict(
    factor: AlphaFactor,
    frame: pd.DataFrame,
    forward_returns: pd.Series | None = None,
    lookback: int = 60,
    oos_ratio: float = 0.2,
) -> dict[str, Any]:
    """Strict alpha benching with random control and OOS split.

    Args:
        factor: Alpha factor to benchmark.
        frame: OHLCV DataFrame.
        forward_returns: Optional pre-computed forward returns.
        lookback: Lookback period for IC calculation.
        oos_ratio: Out-of-sample ratio (e.g., 0.2 = 20% OOS).

    Returns:
        Strict benching results with in-sample, out-of-sample, and random control.
    """
    try:
        # Split into in-sample and out-of-sample
        n = len(frame)
        oos_start = int(n * (1 - oos_ratio))

        is_frame = frame.iloc[:oos_start]
        oos_frame = frame.iloc[oos_start:]

        # In-sample benching
        is_result = bench_alpha(factor, is_frame, forward_returns, lookback)

        # Out-of-sample benching
        oos_result = bench_alpha(factor, oos_frame, forward_returns, lookback)

        # Random control (shuffled returns)
        if forward_returns is None:
            forward_returns = _compute_forward_returns(frame)

        shuffled_returns = forward_returns.sample(frac=1, random_state=42).reindex(
            forward_returns.index
        )
        random_result = bench_alpha(factor, frame, shuffled_returns, lookback)

        return {
            "factor_name": factor.__class__.__name__,
            "in_sample": is_result,
            "out_of_sample": oos_result,
            "random_control": random_result,
            "overfitting_check": _check_overfitting(is_result, oos_result),
        }

    except Exception as e:
        logger.warning("Strict benching failed for %s: %s", factor.__class__.__name__, e)
        return {"note": f"Strict benching failed: {e}"}


def _compute_factor_series(
    factor: AlphaFactor,
    frame: pd.DataFrame,
    lookback: int,
) -> pd.Series:
    """Compute time series of factor values."""
    factor_values = []
    dates = frame.index

    for i in range(lookback, len(frame)):
        window = frame.iloc[: i + 1]
        try:
            value = factor.compute(window)
            factor_values.append(value)
        except Exception:
            factor_values.append(np.nan)

    return pd.Series(factor_values, index=dates[lookback:])


def _compute_forward_returns(frame: pd.DataFrame, horizon: int = 21) -> pd.Series:
    """Compute forward returns from price data."""
    closes = frame["close"].astype(float)
    return closes.pct_change(horizon).shift(-horizon)


def _categorize_factor(
    ic_mean: float,
    ic_ir: float,
    ic_positive_ratio: float,
) -> str:
    """Categorize factor based on IC statistics.

    Returns:
        'alive' if factor has predictive power,
        'reversed' if factor is negatively correlated,
        'dead' if factor has no predictive power.
    """
    if abs(ic_ir) < 0.2:
        return "dead"
    elif ic_ir > 0.2 and ic_positive_ratio > 0.5:
        return "alive"
    elif ic_ir < -0.2 and ic_positive_ratio < 0.5:
        return "reversed"
    else:
        return "dead"


def _check_overfitting(
    is_result: dict[str, Any],
    oos_result: dict[str, Any],
) -> dict[str, Any]:
    """Check for overfitting between in-sample and out-of-sample results."""
    is_ic = is_result.get("ic_ir", 0)
    oos_ic = oos_result.get("ic_ir", 0)

    degradation = abs(is_ic - oos_ic) / abs(is_ic) if is_ic != 0 else 0.0

    if degradation > 0.5:
        verdict = "OVERFIT"
    elif degradation > 0.2:
        verdict = "CAUTION"
    else:
        verdict = "PASS"

    return {
        "is_ic_ir": is_ic,
        "oos_ic_ir": oos_ic,
        "degradation_pct": round(degradation * 100, 1),
        "verdict": verdict,
    }
