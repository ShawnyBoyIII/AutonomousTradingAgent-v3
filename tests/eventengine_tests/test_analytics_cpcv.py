from __future__ import annotations

from math import comb

import numpy as np
import pandas as pd
import pytest

from event_engine.analytics import (
    CombinatorialPurgedCV,
    _normalized_rank,
    apply_embargo,
    purge_overlapping_events,
)


def _event_windows(n: int = 60) -> tuple[pd.DatetimeIndex, pd.Series]:
    starts = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    ends = pd.Series(starts + pd.Timedelta(days=2), index=starts)
    return starts, ends


def test_purge_removes_training_events_overlapping_test_windows() -> None:
    starts, ends = _event_windows(12)
    train = np.array([0, 1, 2, 3, 8, 9, 10, 11])
    test = np.array([4, 5, 6, 7])

    kept = purge_overlapping_events(starts, ends, train, test)

    assert 2 not in kept
    assert 3 not in kept
    assert set(kept).issubset(set(train))


def test_embargo_removes_observations_immediately_after_test() -> None:
    train = np.array([0, 1, 2, 7, 8, 9, 10, 11])
    test = np.array([3, 4, 5, 6])

    kept = apply_embargo(train, test, n_samples=12, embargo_pct=0.25)

    assert 7 not in kept
    assert 8 not in kept
    assert 9 not in kept
    assert 10 in kept


def test_embargo_begins_after_test_event_horizon() -> None:
    starts, ends = _event_windows(12)
    ends.iloc[6] = starts[9]
    train = np.array([0, 1, 2, 7, 8, 9, 10, 11])
    test = np.array([3, 4, 5, 6])

    kept = apply_embargo(
        train,
        test,
        n_samples=12,
        embargo_pct=0.10,
        event_starts=starts,
        event_ends=ends,
    )

    assert 10 not in kept
    assert 11 not in kept


def test_pbo_rank_uses_m_plus_one_normalization() -> None:
    assert _normalized_rank(np.array([1.0, 2.0, 3.0]), 1) == pytest.approx(0.5)


def test_cpcv_generates_all_group_combinations_with_purged_splits() -> None:
    starts, ends = _event_windows(60)
    cpcv = CombinatorialPurgedCV(
        n_groups=6, n_test_groups=2, embargo_pct=0.05
    )

    splits = list(cpcv.split(starts, ends))

    assert len(splits) == comb(6, 2)
    for split in splits:
        assert len(split.test_groups) == 2
        assert not set(split.train_indices).intersection(split.test_indices)
        for train_idx in split.train_indices:
            train_start = starts[train_idx]
            train_end = ends.iloc[train_idx]
            for test_idx in split.test_indices:
                test_start = starts[test_idx]
                test_end = ends.iloc[test_idx]
                assert train_end < test_start or train_start > test_end


def test_cpcv_evaluate_returns_oos_paths_and_pbo_significance() -> None:
    starts, ends = _event_windows(120)
    rng = np.random.default_rng(42)
    returns = pd.DataFrame(
        {
            "stable": rng.normal(0.001, 0.01, 120),
            "noise_a": rng.normal(0.0, 0.012, 120),
            "noise_b": rng.normal(0.0, 0.012, 120),
            "noise_c": rng.normal(0.0, 0.012, 120),
        },
        index=starts,
    )
    cpcv = CombinatorialPurgedCV(6, 2, embargo_pct=0.02)

    result = cpcv.evaluate(returns, event_ends=ends)

    assert result.n_splits == comb(6, 2)
    assert len(result.path_returns) == comb(5, 1)
    for path in result.path_returns:
        assert path.index.equals(starts)
    assert len(result.oos_rank_logits) == result.n_splits
    assert 0.0 <= result.pbo <= 1.0
    assert 0.0 <= result.p_value <= 1.0
    assert result.significant is (result.p_value < 0.05)
    assert result.p_value_method == "strategy-label permutation"
    assert set(result.selected_strategies).issubset(set(returns.columns))


def test_cpcv_prefers_positive_deterministic_sharpe_over_volatile_trial() -> None:
    starts, ends = _event_windows(60)
    returns = pd.DataFrame(
        {
            "deterministic": np.full(60, 0.125),
            "volatile": np.tile([0.10, 0.20], 30),
        },
        index=starts,
    )

    result = CombinatorialPurgedCV(6, 2, embargo_pct=0.0).evaluate(
        returns, event_ends=ends
    )

    assert set(result.selected_strategies) == {"deterministic"}


def test_cpcv_rejects_training_folds_with_fewer_than_two_observations() -> None:
    starts = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    ends = pd.Series(starts, index=starts)
    returns = pd.DataFrame(
        {"a": [0.1, 0.2, 0.3, 0.4], "b": [-0.1, 0.1, -0.1, 0.1]},
        index=starts,
    )

    with pytest.raises(ValueError, match="at least two training observations"):
        CombinatorialPurgedCV(3, 2, embargo_pct=0.0).evaluate(
            returns, event_ends=ends
        )


def test_cpcv_rejects_test_folds_with_fewer_than_two_observations() -> None:
    starts = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    ends = pd.Series(starts, index=starts)
    returns = pd.DataFrame(
        {"a": [0.1, 0.2, 0.3, 0.4], "b": [-0.1, 0.1, -0.1, 0.1]},
        index=starts,
    )

    with pytest.raises(ValueError, match="at least two test observations"):
        CombinatorialPurgedCV(3, 1, embargo_pct=0.0).evaluate(
            returns, event_ends=ends
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_groups": 1, "n_test_groups": 1},
        {"n_groups": 4, "n_test_groups": 4},
        {"n_groups": 4, "n_test_groups": 2, "embargo_pct": -0.1},
    ],
)
def test_cpcv_rejects_invalid_configuration(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(**kwargs)
