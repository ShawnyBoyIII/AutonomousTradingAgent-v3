from __future__ import annotations

import math
import pytest
import numpy as np
import pandas as pd

from trading_bot.data.feature_engineering import GroupByScaler, FeatureEngineer


class TestGroupByScaler:
    def test_groupby_scaler_zscore_normalizes(self) -> None:
        scaler = GroupByScaler(window=60, method="zscore")
        values = [float(i) for i in range(100)]

        result = scaler.fit_transform(values, "TEST")

        assert len(result) == 100
        assert not np.isnan(result).any()
        assert result.dtype == np.float64

    def test_groupby_scaler_minmax_normalizes(self) -> None:
        scaler = GroupByScaler(window=60, method="minmax")
        values = [float(i) for i in range(100)]

        result = scaler.fit_transform(values, "TEST")

        assert len(result) == 100
        assert np.nanmin(result) >= 0.0
        assert np.nanmax(result) <= 1.0

    def test_groupby_scaler_per_ticker_independence(self) -> None:
        scaler = GroupByScaler(window=10, method="zscore")

        values_a = [10.0, 20.0, 30.0, 40.0, 50.0]
        values_b = [100.0, 90.0, 80.0, 70.0, 60.0]

        result_a = scaler.fit_transform(values_a, "TICKER_A")
        result_b = scaler.fit_transform(values_b, "TICKER_B")

        assert len(result_a) == 5
        assert len(result_b) == 5
        assert not np.array_equal(result_a, result_b)

    def test_groupby_scaler_handles_nan(self) -> None:
        scaler = GroupByScaler(window=60, method="zscore")
        values = [10.0, None, 20.0, float("nan"), 30.0]

        result = scaler.fit_transform(values, "TEST")

        assert len(result) == 5
        assert np.isnan(result[1])
        assert np.isnan(result[3])
        assert not np.isnan(result[0])
        assert not np.isnan(result[2])
        assert not np.isnan(result[4])

    def test_groupby_scaler_window_limit(self) -> None:
        scaler = GroupByScaler(window=5, method="zscore")
        values = [float(i) for i in range(20)]

        result = scaler.fit_transform(values, "TEST")

        assert len(result) == 20
        assert not np.isnan(result).any()

    def test_groupby_scaler_empty_history(self) -> None:
        scaler = GroupByScaler(window=60, method="zscore")
        values = [10.0]

        result = scaler.fit_transform(values, "TEST")

        assert len(result) == 1
        assert result[0] == 0.0


