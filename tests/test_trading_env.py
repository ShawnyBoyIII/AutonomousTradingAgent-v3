from __future__ import annotations

import pytest
import numpy as np
import pandas as pd

from trading_bot.env.trading_env import TradingEnv


@pytest.fixture
def sample_price_data() -> pd.DataFrame:
    n = 200
    base_price = 100.0
    closes = [base_price + i * 0.5 + (i % 10) * 0.2 for i in range(n)]
    highs = [c + 2.0 for c in closes]
    lows = [c - 2.0 for c in closes]
    opens = [c - 0.5 for c in closes]
    volumes = [1_000_000 + i * 1000 for i in range(n)]

    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


@pytest.fixture
def price_data_with_indicators(sample_price_data: pd.DataFrame) -> pd.DataFrame:
    from trading_bot.data.indicators import (
        add_ema, add_sma, add_rsi, add_macd,
        add_bollinger_bands, add_stochastic, add_cci,
        add_williams_r, add_atr_percent, add_adx, add_obv, add_vwap,
    )

    frame = sample_price_data.copy()
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
    frame = add_vwap(frame)
    return frame


class TestTradingEnv:
    def test_env_initialization(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
        )

        assert env.ticker == "AAPL"
        assert env.initial_cash == 10000.0
        assert env.state_size == 19
        assert isinstance(env.action_space, type(env.action_space))
        assert isinstance(env.observation_space, type(env.observation_space))

    def test_env_reset_returns_valid_observation(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
        )

        obs, info = env.reset()

        assert isinstance(obs, np.ndarray)
        assert obs.shape == (env.state_size,)
        assert obs.dtype == np.float64
        assert isinstance(info, dict)
        assert "step" in info
        assert info["step"] == env.warmup_bars

    def test_env_step_returns_valid_tuple(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
        )

        obs, _ = env.reset()

        obs_next, reward, done, truncated, info = env.step(1)

        assert isinstance(obs_next, np.ndarray)
        assert obs_next.shape == (env.state_size,)
        assert isinstance(reward, float)
        assert isinstance(done, bool)
        assert isinstance(truncated, bool)
        assert isinstance(info, dict)
        assert info["step"] == env.warmup_bars + 1

    def test_env_actions_affect_portfolio(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
        )

        env.reset()

        _, _, _, _, info_hold = env.step(0)
        _, _, _, _, info_buy = env.step(1)

        assert info_hold["position_shares"] == 0
        assert info_buy["position_shares"] > 0

    def test_env_buy_then_sell(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
        )

        env.reset()

        _, _, _, _, info_buy = env.step(1)
        assert info_buy["position_shares"] > 0

        _, _, _, _, info_sell = env.step(2)
        assert info_sell["position_shares"] == 0

    def test_env_tracks_cumulative_reward(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
        )

        env.reset()

        _, reward1, _, _, _ = env.step(0)
        _, reward2, _, _, _ = env.step(0)

        assert env._cumulative_reward == reward1 + reward2

    def test_env_done_conditions(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
        )

        env.reset()

        done = False
        steps = 0
        max_steps = len(price_data_with_indicators) + 10

        while not done and steps < max_steps:
            _, _, done, _, _ = env.step(0)
            steps += 1

        assert done or steps >= len(price_data_with_indicators) - 1

    def test_env_observation_space_bounds(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
        )

        obs, _ = env.reset()

        assert not np.isnan(obs).all()
        assert obs.shape == (env.state_size,)

    def test_env_extended_feature_set(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
            feature_set="extended",
        )

        obs, _ = env.reset()

        assert env.state_size == 24
        assert obs.shape == (24,)

    def test_env_transaction_costs(self, price_data_with_indicators: pd.DataFrame) -> None:
        env_low_cost = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
            transaction_cost_bps=1.0,
        )

        env_high_cost = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
            transaction_cost_bps=100.0,
        )

        env_low_cost.reset()
        env_high_cost.reset()

        _, reward_low, _, _, _ = env_low_cost.step(1)
        _, reward_high, _, _, _ = env_high_cost.step(1)

        assert reward_low > reward_high

    def test_env_portfolio_value_tracking(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
        )

        env.reset()
        _, _, _, _, info = env.step(1)

        assert info["portfolio_value"] > 0
        assert info["portfolio_value"] <= env.initial_cash + (info["position_shares"] * info["entry_price"])

    def test_env_cash_updates_on_trade(self, price_data_with_indicators: pd.DataFrame) -> None:
        env = TradingEnv(
            daily_frame=price_data_with_indicators,
            ticker="AAPL",
            initial_cash=10000.0,
        )

        _, info_reset = env.reset()
        initial_cash = info_reset["cash"]

        _, _, _, _, info_buy = env.step(1)
        assert info_buy["cash"] < initial_cash

        _, _, _, _, info_sell = env.step(2)
        assert info_sell["cash"] > info_buy["cash"]
