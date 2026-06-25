from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from trading_bot.rl.env import TradingConfig, TradingEnv
from trading_bot.rl.trainer import RLTrainer, TrainingConfig as RLTrainingConfig

logger = logging.getLogger(__name__)


@dataclass
class RLAgentConfig:
    enabled: bool = False
    model_path: str | None = None
    env_config: TradingConfig = field(default_factory=TradingConfig)
    training: RLTrainingConfig = field(default_factory=RLTrainingConfig)
    prediction_mode: str = "deterministic"


class RLAgent:
    """RL agent wrapper for trading signals.

    Can be used in two modes:
    1. Training mode: trains a new agent on TradingEnv
    2. Inference mode: loads a trained model and generates predictions

    Integrates with the existing strategy layer as an optional signal source.
    """

    def __init__(self, config: RLAgentConfig | None = None) -> None:
        self.config = config or RLAgentConfig()
        self._trainer: RLTrainer | None = None
        self._model = None
        self._env: TradingEnv | None = None

    def train(self) -> RLTrainer:
        if not self.config.enabled:
            raise RuntimeError("RL agent is disabled. Set config.enabled=True")

        self._trainer = RLTrainer(
            training_config=self.config.training
        )
        self._model = self._trainer.train()
        return self._trainer

    def load(self, model_path: str | Path | None = None) -> None:
        path = model_path or self.config.model_path
        if path is None:
            raise ValueError("No model path specified. Provide model_path or set config.model_path")

        if self._trainer is None:
            self._trainer = RLTrainer()

        self._model = self._trainer.load(path)
        logger.info(f"RL agent loaded from {path}")

    def predict(
        self,
        observation: np.ndarray | None = None,
        deterministic: bool | None = None,
    ) -> tuple[int, dict[str, float] | None]:
        if self._model is None:
            raise RuntimeError(
                "No model loaded. Call train() or load() first."
            )

        if observation is None:
            if self._env is None:
                self._env = TradingEnv(config=self.config.env_config)
            observation, _ = self._env.reset()

        if deterministic is None:
            deterministic = self.config.prediction_mode == "deterministic"

        action, info = self._model.predict(observation, deterministic=deterministic)
        return int(action), info

    def evaluate(self, n_episodes: int = 10) -> dict[str, float]:
        if self._trainer is None:
            raise RuntimeError("No trainer available. Call train() first.")

        return self._trainer.evaluate(n_episodes=n_episodes)

    def save(self, path: str | Path) -> None:
        if self._model is None:
            raise RuntimeError("No model to save. Train or load a model first.")
        self._model.save(path)
        logger.info(f"Model saved to {path}")