class TestFeatureEngineer:
    @pytest.fixture
    def sample_frame(self) -> pd.DataFrame:
        n = 100
        base_price = 100.0
        closes = [base_price + i * 0.5 for i in range(n)]
        highs = [c + 2.0 for c in closes]
        lows = [c - 2.0 for c in closes]
        opens = [c - 0.5 for c in closes]
        volumes = [1_000_000 + i * 1000 for i in range(n)]

        frame = pd.DataFrame({
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        })
        return frame

    @pytest.fixture
    def frame_with_indicators(self, sample_frame: pd.DataFrame) -> pd.DataFrame:
        from trading_bot.data.indicators import (
            add_ema, add_sma, add_rsi, add_macd,
            add_bollinger_bands, add_stochastic, add_cci,
            add_williams_r, add_atr_percent, add_adx, add_obv,
        )

        frame = sample_frame.copy()
        frame = add_ema(frame, period=20, column_name="ema_20")
        frame = add_sma(frame, period=50, column_name="sma_50")
        frame = add_rsi(frame, period=14)
        frame = add_macd(frame)
        frame = add_bollinger_bands(frame, period=20)
        frame = add_stochastic(frame, k_period=14, d_period=3)
        frame = add_cci(frame, period=20)
        frame = add_williams_r(frame, period=14)
        frame = add_atr_percent(frame, period=14)
        frame = add_adx(frame, period=14)
        frame = add_obv(frame)
        return frame

    def test_feature_engineer_standard_features(self, frame_with_indicators: pd.DataFrame) -> None:
        engineer = FeatureEngineer(feature_set="standard")

        state = engineer.build_state(
            frame=frame_with_indicators,
            ticker="TEST",
            portfolio_weight=0.15,
            unrealized_pnl_pct=0.02,
            cash_ratio=0.85,
        )

        assert len(state) == engineer.state_size
        assert len(engineer.feature_names) == 16
        assert engineer.state_size == 19
        assert len(state) == 19

    def test_feature_engineer_extended_features(self, frame_with_indicators: pd.DataFrame) -> None:
        engineer = FeatureEngineer(feature_set="extended")

        state = engineer.build_state(
            frame=frame_with_indicators,
            ticker="TEST",
            portfolio_weight=0.15,
            unrealized_pnl_pct=0.02,
            cash_ratio=0.85,
        )

        assert len(state) == engineer.state_size
        assert len(engineer.feature_names) == 21
        assert engineer.state_size == 24
        assert len(state) == 24

    def test_feature_engineer_state_size(self) -> None:
        engineer_standard = FeatureEngineer(feature_set="standard")
        engineer_extended = FeatureEngineer(feature_set="extended")

        assert engineer_standard.state_size == 19
        assert engineer_extended.state_size == 24
        assert len(engineer_standard.feature_names) == 16
        assert len(engineer_extended.feature_names) == 21

    def test_feature_engineer_with_realistic_frame(self, frame_with_indicators: pd.DataFrame) -> None:
        engineer = FeatureEngineer(feature_set="standard")

        state = engineer.build_state(
            frame=frame_with_indicators,
            ticker="AAPL",
            portfolio_weight=0.0,
            unrealized_pnl_pct=0.0,
            cash_ratio=1.0,
        )

        assert isinstance(state, np.ndarray)
        assert state.dtype == np.float64
        assert len(state) == 19
        assert not np.isnan(state).all()

    def test_feature_engineer_handles_missing_indicators(self, sample_frame: pd.DataFrame) -> None:
        engineer = FeatureEngineer(feature_set="standard")

        state = engineer.build_state(
            frame=sample_frame,
            ticker="TEST",
            portfolio_weight=0.0,
            unrealized_pnl_pct=0.0,
            cash_ratio=1.0,
        )

        assert len(state) == 19
        assert np.isnan(state).any()
        assert not np.isnan(state[-3:]).any()

    def test_feature_engineer_insufficient_history(self) -> None:
        engineer = FeatureEngineer(feature_set="standard")
        small_frame = pd.DataFrame({
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1_000_000, 1_100_000],
        })

        state = engineer.build_state(
            frame=small_frame,
            ticker="TEST",
            portfolio_weight=0.0,
            unrealized_pnl_pct=0.0,
            cash_ratio=1.0,
        )

        assert len(state) == 19
        assert np.isnan(state[1:-3]).all()
        assert not np.isnan(state[-3:]).any()

    def test_feature_engineer_portfolio_features(self, frame_with_indicators: pd.DataFrame) -> None:
        engineer = FeatureEngineer(feature_set="standard")

        state1 = engineer.build_state(
            frame=frame_with_indicators,
            ticker="TEST",
            portfolio_weight=0.5,
            unrealized_pnl_pct=0.1,
            cash_ratio=0.5,
        )

        state2 = engineer.build_state(
            frame=frame_with_indicators,
            ticker="TEST",
            portfolio_weight=0.0,
            unrealized_pnl_pct=0.0,
            cash_ratio=1.0,
        )

        assert not np.array_equal(state1[-3:], state2[-3:])

    def test_feature_engineer_ticker_normalization(self, frame_with_indicators: pd.DataFrame) -> None:
        engineer = FeatureEngineer(feature_set="standard")

        frame_aapl = frame_with_indicators.copy()
        frame_goog = frame_with_indicators.copy()
        frame_goog["close"] = frame_goog["close"] * 0.5

        state1 = engineer.build_state(
            frame=frame_aapl,
            ticker="AAPL",
            portfolio_weight=0.0,
            unrealized_pnl_pct=0.0,
            cash_ratio=1.0,
        )

        state2 = engineer.build_state(
            frame=frame_goog,
            ticker="GOOG",
            portfolio_weight=0.0,
            unrealized_pnl_pct=0.0,
            cash_ratio=1.0,
        )

        assert len(state1) == len(state2)
        assert not np.array_equal(state1, state2)
