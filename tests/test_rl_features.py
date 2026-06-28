from __future__ import annotations

import pandas as pd

from trading_bot.models.portfolio import PortfolioState
from trading_bot.rl.features import (
    FEATURE_COLS,
    PORTFOLIO_FEATURES,
    build_market_feature_frame,
    build_market_feature_row,
    build_observation,
    build_portfolio_feature_row,
)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0,
                      110.0, 111.0, 112.0, 113.0, 114.0, 115.0, 116.0, 117.0, 118.0, 119.0],
            "open": [99.0] * 20,
            "high": [120.0] * 20,
            "low": [98.0] * 20,
            "volume": [1000 + i for i in range(20)],
        }
    )


def test_build_market_feature_row_returns_shared_shape() -> None:
    row = build_market_feature_row(_frame())
    assert len(row) == len(FEATURE_COLS)


def test_build_market_feature_row_uses_defaults_for_missing_indicators() -> None:
    row = build_market_feature_row(_frame().head(2))

    assert row[FEATURE_COLS.index("rsi_14")] == 50.0


def test_precomputed_market_feature_frame_keeps_same_row_shape() -> None:
    row = build_market_feature_row(build_market_feature_frame(_frame()))

    assert len(row) == len(FEATURE_COLS)


def test_build_observation_pads_window() -> None:
    market_row = build_market_feature_row(_frame())
    portfolio_row = build_portfolio_feature_row(PortfolioState(cash=100_000.0, equity=100_000.0))

    obs = build_observation([market_row], portfolio_row, observer_window=5)

    assert obs.shape == (5, len(FEATURE_COLS) + len(PORTFOLIO_FEATURES))
    assert obs[-1][-1] == 0.0
