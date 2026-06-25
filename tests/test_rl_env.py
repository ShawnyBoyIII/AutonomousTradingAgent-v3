"""Tests for the gymnasium-compatible RL trading environment."""

from __future__ import annotations

import numpy as np
import pytest
import gymnasium as gym

from trading_bot.rl.env import TradingConfig, TradingEnv
from trading_bot.rl.actions import BSHActionScheme, ProportionActionScheme
from trading_bot.rl.rewards import (
    SimpleProfitReward,
    RiskAdjustedReward,
    CompoundDailyReward,
    ShannonEntropyReward,
)
from trading_bot.rl.observer import TensorTradeObserver
from trading_bot.models.portfolio import PortfolioState
from trading_bot.rl.trainer import RLTrainer, TrainingConfig


@pytest.fixture
def mock_market_data(monkeypatch):
    """Mock fetch_bars to return synthetic OHLCV data."""
    def make_df(n=100, base_price=100.0):
        import pandas as pd
        dates = pd.date_range("2025-01-01", periods=n, freq="1d")
        prices = [base_price * (1 + np.random.randn() * 0.02) for _ in range(n)]
        prices = [max(p, 1.0) for p in prices]
        return pd.DataFrame({
            "timestamp": dates,
            "open": prices,
            "high": [p * 1.02 for p in prices],
            "low": [p * 0.98 for p in prices],
            "close": prices,
            "volume": [int(1e6 * abs(np.random.randn())) for _ in range(n)],
        })

    def mock_fetch(symbol, period, interval, start=None, end=None):
        base = {"AAPL": 150.0, "MSFT": 380.0, "GOOGL": 140.0, "AMZN": 175.0, "NVDA": 800.0}
        return make_df(n=100, base_price=base.get(symbol, 100.0))

    monkeypatch.setattr(
        "trading_bot.data.market_data.fetch_bars",
        mock_fetch,
    )
    return mock_fetch


@pytest.fixture
def config():
    return TradingConfig(
        starting_cash=100_000.0,
        symbols=["AAPL", "MSFT", "GOOGL"],
        observer_window=5,
        max_episode_steps=50,
        action_scheme="bsh",
        reward_scheme="simple_profit",
    )


@pytest.fixture
def env(config, mock_market_data):
    return TradingEnv(config=config)


class TestTradingEnv:
    def test_env_creation(self, env):
        assert env.action_space is not None
        assert env.observation_space is not None
        assert isinstance(env.action_space, gym.spaces.Discrete)
        assert isinstance(env.observation_space, gym.spaces.Box)

    def test_reset(self, env):
        obs, info = env.reset()
        assert obs is not None
        assert isinstance(obs, np.ndarray)
        assert obs.shape == (5, 44)  # (window_size, n_features)
        assert "net_worth" in info
        assert "step" in info
        assert info["step"] == 0

    def test_step(self, env, mock_market_data):
        env.reset()
        obs, reward, terminated, truncated, info = env.step(0)
        assert obs is not None
        assert isinstance(obs, np.ndarray)
        assert isinstance(reward, float)
        assert isinstance(terminated, bool)
        assert isinstance(truncated, bool)
        assert "net_worth" in info
        assert "reward" in info

    def test_step_returns(self, env, mock_market_data):
        env.reset()
        for _ in range(10):
            action = env.action_space.sample()
            obs, reward, terminated, truncated, info = env.step(action)
            assert obs.shape == (5, 44)
            assert isinstance(reward, float)

    def test_portfolio_tracking(self, env, mock_market_data):
        env.reset()
        initial_equity = env.get_portfolio_state().equity
        assert initial_equity == 100_000.0

        for _ in range(20):
            action = env.action_space.sample()
            env.step(action)

        state = env.get_portfolio_state()
        assert state is not None
        assert state.equity > 0
        assert isinstance(state.cash, float)

    def test_max_episode_steps(self, env, mock_market_data):
        env.reset()
        steps = 0
        while steps < env.config.max_episode_steps:
            action = env.action_space.sample()
            _, _, terminated, truncated, _ = env.step(action)
            steps += 1
            if truncated:
                break
        assert steps <= env.config.max_episode_steps

    def test_broker_access(self, env, mock_market_data):
        env.reset()
        broker = env.get_broker()
        assert broker is not None
        assert broker.cash > 0

    def test_render(self, env, mock_market_data, capsys):
        env.reset()
        env.step(0)
        env.render()
        captured = capsys.readouterr()
        assert "Step" in captured.out
        assert "Equity" in captured.out

    def test_episode_summary_in_terminal_info(self, mock_market_data):
        env = TradingEnv(
            config=TradingConfig(
                symbols=["AAPL"],
                observer_window=5,
                max_episode_steps=2,
                reward_scheme="compound_daily",
            )
        )
        env.reset()
        _, _, _, truncated, info = env.step(2)  # BUY AAPL
        assert not truncated
        _, _, _, truncated, info = env.step(3)  # SELL AAPL
        assert truncated
        summary = info["episode_summary"]
        assert summary["trade_count"] == 2
        assert summary["buy_count"] == 1
        assert summary["sell_count"] == 1
        assert summary["steps"] == 2


