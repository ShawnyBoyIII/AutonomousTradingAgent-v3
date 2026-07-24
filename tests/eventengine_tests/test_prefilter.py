"""Stage 4b: vectorized pre-filter for Bollinger / z-score mean reversion."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from event_engine.exceptions import EventValidationError
from event_engine.prefilter import (
    PreFilterParameterScore,
    PreFilterResult,
    VectorizedPreFilter,
)


def _ou_series(n: int = 500, *, drift: float = 0.0, vol: float = 0.01,
              seed: int = 0) -> pd.Series:
    """Synthesize an Ornstein-Uhlenbeck-style series with a mean-reverting drift.

    The output is what the pre-filter ``screen`` method expects:
    a numeric ``pandas.Series`` whose index has a stable cadence
    (here, integer step labels).
    """
    rng = np.random.default_rng(seed)
    eps = rng.normal(0, vol, n)
    out = np.zeros(n, dtype=np.float64)
    out[0] = 100.0
    for i in range(1, n):
        out[i] = out[i - 1] - drift * (out[i - 1] - 100.0) + eps[i]
    return pd.Series(out)


def test_constructor_requires_lookbacks_and_entry_zs():
    with pytest.raises(EventValidationError):
        VectorizedPreFilter(lookbacks=[], entry_zs=[1.0])
    with pytest.raises(EventValidationError):
        VectorizedPrefilter_silly = VectorizedPreFilter(lookbacks=[10], entry_zs=[])
    with pytest.raises(EventValidationError):
        VectorizedPreFilter(lookbacks=[10], entry_zs=[1.0], exit_z=-0.1)


def test_screen_returns_only_combinations_meeting_min_trades():
    pre = VectorizedPreFilter(
        lookbacks=[10, 20],
        entry_zs=[1.0, 2.0, 3.0],
        min_trades=2,
    )
    prices = _ou_series(n=400, drift=0.5, vol=0.02, seed=7)  # mean-reverting
    result = pre.screen(prices, top_n=4, signal_scale_qty=10)
    assert isinstance(result, PreFilterResult)
    assert 0 < len(result.scores) <= 4
    for score in result.scores:
        assert score.trade_count >= 2
        # Parameters snapshot is a dict with the expected keys.
        assert set(score.parameters) == {
            "lookback", "entry_z", "exit_z", "signal_scale_qty",
        }


def test_screen_orders_results_by_edge_score_descending():
    pre = VectorizedPreFilter(
        lookbacks=[20, 30, 50],
        entry_zs=[1.5, 2.0],
        min_trades=3,
    )
    prices = _ou_series(n=500, drift=0.6, vol=0.015, seed=11)
    result = pre.screen(prices, top_n=5)
    if len(result.scores) >= 2:
        for prev, nxt in zip(result.scores, result.scores[1:]):
            assert prev.edge_score >= nxt.edge_score


def test_screen_rejects_empty_prices():
    pre = VectorizedPreFilter(lookbacks=[10], entry_zs=[1.0])
    with pytest.raises(EventValidationError):
        pre.screen(pd.Series([], dtype="float64"))


def test_screen_rejects_nonpositive_prices():
    pre = VectorizedPreFilter(lookbacks=[10], entry_zs=[1.0])
    bad = pd.Series([100.0, 0.0, 99.0])
    with pytest.raises(EventValidationError):
        pre.screen(bad)


def test_screen_rejects_prices_with_persistent_nan():
    """A series that is *entirely* NaN can't be filled by ffill+bfill
    and is rejected. Series with some NaN that gets fully filled are
    accepted.
    """
    pre = VectorizedPrefilter_silly = VectorizedPreFilter(lookbacks=[10], entry_zs=[1.0])
    all_nan = pd.Series([np.nan] * 20)
    with pytest.raises(EventValidationError):
        pre.screen(all_nan)


def test_screen_handles_constant_prices():
    """A perfectly flat series has zero std; the screen must still
    finish without raising, even though every combination reports
    zero trades (and is therefore filtered by ``min_trades``)."""
    pre = VectorizedPreFilter(
        lookbacks=[10, 20], entry_zs=[1.0, 2.0], min_trades=1
    )
    prices = pd.Series([100.0] * 200)
    result = pre.screen(prices)
    assert isinstance(result, PreFilterResult)


def test_sharpe_is_finite_under_default_configurations():
    """Cap on extreme Sharpe values keeps the matrix sweep finite."""
    pre = VectorizedPreFilter(
        lookbacks=[20], entry_zs=[1.0], min_trades=1
    )
    prices = _ou_series(n=300, drift=0.0, vol=0.005, seed=3)
    result = pre.screen(prices)
    for score in result.scores:
        assert -10.0 <= score.sharpe <= 10.0


def test_top_n_limits_result_size():
    pre = VectorizedPreFilter(
        lookbacks=[10, 20, 30],
        entry_zs=[1.0, 1.5, 2.0],
        min_trades=1,
    )
    prices = _ou_series(n=600, drift=0.5, vol=0.02, seed=5)
    result = pre.screen(prices, top_n=2)
    assert len(result.scores) <= 2


def test_make_strategy_round_trip():
    """make_strategy must produce a strategy with the exact
    parameters from the pre-filter output (so the event-driven core
    uses the same configuration the vectorized sweep evaluated)."""
    pre = VectorizedPreFilter(
        lookbacks=[20], entry_zs=[2.0], exit_z=0.5
    )
    prices = _ou_series(n=300, drift=0.4, vol=0.02, seed=4)
    result = pre.screen(prices, top_n=1, signal_scale_qty=25)
    if result.scores:
        params = result.scores[0].parameters
        strategy = pre.make_strategy(params)
        from event_engine.strategy import BollingerZScoreReversionStrategy
        assert isinstance(strategy, BollingerZScoreReversionStrategy)
        assert strategy.lookback == params["lookback"]
        assert strategy.entry_z == params["entry_z"]
        assert strategy.exit_z == params["exit_z"]
        assert strategy.signal_scale_qty == params["signal_scale_qty"]


def test_metric_caps_apply_uniformly():
    """``PreFilterParameterScore.from_metrics`` caps the edge score at
    4 decimal places; this test guards that contract."""
    s = PreFilterParameterScore.from_metrics(
        parameters={"lookback": 10, "entry_z": 2.0,
                   "exit_z": 0.0, "signal_scale_qty": 5},
        sharpe=1.23456789,
        total_return=0.23456789,
        max_drawdown=-0.23456789,
        trade_count=10,
    )
    # Edge score rounded to 4 dp: sharpe=1.2346, dd=0.2346,
    # penalty=0.5*0.2346=0.1173; edge = 1.2346 - 0.1173 = 1.1173.
    assert s.sharpe == 1.2346
    assert s.total_return == 0.2346
    assert s.max_drawdown == -0.2346
    assert s.edge_score == 1.1173
