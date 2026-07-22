"""TDD: add_all_features produces a single canonical feature pipeline."""
from __future__ import annotations

import pandas as pd

from trading_bot.data.feature_pipeline import add_all_features


def _frame(periods: int = 80) -> pd.DataFrame:
    idx = pd.date_range("2026-07-21 09:30", periods=periods, freq="5min")
    return pd.DataFrame(
        {
            "open": [100 + i * 0.1 for i in range(periods)],
            "high": [101 + i * 0.1 for i in range(periods)],
            "low": [99 + i * 0.1 for i in range(periods)],
            "close": [100.5 + i * 0.1 for i in range(periods)],
            "volume": [1_000_000 + i * 100 for i in range(periods)],
        },
        index=idx,
    )


def test_add_all_features_adds_canonical_columns() -> None:
    out = add_all_features(_frame())
    for col in ["ema_20", "sma_50", "atr_14", "rsi_14", "bb_upper", "bb_lower", "vwap"]:
        assert col in out.columns


def test_add_all_features_does_not_mutate_input() -> None:
    frame = _frame()
    snapshot_cols = set(frame.columns)
    add_all_features(frame)
    assert set(frame.columns) == snapshot_cols


def test_add_all_features_handles_empty_frame() -> None:
    empty = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": [], "volume": []},
        index=pd.DatetimeIndex([]),
    )
    out = add_all_features(empty)
    assert out.empty


def test_add_all_features_works_with_minimal_columns() -> None:
    """A frame with only close should not crash; missing features are simply absent."""
    minimal = pd.DataFrame(
        {"close": [100.0, 101.0, 102.0]},
        index=pd.date_range("2026-07-21 09:30", periods=3, freq="5min"),
    )
    out = add_all_features(minimal)
    # atr requires high/low, may be absent; rsi may be present
    assert "close" in out.columns
