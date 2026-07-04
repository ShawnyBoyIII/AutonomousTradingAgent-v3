from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import torch

from trading_bot.rl.env import TradingConfig, TradingEnv
from trading_bot.rl.trainer import RLTrainer, TrainingConfig as RLTrainingConfig
from trading_bot.rl.features import (
    build_cross_symbol_features,
    build_market_feature_row,
    build_observation,
)

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

    @classmethod
    def load(cls, model_path: str | Path | None = None, feature_set: str = "standard", verbose: int = 1) -> "RLAgent":
        from trading_bot.rl.trainer import TrainingConfig as RLTrainingConfig

        # Try to load training metadata (symbols, etc.) saved alongside the model
        import json
        path_obj = Path(model_path) if model_path else None
        meta_symbols = ["AAPL"]
        meta_max_symbols = None
        meta_action_scheme = None
        if path_obj is not None:
            meta_path = path_obj.with_suffix(".zip")
            if meta_path.suffix != ".zip":
                meta_path = path_obj
            meta_path = meta_path.parent / (meta_path.stem + "_meta.json")
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    meta_symbols = [s.strip().upper() for s in meta.get("symbols", ["AAPL"])]
                    meta_max_symbols = meta.get("max_symbols")
                    meta_action_scheme = meta.get("action_scheme")
                except Exception as e:
                    logger.debug("RL agent error: %s", e)

        config = RLAgentConfig(
            enabled=True,
            model_path=str(model_path) if model_path else None,
            env_config=TradingConfig(
                symbols=list(meta_symbols),
                bar_period="1y",
                bar_interval="1d",
                observer_window=10,
                starting_cash=100_000.0,
                fee_per_order=1.0,
                slippage_bps=5,
                max_positions=10,
                max_episode_steps=500,
                max_symbols=meta_max_symbols,
                action_scheme=meta_action_scheme or "bsh",
            ),
            training=RLTrainingConfig(
                env_config=TradingConfig(
                    symbols=list(meta_symbols),
                    bar_period="1y",
                    bar_interval="1d",
                    observer_window=10,
                    starting_cash=100_000.0,
                    fee_per_order=1.0,
                    slippage_bps=5,
                    max_positions=10,
                    max_episode_steps=500,
                    max_symbols=meta_max_symbols,
                    action_scheme=meta_action_scheme or "bsh",
                ),
                model_type="PPO",
                total_timesteps=50000,
                verbose=verbose,
            ),
            prediction_mode="deterministic",
        )

        agent = cls(config=config)
        path = model_path or agent.config.model_path
        if path is None:
            raise ValueError("No model path specified. Provide model_path or set config.model_path")

        if agent._trainer is None:
            agent._trainer = RLTrainer(
                training_config=agent.config.training
            )

        agent._model = agent._trainer.load(path)
        logger.info(f"RL agent loaded from {path}")
        return agent

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

    def predict_signal(
        self,
        daily_frame: Any,
        ticker: str,
        portfolio_weight: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
        cash_ratio: float = 1.0,
        symbols: list[str] | None = None,
        market_frames: dict[str, Any] | None = None,
    ) -> tuple[int, float]:
        """Predict trading signal from market data.
        
        Builds observation from market data and returns (action, confidence).
        Returned action is normalized to 0=HOLD, 1=BUY, 2=SELL for the ticker.
        Confidence: float between 0 and 1
        """
        if self._model is None:
            raise RuntimeError("No model loaded. Call train() or load() first.")

        # Determine number of symbols and features from observation space
        obs_shape = self._model.observation_space.shape
        n_portfolio_features = 5
        n_symbols = len(self.config.env_config.symbols) if self.config.env_config.symbols else 1
        features_per_symbol = (obs_shape[1] - n_portfolio_features) // n_symbols

        symbol_list = [s.upper().strip() for s in (symbols or self.config.env_config.symbols or [ticker])]
        
        # Build market rows + cross features for each symbol
        if market_frames is None:
            market_frames = {symbol_list[0]: daily_frame.copy()} if symbol_list else {}
        
        # Build cross-symbol feature frames (all symbols' data for correlation calculation)
        cross_symbol_frames: dict[str, Any] = {}
        for sym in symbol_list:
            frame = market_frames.get(sym)
            if frame is not None:
                cross_symbol_frames[sym] = frame
        
        market_rows = []
        for i, frame_symbol in enumerate(symbol_list):
            frame = market_frames.get(frame_symbol)
            if frame is not None and not frame.empty:
                market_feat = build_market_feature_row(frame.copy())
            else:
                market_feat = [0.0] * 13  # fallback for missing frames
            
            # Add cross-symbol features
            if cross_symbol_frames and frame is not None:
                cross_feat = build_cross_symbol_features(frame_symbol, cross_symbol_frames, len(frame) - 1)
                row = market_feat + cross_feat
            else:
                # Pad with zeros to match expected features_per_symbol
                row = market_feat + [0.0] * (features_per_symbol - len(market_feat))
            
            market_rows.append(row)
        
        # Pad or truncate to match number of symbols in observation space
        while len(market_rows) < n_symbols:
            market_rows.append([0.0] * features_per_symbol)
        market_rows = market_rows[:n_symbols]

        position_weight_sum = portfolio_weight
        realized_pnl_pct = unrealized_pnl_pct

        portfolio_features = [
            cash_ratio, 1 if portfolio_weight > 0 else 0, position_weight_sum,
            unrealized_pnl_pct, realized_pnl_pct,
        ]
        
        data_indices = {}
        for sym in symbol_list:
            frame = market_frames.get(sym)
            if frame is not None and not frame.empty:
                data_indices[sym] = len(frame) - 1
        
        symbols_for_observation = symbol_list[:n_symbols]
        
        observation = build_observation(
            market_rows,
            portfolio_features,
            observer_window=obs_shape[0],
            data_frames=market_frames if market_frames else None,
            data_indices=data_indices if data_indices else None,
            symbol_frames=cross_symbol_frames if cross_symbol_frames else None,
            symbols=symbols_for_observation,
        )

        raw_action, _ = self._model.predict(observation, deterministic=True)
        raw_action = int(raw_action)

        # Determine action scheme from meta or action space size
        action_scheme = self.config.env_config.action_scheme or "bsh"
        ticker_upper = ticker.upper().strip()
        trained_symbol_list = symbols_for_observation
        try:
            symbol_idx = trained_symbol_list.index(ticker_upper)
        except ValueError:
            return 0, 0.0

        normalized_action = 0  # HOLD
        confidence = 0.5

        if action_scheme == "proportion":
            # ProportionActionScheme: Discrete(n_symbols * 2 * 10 + 1)
            # Action 0 = HOLD
            # Action 1-10 = BUY symbol 0 at 10%-100%
            # Action 11-20 = SELL symbol 0 at 10%-100%
            # Action 21-30 = BUY symbol 1 at 10%-100%
            # etc.
            if raw_action >= 1:
                action_minus_1 = raw_action - 1
                actual_symbol_idx = action_minus_1 // 20
                direction = (action_minus_1 // 10) % 2
                if actual_symbol_idx == symbol_idx:
                    if direction == 0:
                        normalized_action = 1  # BUY
                    elif direction == 1:
                        normalized_action = 2  # SELL
                    # Confidence based on proportion (higher proportion = more confident)
                    prop_idx = action_minus_1 % 10
                    confidence = 0.5 + (prop_idx / 20.0)  # 0.55 to 1.0
        else:
            # BSHActionScheme: Discrete(n_symbols * 3 + 1)
            # Action 0 = global HOLD
            # symbol_idx*3 + 1 = HOLD for symbol
            # symbol_idx*3 + 2 = BUY for symbol
            # symbol_idx*3 + 3 = SELL for symbol
            if raw_action == symbol_idx * 3 + 2:
                normalized_action = 1  # BUY
            elif raw_action == symbol_idx * 3 + 3:
                normalized_action = 2  # SELL

        # Estimate confidence from action distribution
        try:
            obs_tensor = torch.as_tensor(observation.reshape(1, *observation.shape), dtype=torch.float32)
            dist = self._model.policy.get_distribution(obs_tensor)
            action_probs = dist.distribution.probs.detach().cpu().numpy() if hasattr(dist.distribution, 'probs') else None
            if action_probs is not None and len(action_probs) > 0:
                probs = np.asarray(action_probs).flatten()
                if action_scheme == "proportion":
                    # Confidence = probability assigned to any non-HOLD action for this symbol
                    start = symbol_idx * 20 + 1
                    end = start + 20
                    confidence = float(np.sum(probs[start:end]))
                else:
                    # Confidence = probability assigned to relevant actions for this symbol
                    relevant_actions = [symbol_idx * 3 + 1, symbol_idx * 3 + 2, symbol_idx * 3 + 3]
                    confidence = float(np.sum(probs[a] for a in relevant_actions if 0 <= a < len(probs)))
                confidence = min(confidence, 1.0)
            elif confidence < 0.5:
                confidence = 0.5
        except Exception as e:
            logger.debug("RL agent error: %s", e)

        return normalized_action, confidence

    def evaluate(self, n_episodes: int = 10) -> dict[str, float]:
        if self._model is None:
            raise RuntimeError("No model available. Train or load a model first.")

        # If trainer exists (training mode), use it
        if self._trainer is not None:
            return self._trainer.evaluate(n_episodes=n_episodes)

        # Otherwise create a temporary trainer for evaluation
        from trading_bot.rl.trainer import RLTrainer

        trainer = RLTrainer(self.config.training)
        trainer._model = self._model
        trainer._env = self._env
        return trainer.evaluate(n_episodes=n_episodes)

    def save(self, path: str | Path) -> None:
        if self._model is None:
            raise RuntimeError("No model to save. Train or load a model first.")
        self._model.save(path)
        logger.info(f"Model saved to {path}")
