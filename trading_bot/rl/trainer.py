from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from trading_bot.rl.env import TradingConfig, TradingEnv

logger = logging.getLogger(__name__)


@dataclass
class TrainingConfig:
    env_config: TradingConfig = field(default_factory=TradingConfig)
    model_type: str = "PPO"
    total_timesteps: int = 50000
    learning_rate: float = 3e-4
    n_steps: int = 128
    batch_size: int = 64
    n_epochs: int = 10
    gamma: float = 0.995
    gae_lambda: float = 0.95
    clip_range: float = 0.2
    ent_coef: float = 0.01
    vf_coef: float = 0.5
    max_grad_norm: float = 0.5
    verbose: int = 1
    log_dir: str = "state/rl_logs"
    eval_envs: int = 3
    eval_freq: int = 5000
    checkpoint_freq: int = 10000


class RLTrainer:
    """Trains RL agents on TradingEnv using stable-baselines3.

    Supports PPO, A2C, DQN, SAC, TD3, and DDPG agents.
    """

    def __init__(self, training_config: TrainingConfig | None = None) -> None:
        self.config = training_config or TrainingConfig()
        self._env: TradingEnv | None = None
        self._model = None
        self._best_mean_reward = -np.inf
        self._checkpoint_dir = Path(self.config.log_dir)
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def _make_env(self, seed: int = 0) -> TradingEnv:
        self._env = TradingEnv(config=self.config.env_config)
        return self._env

    def train(self) -> Any:
        try:
            from stable_baselines3 import A2C, DDPG, DQN, PPO, SAC, TD3
        except ImportError:
            raise ImportError(
                "stable-baselines3 is required for RL training. "
                "Install with: pip install stable-baselines3"
            )

        model_class = {
            "PPO": PPO,
            "A2C": A2C,
            "DQN": DQN,
            "SAC": SAC,
            "TD3": TD3,
            "DDPG": DDPG,
        }.get(self.config.model_type)

        if model_class is None:
            raise ValueError(
                f"Unknown model type: {self.config.model_type}. "
                f"Supported: {list(model_class.keys())}"
            )

        env = self._make_env()

        kwargs: dict[str, Any] = {
            "learning_rate": self.config.learning_rate,
            "n_steps": self.config.n_steps,
            "batch_size": self.config.batch_size,
            "n_epochs": self.config.n_epochs,
            "gamma": self.config.gamma,
            "gae_lambda": self.config.gae_lambda,
            "clip_range": self.config.clip_range,
            "ent_coef": self.config.ent_coef,
            "vf_coef": self.config.vf_coef,
            "max_grad_norm": self.config.max_grad_norm,
            "verbose": self.config.verbose,
        }

        if self.config.model_type == "PPO":
            kwargs["n_steps"] = 128
            model = model_class("MultiInputPolicy", env, **kwargs)
        elif self.config.model_type == "A2C":
            model = model_class("MultiInputPolicy", env, **kwargs)
        elif self.config.model_type == "DQN":
            model = model_class("MultiInputPolicy", env, **kwargs)
        elif self.config.model_type == "SAC":
            kwargs["learning_starts"] = 1000
            kwargs["reward_scale"] = 1.0
            model = model_class("MultiInputPolicy", env, **kwargs)
        elif self.config.model_type == "TD3":
            kwargs["learning_starts"] = 1000
            kwargs["reward_scale"] = 1.0
            model = model_class("MultiInputPolicy", env, **kwargs)
        elif self.config.model_type == "DDPG":
            kwargs["learning_starts"] = 1000
            kwargs["reward_scale"] = 1.0
            model = model_class("MultiInputPolicy", env, **kwargs)

        logger.info(
            f"Training {self.config.model_type} on TradingEnv "
            f"({self.config.total_timesteps} timesteps)"
        )

        model.learn(total_timesteps=self.config.total_timesteps)

        checkpoint_path = self._checkpoint_dir / f"{self.config.model_type}_final"
        model.save(checkpoint_path)
        logger.info(f"Model saved to {checkpoint_path}")

        return model

    def evaluate(self, n_episodes: int = 10) -> dict[str, float]:
        if self._model is None:
            raise RuntimeError("No model loaded. Call train() or load() first.")

        env = self._make_env()
        rewards = []

        for _ in range(n_episodes):
            obs, _ = env.reset()
            done = False
            truncated = False
            total_reward = 0.0

            while not (done or truncated):
                action, _ = self._model.predict(obs, deterministic=True)
                obs, reward, done, truncated, _ = env.step(action)
                total_reward += reward

            rewards.append(total_reward)

        mean_reward = np.mean(rewards)
        std_reward = np.std(rewards)
        final_equities = [
            env.get_portfolio_state().equity if env.get_portfolio_state() else 0.0
            for _ in range(n_episodes)
        ]

        result = {
            "mean_reward": float(mean_reward),
            "std_reward": float(std_reward),
            "mean_final_equity": float(np.mean(final_equities)),
            "min_final_equity": float(np.min(final_equities)),
            "max_final_equity": float(np.max(final_equities)),
        }

        logger.info(
            f"Evaluation ({n_episodes} episodes): "
            f"mean_reward={mean_reward:.4f}, "
            f"final_equity=${np.mean(final_equities):,.2f}"
        )

        return result

    def predict(
        self,
        obs: np.ndarray,
        deterministic: bool = True,
    ) -> tuple[int, dict[str, float] | None]:
        if self._model is None:
            raise RuntimeError("No model loaded. Call train() or load() first.")
        action, _ = self._model.predict(obs, deterministic=deterministic)
        return int(action), None

    def load(self, path: str | Path) -> Any:
        try:
            from stable_baselines3 import PPO
        except ImportError:
            raise ImportError("stable-baselines3 required for model loading")

        self._model = PPO.load(path)
        logger.info(f"Model loaded from {path}")
        return self._model
