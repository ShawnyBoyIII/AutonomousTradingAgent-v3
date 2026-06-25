from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    import pandas as pd
    from stable_baselines3 import PPO, A2C, SAC, TD3, DDPG

from trading_bot.data.feature_engineering import FeatureEngineer
from trading_bot.env.trading_env import TradingEnv


class RLAgent:
    """Wrapper for stable-baselines3 DRL agents.

    Provides a unified interface for training and inference across different
    agent types (PPO, A2C, SAC, TD3, DDPG).

    Usage::

        agent = RLAgent(agent_type="PPO", feature_set="standard")
        agent.train(daily_frame, ticker="AAPL", episodes=100)
        agent.save("trained_models/rl_agent.zip")

        loaded = RLAgent.load("trained_models/rl_agent.zip")
        action, confidence = loaded.predict(daily_frame, ticker="AAPL")
    """

    SUPPORTED_AGENTS = {"PPO", "A2C", "SAC", "TD3", "DDPG"}
    AGENT_CLASSES: dict[str, type] = {}

    def __init__(
        self,
        agent_type: str = "PPO",
        feature_set: str = "standard",
        learning_rate: float = 3e-4,
        verbose: int = 0,
    ) -> None:
        if agent_type not in self.SUPPORTED_AGENTS:
            raise ValueError(f"Unsupported agent type: {agent_type}. Choose from {self.SUPPORTED_AGENTS}")

        self.agent_type = agent_type
        self.feature_set = feature_set
        self.learning_rate = learning_rate
        self.verbose = verbose

        self.feature_engineer = FeatureEngineer(feature_set=feature_set)
        self._model: Any | None = None

    def _get_agent_class(self) -> type:
        """Lazy import of agent class to avoid dependency when not using RL."""
        if not self.AGENT_CLASSES:
            try:
                from stable_baselines3 import PPO, A2C, SAC, TD3, DDPG

                self.AGENT_CLASSES = {
                    "PPO": PPO,
                    "A2C": A2C,
                    "SAC": SAC,
                    "TD3": TD3,
                    "DDPG": DDPG,
                }
            except ImportError as e:
                raise ImportError(
                    "stable-baselines3 not installed. Install with: pip install -e '.[rl]'"
                ) from e
        return self.AGENT_CLASSES[self.agent_type]

    def train(
        self,
        daily_frame: "pd.DataFrame",
        ticker: str = "AAPL",
        episodes: int = 100,
        timesteps: int = 100000,
        initial_cash: float = 10000.0,
        transaction_cost_bps: float = 10.0,
    ) -> dict[str, float]:
        """Train the DRL agent on historical data.

        Args:
            daily_frame: OHLCV DataFrame with indicator columns.
            ticker: Ticker symbol for identification.
            episodes: Number of training episodes.
            timesteps: Total timesteps to train.
            initial_cash: Starting cash for the environment.
            transaction_cost_bps: Transaction cost in basis points.

        Returns:
            Dictionary with training metrics (final reward, episodes completed).
        """
        agent_class = self._get_agent_class()

        env = TradingEnv(
            daily_frame=daily_frame,
            ticker=ticker,
            initial_cash=initial_cash,
            feature_set=self.feature_set,
            transaction_cost_bps=transaction_cost_bps,
        )

        self._model = agent_class(
            "MlpPolicy",
            env,
            learning_rate=self.learning_rate,
            verbose=self.verbose,
        )

        self._model.learn(total_timesteps=timesteps)

        return {
            "agent_type": self.agent_type,
            "timesteps": timesteps,
            "feature_set": self.feature_set,
        }

    def predict(
        self,
        daily_frame: "pd.DataFrame",
        ticker: str = "",
        portfolio_weight: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
        cash_ratio: float = 1.0,
    ) -> tuple[int, float]:
        """Predict action for current market state.

        Args:
            daily_frame: OHLCV DataFrame with indicator columns.
            ticker: Ticker symbol for normalization.
            portfolio_weight: Current position weight (0-1).
            unrealized_pnl_pct: Current unrealized PnL as percentage.
            cash_ratio: Fraction of portfolio in cash (0-1).

        Returns:
            Tuple of (action, confidence) where action is 0=HOLD, 1=BUY, 2=SELL.
        """
        if self._model is None:
            raise RuntimeError("Model not trained or loaded. Call train() or load() first.")

        state = self.feature_engineer.build_state(
            frame=daily_frame,
            ticker=ticker,
            portfolio_weight=portfolio_weight,
            unrealized_pnl_pct=unrealized_pnl_pct,
            cash_ratio=cash_ratio,
        )

        action, confidence = self._model.predict(state, deterministic=True)
        return int(action), float(confidence)

    def save(self, path: str | Path) -> None:
        """Save trained model to disk."""
        if self._model is None:
            raise RuntimeError("No model to save. Train or load first.")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._model.save(str(path))

    @classmethod
    def load(
        cls,
        path: str | Path,
        feature_set: str = "standard",
        verbose: int = 0,
    ) -> "RLAgent":
        """Load trained model from disk.

        Args:
            path: Path to saved model (.zip file).
            feature_set: Feature set used during training.
            verbose: Verbosity level.

        Returns:
            RLAgent instance with loaded model.
        """
        from stable_baselines3.common.base_class import BaseAlgorithm

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Model file not found: {path}")

        agent = cls(
            agent_type="PPO",
            feature_set=feature_set,
            verbose=verbose,
        )

        agent_class = agent._get_agent_class()
        agent._model = agent_class.load(str(path))

        return agent

    def evaluate(
        self,
        daily_frame: "pd.DataFrame",
        ticker: str = "AAPL",
        episodes: int = 10,
        initial_cash: float = 10000.0,
        transaction_cost_bps: float = 10.0,
    ) -> dict[str, float]:
        """Evaluate trained agent on historical data.

        Args:
            daily_frame: OHLCV DataFrame with indicator columns.
            ticker: Ticker symbol.
            episodes: Number of evaluation episodes.
            initial_cash: Starting cash.
            transaction_cost_bps: Transaction cost in basis points.

        Returns:
            Dictionary with evaluation metrics (avg reward, win rate, final equity).
        """
        if self._model is None:
            raise RuntimeError("Model not trained or loaded.")

        env = TradingEnv(
            daily_frame=daily_frame,
            ticker=ticker,
            initial_cash=initial_cash,
            feature_set=self.feature_set,
            transaction_cost_bps=transaction_cost_bps,
            render_mode=None,
        )

        rewards: list[float] = []
        final_equities: list[float] = []
        wins = 0

        for _ in range(episodes):
            obs, _ = env.reset()
            episode_reward = 0.0
            done = False

            while not done:
                action, _ = self._model.predict(obs, deterministic=True)
                obs, reward, done, truncated, info = env.step(int(action))
                episode_reward += reward

                if done:
                    final_equities.append(info.get("portfolio_value", initial_cash))
                    if info.get("portfolio_value", initial_cash) > initial_cash:
                        wins += 1

            rewards.append(episode_reward)

        return {
            "avg_reward": float(np.mean(rewards)),
            "std_reward": float(np.std(rewards)),
            "win_rate": wins / episodes if episodes > 0 else 0.0,
            "avg_final_equity": float(np.mean(final_equities)),
            "initial_cash": initial_cash,
        }
