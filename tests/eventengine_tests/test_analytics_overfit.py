from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from event_engine.analytics import DSRDiagnostics


def _returns(seed: int = 7, mean: float = 0.001, n: int = 500) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mean, 0.01, n), dtype="float64")


def test_probabilistic_sharpe_corrects_for_non_normal_moments() -> None:
    result = DSRDiagnostics.probabilistic_sharpe_ratio(
        _returns(mean=0.003), benchmark_sharpe=0.0
    )

    assert 0.5 < result.probability <= 1.0
    assert result.p_value == pytest.approx(1.0 - result.probability)
    assert result.sample_length == 500
    assert np.isfinite(result.skewness)
    assert np.isfinite(result.kurtosis)
    assert result.significant is (result.p_value < 0.05)


def test_deflated_sharpe_penalizes_more_trials() -> None:
    returns = _returns(mean=0.0015)
    trial_sharpes = np.linspace(-0.1, 0.4, 20)

    few = DSRDiagnostics.deflated_sharpe_ratio(
        returns, trial_sharpes=trial_sharpes, n_trials=5
    )
    many = DSRDiagnostics.deflated_sharpe_ratio(
        returns, trial_sharpes=trial_sharpes, n_trials=500
    )

    assert many.benchmark_sharpe > few.benchmark_sharpe
    assert many.probability < few.probability
    assert many.trial_count == 500
    assert many.trial_sharpe_variance == pytest.approx(
        np.var(trial_sharpes, ddof=1)
    )
    assert many.p_value == pytest.approx(1.0 - many.probability)


def test_zero_variance_returns_produce_neutral_psr_without_division_error() -> None:
    result = DSRDiagnostics.probabilistic_sharpe_ratio(
        pd.Series([0.0] * 50), benchmark_sharpe=0.0
    )

    assert result.observed_sharpe == 0.0
    assert result.probability == 0.5
    assert result.p_value == 0.5
    assert result.significant is False


def test_constant_positive_returns_produce_certain_psr() -> None:
    result = DSRDiagnostics.probabilistic_sharpe_ratio(
        pd.Series([0.01] * 50), benchmark_sharpe=0.0
    )

    assert np.isposinf(result.observed_sharpe)
    assert result.probability == 1.0
    assert result.p_value == 0.0
    assert result.significant is True


def test_psr_rejects_too_few_or_nonfinite_observations() -> None:
    with pytest.raises(ValueError, match="at least three"):
        DSRDiagnostics.probabilistic_sharpe_ratio(pd.Series([0.1, 0.2]))
    with pytest.raises(ValueError, match="finite"):
        DSRDiagnostics.probabilistic_sharpe_ratio(
            pd.Series([0.1, np.nan, 0.2, 0.3])
        )
