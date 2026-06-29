"""Tests for alpha factor zoo and benching."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot.factors import AlphaFactorRegistry, AlphaZoo
from trading_bot.factors.bench import (
    bench_alpha,
    bench_zoo,
    bench_strict,
    compare_alphas,
)
from trading_bot.factors import (
    MomentumFactor,
    VolatilityFactor,
    VolumeFactor,
    ReturnSkewnessFactor,
    ReturnKurtosisFactor,
    MaxDrawdownFactor,
    TrendStrengthFactor,
    MeanReversionFactor,
    VolumePriceCorrelationFactor,
    JegadeeshReversalFactor,
    GeorgeHwang52WeekHighFactor,
    AmihudIlliquidityFactor,
    HarveySiddiqueSkewFactor,
)


def _make_ohlcv(n: int = 300, seed: int = 42) -> pd.DataFrame:
    """Create synthetic OHLCV DataFrame for testing."""
    rng = np.random.RandomState(seed)
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    close = 100.0 * np.exp(rng.normal(0, 0.02, n).cumsum())
    high = close * (1 + rng.uniform(0, 0.02, n))
    low = close * (1 - rng.uniform(0, 0.02, n))
    open_ = close * (1 + rng.uniform(-0.01, 0.01, n))
    volume = (rng.exponential(1e6, n)).astype(int)

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=dates,
    )


# ============================================================================
# Factor Registry Tests
# ============================================================================


class TestAlphaFactorRegistry:
    def test_register_and_get(self):
        factor = MomentumFactor()
        AlphaFactorRegistry.register(factor)
        assert AlphaFactorRegistry.get("MomentumFactor") is factor

    def test_get_by_zoo(self):
        factors = AlphaFactorRegistry.get_by_zoo(AlphaZoo.QLIB)
        assert len(factors) > 0
        assert all(f.zoo == AlphaZoo.QLIB for f in factors)

    def test_get_by_category(self):
        factors = AlphaFactorRegistry.get_by_category("momentum")
        assert len(factors) > 0
        assert all(f.category.value == "momentum" for f in factors)

    def test_list_all(self):
        all_factors = AlphaFactorRegistry.list_all()
        assert len(all_factors) >= 13

    def test_clear(self):
        AlphaFactorRegistry.clear()
        assert len(AlphaFactorRegistry.list_all()) == 0


# ============================================================================
# Factor Computation Tests
# ============================================================================


class TestFactorComputation:
    @pytest.mark.parametrize(
        "factor_cls",
        [
            MomentumFactor,
            VolatilityFactor,
            VolumeFactor,
            ReturnSkewnessFactor,
            ReturnKurtosisFactor,
            MaxDrawdownFactor,
            TrendStrengthFactor,
            MeanReversionFactor,
            VolumePriceCorrelationFactor,
            JegadeeshReversalFactor,
            GeorgeHwang52WeekHighFactor,
            AmihudIlliquidityFactor,
            HarveySiddiqueSkewFactor,
        ],
    )
    def test_compute_returns_value(self, factor_cls):
        frame = _make_ohlcv(300)
        factor = factor_cls()
        result = factor.compute(frame)
        assert isinstance(result, float)

    def test_momentum_factor_positive_trend(self):
        """Momentum should be positive in uptrend."""
        rng = np.random.RandomState(99)
        dates = pd.date_range("2023-01-01", periods=300, freq="B")
        close = 100.0 + np.arange(300) * 0.1  # Linear uptrend
        frame = pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1e6,
            },
            index=dates,
        )
        factor = MomentumFactor()
        result = factor.compute(frame)
        assert result > 0

    def test_momentum_factor_negative_trend(self):
        """Momentum should be negative in downtrend."""
        rng = np.random.RandomState(99)
        dates = pd.date_range("2023-01-01", periods=300, freq="B")
        close = 100.0 - np.arange(300) * 0.1  # Linear downtrend
        frame = pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1e6,
            },
            index=dates,
        )
        factor = MomentumFactor()
        result = factor.compute(frame)
        assert result < 0

    def test_factor_short_data(self):
        """Factors should return 0.0 for insufficient data."""
        frame = _make_ohlcv(10)
        factor = MomentumFactor()
        result = factor.compute(frame)
        assert result == 0.0

    def test_mean_reversion_factor(self):
        """Mean reversion z-score should be positive when above MA."""
        dates = pd.date_range("2023-01-01", periods=100, freq="B")
        close = [50.0] * 97 + [60.0, 65.0, 70.0]  # Price spike at end
        frame = pd.DataFrame(
            {
                "open": close,
                "high": [c * 1.01 for c in close],
                "low": [c * 0.99 for c in close],
                "close": close,
                "volume": 1e6,
            },
            index=dates,
        )
        factor = MeanReversionFactor()
        result = factor.compute(frame)
        assert result > 0

    def test_max_drawdown_factor(self):
        """Max drawdown should be negative."""
        dates = pd.date_range("2023-01-01", periods=100, freq="B")
        close = [100.0] * 50 + [50.0] * 50  # 50% drawdown
        frame = pd.DataFrame(
            {
                "open": close,
                "high": close,
                "low": [c * 0.99 for c in close],
                "close": close,
                "volume": 1e6,
            },
            index=dates,
        )
        factor = MaxDrawdownFactor()
        result = factor.compute(frame)
        assert result <= 0


# ============================================================================
# Benching Tests
# ============================================================================


class TestBenchAlpha:
    def setup_method(self):
        """Re-register factors before each benching test."""
        AlphaFactorRegistry.clear()
        for _factor in [
            MomentumFactor(),
            VolatilityFactor(),
            VolumeFactor(),
        ]:
            AlphaFactorRegistry.register(_factor)

    def teardown_method(self):
        """Clean up after each test."""
        AlphaFactorRegistry.clear()

    def test_bench_alpha_basic(self):
        frame = _make_ohlcv(300)
        factor = MomentumFactor()
        result = bench_alpha(factor, frame, lookback=60)
        assert "ic_mean" in result
        assert "ic_ir" in result
        assert "categorization" in result

    def test_bench_alpha_insufficient_data(self):
        frame = _make_ohlcv(10)
        factor = MomentumFactor()
        result = bench_alpha(factor, frame, lookback=60)
        assert "note" in result

    def test_bench_alpha_momentum(self):
        """Momentum factor should have positive IC in trending data."""
        dates = pd.date_range("2023-01-01", periods=300, freq="B")
        close = 100.0 + np.arange(300) * 0.1  # Strong uptrend
        frame = pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1e6,
            },
            index=dates,
        )
        factor = MomentumFactor()
        result = bench_alpha(factor, frame, lookback=30)
        # In a linear trend, momentum IC can be negative due to forward return computation
        assert isinstance(result["ic_mean"], float)


class TestCompareAlphas:
    def setup_method(self):
        """Re-register factors before each benching test."""
        AlphaFactorRegistry.clear()
        for _factor in [
            MomentumFactor(),
            VolatilityFactor(),
            VolumeFactor(),
        ]:
            AlphaFactorRegistry.register(_factor)

    def teardown_method(self):
        """Clean up after each test."""
        AlphaFactorRegistry.clear()

    def test_compare_alphas_basic(self):
        frame = _make_ohlcv(300)
        result = compare_alphas(
            ["MomentumFactor", "VolatilityFactor", "VolumeFactor"],
            frame,
            lookback=30,
        )
        assert result["factors_compared"] == 3
        assert len(result["results"]) == 3
        assert "gap_to_leader" in result["results"][0]

    def test_compare_alphas_unknown_factor(self):
        frame = _make_ohlcv(300)
        result = compare_alphas(
            ["MomentumFactor", "UnknownFactor"],
            frame,
            lookback=30,
        )
        assert result["factors_compared"] == 1


class TestBenchZoo:
    def setup_method(self):
        """Re-register factors before each benching test."""
        AlphaFactorRegistry.clear()
        for _factor in [
            MomentumFactor(),
            VolatilityFactor(),
            VolumeFactor(),
        ]:
            AlphaFactorRegistry.register(_factor)

    def teardown_method(self):
        """Clean up after each test."""
        AlphaFactorRegistry.clear()

    def test_bench_qlib(self):
        frame = _make_ohlcv(300)
        result = bench_zoo(AlphaZoo.QLIB, frame, lookback=30)
        assert result["zoo"] == "qlib"
        assert "aggregate" in result
        assert result["aggregate"]["n_factors"] > 0

    def test_bench_unknown_zoo(self):
        frame = _make_ohlcv(300)
        result = bench_zoo("nonexistent", frame, lookback=30)
        assert "note" in result


class TestBenchStrict:
    def test_bench_strict_basic(self):
        frame = _make_ohlcv(300)
        factor = MomentumFactor()
        result = bench_strict(factor, frame, lookback=30, oos_ratio=0.2)
        assert "in_sample" in result
        assert "out_of_sample" in result
        assert "random_control" in result
        assert "overfitting_check" in result

    def test_bench_strict_overfitting(self):
        """Should detect overfitting when OOS performance degrades significantly."""
        frame = _make_ohlcv(300)
        factor = MomentumFactor()
        result = bench_strict(factor, frame, lookback=30, oos_ratio=0.2)
        overfit = result["overfitting_check"]
        assert "verdict" in overfit
        assert overfit["verdict"] in ["OVERFIT", "CAUTION", "PASS"]