class TestActionSchemes:
    def test_bsh_action_space(self, mock_market_data):
        scheme = BSHActionScheme(symbols=["AAPL", "MSFT"])
        assert isinstance(scheme.action_space, gym.spaces.Discrete)
        expected = 2 * 3 + 1  # n_symbols * 3 directions + 1 no-op
        assert scheme.action_space.n == expected

    def test_bsh_hold_action(self, mock_market_data):
        scheme = BSHActionScheme(symbols=["AAPL"], max_shares=100)
        scheme.reset_portfolio(None)
        scheme.perform(0, {"AAPL": 150.0})  # no-op

    def test_proportion_action_space(self, mock_market_data):
        scheme = ProportionActionScheme(symbols=["AAPL", "MSFT"])
        assert isinstance(scheme.action_space, gym.spaces.Discrete)
        expected = 2 * 2 * 10 + 1  # n_symbols * 2 directions * 10 proportions + 1
        assert scheme.action_space.n == expected

    def test_proportion_zero_action(self, mock_market_data):
        scheme = ProportionActionScheme(symbols=["AAPL"])
        scheme.reset_portfolio(None)
        scheme.perform(0, {"AAPL": 150.0})  # no-op


class TestRewardSchemes:
    def test_simple_profit_positive(self):
        reward = SimpleProfitReward()
        r = reward.compute_reward(110_000, 100_000)
        assert r == 10_000.0

    def test_simple_profit_negative(self):
        reward = SimpleProfitReward()
        r = reward.compute_reward(90_000, 100_000)
        assert r == -10_000.0

    def test_risk_adjusted_positive(self):
        reward = RiskAdjustedReward()
        r = reward.compute_reward(110_000, 100_000)
        assert abs(r - 0.1) < 0.001

    def test_risk_adjusted_zero_previous(self):
        reward = RiskAdjustedReward()
        r = reward.compute_reward(100, 0)
        assert r == 0.0

    def test_compound_daily_positive(self):
        reward = CompoundDailyReward()
        r = reward.compute_reward(110_000, 100_000)
        import math
        expected = math.log(110_000 / 100_000)
        assert abs(r - expected) < 0.001

    def test_shannon_entropy(self):
        reward = ShannonEntropyReward()
        r = reward.compute_reward(110_000, 100_000)
        import math
        expected = math.log(110_000 / 100_000)
        assert abs(r - expected) < 0.001

    def test_env_accepts_shannon_reward_scheme(self, mock_market_data):
        env = TradingEnv(
            config=TradingConfig(
                symbols=["AAPL"],
                observer_window=5,
                reward_scheme="shannon_entropy",
            )
        )
        env.reset()
        _, reward, _, _, _ = env.step(0)
        assert isinstance(reward, float)


class TestRLTrainer:
    def test_evaluate_reports_per_episode_final_equity(self, mock_market_data):
        trainer = RLTrainer(
            TrainingConfig(
                env_config=TradingConfig(
                    symbols=["AAPL"],
                    observer_window=5,
                    max_episode_steps=2,
                )
            )
        )

        class FakeModel:
            def predict(self, obs, deterministic=True):
                return 0, None

        trainer._model = FakeModel()
        result = trainer.evaluate(n_episodes=2)
        assert "mean_final_equity" in result
        assert "mean_trade_count" in result
        assert result["min_final_equity"] <= result["max_final_equity"]


class TestObserver:
    def test_observation_shape(self, config, mock_market_data):
        observer = TensorTradeObserver(
            symbols=config.symbols,
            window_size=config.observer_window,
        )
        state = PortfolioState(cash=100_000, equity=100_000)
        prices = {"AAPL": 150.0, "MSFT": 380.0, "GOOGL": 140.0}

        obs = observer.observe(state, prices, 0)
        assert isinstance(obs, np.ndarray)
        assert obs.shape == (config.observer_window, 44)

    def test_observation_space(self, config, mock_market_data):
        observer = TensorTradeObserver(
            symbols=config.symbols,
            window_size=config.observer_window,
        )
        assert isinstance(observer.observation_space, gym.spaces.Box)
        assert observer.observation_space.shape == (
            config.observer_window,
            44,
        )

    def test_observation_padded_initial(self, config, mock_market_data):
        observer = TensorTradeObserver(
            symbols=config.symbols,
            window_size=10,
        )
        state = PortfolioState(cash=100_000, equity=100_000)
        prices = {"AAPL": 150.0, "MSFT": 380.0, "GOOGL": 140.0}

        obs = observer.observe(state, prices, 0)
        assert obs.shape == (10, 44)

    def test_portfolio_features(self, config, mock_market_data):
        observer = TensorTradeObserver(
            symbols=config.symbols,
            window_size=5,
        )
        state = PortfolioState(
            cash=50_000,
            equity=80_000,
            unrealized_pnl=5_000,
            realized_pnl=2_000,
        )
        prices = {"AAPL": 150.0, "MSFT": 380.0, "GOOGL": 140.0}

        obs = observer.observe(state, prices, 0)
        assert obs is not None
        assert not np.all(obs == 0)
