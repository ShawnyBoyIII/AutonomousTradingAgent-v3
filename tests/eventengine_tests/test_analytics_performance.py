from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from event_engine.analytics import PerformanceAnalytics


def _equity(values: list[float]) -> pd.Series:
    return pd.Series(
        values,
        index=pd.date_range("2024-01-02", periods=len(values), freq="B", tz="UTC"),
        dtype="float64",
        name="equity",
    )


def test_r_multiples_use_initial_dollar_risk_and_skip_zero_risk() -> None:
    trades = pd.DataFrame(
        {"pnl": [100.0, -50.0, 25.0], "initial_risk": [50.0, 25.0, 0.0]}
    )
    analytics = PerformanceAnalytics(_equity([100.0, 101.0]), trades)

    result = analytics.r_multiples()

    assert result.iloc[0] == 2.0
    assert result.iloc[1] == -2.0
    assert math.isnan(result.iloc[2])


def test_sqn_uses_100_trade_cap_and_reports_one_sided_significance() -> None:
    r_values = np.tile([1.0, 2.0, -0.5, 1.5], 30)
    trades = pd.DataFrame({"pnl": r_values * 100.0, "initial_risk": 100.0})
    analytics = PerformanceAnalytics(_equity([100.0, 101.0]), trades)

    result = analytics.sqn()
    expected = math.sqrt(100) * np.mean(r_values) / np.std(r_values, ddof=1)

    assert result.trade_count == 120
    assert result.sqn_100 == pytest.approx(expected)
    assert result.p_value < 0.05
    assert result.significant is True
    assert result.rating in {"Excellent", "Superb", "Holy Grail"}


@pytest.mark.parametrize(
    ("sqn", "rating"),
    [
        (1.0, "Poor"),
        (1.8, "Below Average"),
        (2.2, "Average"),
        (2.7, "Good"),
        (4.0, "Excellent"),
        (6.0, "Superb"),
        (7.0, "Holy Grail"),
    ],
)
def test_tharp_rating_boundaries(sqn: float, rating: str) -> None:
    assert PerformanceAnalytics.tharp_rating(sqn) == rating


def test_standard_metrics_include_drawdown_and_duration() -> None:
    equity = _equity([100.0, 110.0, 88.0, 90.0, 115.0, 120.0])
    analytics = PerformanceAnalytics(equity, periods_per_year=252)

    result = analytics.metrics()

    assert result.max_drawdown == pytest.approx(0.20)
    assert result.max_drawdown_duration == 3
    assert result.cagr > 0.0
    assert result.annualized_volatility > 0.0
    assert math.isfinite(result.sortino_ratio)
    assert result.calmar_ratio == pytest.approx(result.cagr / 0.20)


def test_flat_equity_handles_all_zero_denominators() -> None:
    result = PerformanceAnalytics(_equity([100.0] * 10)).metrics()

    assert result.cagr == 0.0
    assert result.annualized_volatility == 0.0
    assert result.sortino_ratio == 0.0
    assert result.calmar_ratio == 0.0
    assert result.max_drawdown == 0.0
    assert result.max_drawdown_duration == 0


def test_sortino_uses_target_semideviation_across_all_observations() -> None:
    returns = np.array([0.02, -0.01, 0.02, -0.01])
    values = 100.0 * np.cumprod(np.concatenate(([1.0], 1.0 + returns)))
    result = PerformanceAnalytics(_equity(values.tolist())).metrics()
    expected_downside = np.sqrt(np.mean(np.minimum(returns, 0.0) ** 2)) * np.sqrt(252)
    expected = np.mean(returns) * 252 / expected_downside

    assert result.sortino_ratio == pytest.approx(expected)


def test_equity_must_be_positive_dated_and_chronological() -> None:
    with pytest.raises(ValueError, match="DatetimeIndex"):
        PerformanceAnalytics(pd.Series([100.0, 101.0]))
    with pytest.raises(ValueError, match="positive"):
        PerformanceAnalytics(_equity([100.0, 0.0]))
    with pytest.raises(ValueError, match="chronological"):
        PerformanceAnalytics(_equity([100.0, 101.0]).sort_index(ascending=False))
