from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import torch

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

    @classmethod
    def load(cls, model_path: str | Path | None = None, feature_set: str = "standard", verbose: int = 1) -> "RLAgent":
        from trading_bot.rl.trainer import TrainingConfig as RLTrainingConfig

        # Try to load training metadata (symbols, etc.) saved alongside the model
        import json
        path_obj = Path(model_path) if model_path else None
        meta_symbols = ["AAPL"]
        meta_max_symbols = None
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
                except Exception:
                    pass

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
    ) -> tuple[int, float]:
        """Predict trading signal from market data.
        
        Builds observation from market data and returns (action, confidence).
        Returned action is normalized to 0=HOLD, 1=BUY, 2=SELL for the ticker.
        Confidence: float between 0 and 1
        """
        if self._model is None:
            raise RuntimeError("No model loaded. Call train() or load() first.")

        from trading_bot.data.indicators import add_ema, add_rsi, add_sma, add_macd, add_bollinger_bands, add_atr_percent
        import pandas as pd
        
        # Determine number of symbols from observation space
        obs_shape = self._model.observation_space.shape
        n_market_features = 13  # len(FEATURE_COLS)
        n_portfolio_features = 5
        n_symbols = (obs_shape[1] - n_portfolio_features) // n_market_features
        
        def _build_feature_row(df: Any) -> list[float]:
            if isinstance(df, pd.DataFrame):
                df = df.copy()
            df = add_ema(df, 12, "ema_12")
            df = add_ema(df, 26, "ema_26")
            df = add_rsi(df, 14)
            df = add_sma(df, 20, "sma_20")
            df = add_macd(df, 12, 26, 9)
            df = add_bollinger_bands(df, 20, 2.0)
            df = add_atr_percent(df, 14)

            close_col = float(df["close"].iloc[-1]) if "close" in df.columns else 0.0
            returns = 0.0
            if len(df) >= 2:
                prev_close = float(df["close"].iloc[-2])
                if prev_close > 0:
                    returns = (close_col - prev_close) / prev_close

            rsi = float(df["rsi_14"].iloc[-1]) if "rsi_14" in df.columns and pd.notna(df["rsi_14"].iloc[-1]) else 50.0
            ema_12 = float(df["ema_12"].iloc[-1]) if "ema_12" in df.columns and pd.notna(df["ema_12"].iloc[-1]) else close_col
            ema_26 = float(df["ema_26"].iloc[-1]) if "ema_26" in df.columns and pd.notna(df["ema_26"].iloc[-1]) else close_col
            sma_20 = float(df["sma_20"].iloc[-1]) if "sma_20" in df.columns and pd.notna(df["sma_20"].iloc[-1]) else close_col

            macd_line = float(df["macd_line"].iloc[-1]) if "macd_line" in df.columns and pd.notna(df["macd_line"].iloc[-1]) else 0.0
            macd_signal = float(df["macd_signal"].iloc[-1]) if "macd_signal" in df.columns and pd.notna(df["macd_signal"].iloc[-1]) else 0.0
            macd_hist = float(df["macd_histogram"].iloc[-1]) if "macd_histogram" in df.columns and pd.notna(df["macd_histogram"].iloc[-1]) else 0.0

            bb_pct = float(df["bb_percent_b"].iloc[-1]) if "bb_percent_b" in df.columns and pd.notna(df["bb_percent_b"].iloc[-1]) else 50.0
            bb_w = float(df["bb_width"].iloc[-1]) if "bb_width" in df.columns and pd.notna(df["bb_width"].iloc[-1]) else 0.0
            atr_pct = float(df["atr_pct"].iloc[-1]) if "atr_pct" in df.columns and pd.notna(df["atr_pct"].iloc[-1]) else 0.0

            volume = float(df["volume"].iloc[-1]) if "volume" in df.columns else 0.0
            volume_ratio = 1.0
            if len(df) >= 2 and "volume" in df.columns:
                prev_vol = float(df["volume"].iloc[-2])
                if prev_vol > 0:
                    volume_ratio = volume / prev_vol

            return [
                close_col, returns, rsi, ema_12, ema_26,
                sma_20, macd_line, macd_signal, macd_hist,
                bb_pct, bb_w, atr_pct, volume_ratio,
            ]
        
        # Build features for the target symbol
        market_features = _build_feature_row(daily_frame)
        
        # Pad with dummy features for additional symbols (use zeros for symbols not in scan)
        for _ in range(n_symbols - 1):
            market_features.extend([0.0] * n_market_features)

        equity = max(cash_ratio * 100_000, 1e-8) if cash_ratio > 0 else 1e-8
        position_weight_sum = portfolio_weight
        realized_pnl_pct = unrealized_pnl_pct

        portfolio_features = [
            cash_ratio, 1 if portfolio_weight > 0 else 0, position_weight_sum,
            unrealized_pnl_pct, realized_pnl_pct,
        ]

        all_features = market_features + portfolio_features
        n_features = len(all_features)

        history_list = [all_features]
        observer_window = obs_shape[0]
        padding_rows = observer_window - len(history_list)
        zero_row = [0.0] * n_features
        for _ in range(padding_rows):
            history_list.insert(0, zero_row)

        observation = np.array(history_list[:observer_window], dtype=np.float32)

        raw_action, _ = self._model.predict(observation, deterministic=True)
        raw_action = int(raw_action)

        # Map raw BSH action to normalized action for this ticker
        # Action space: Discrete(n_symbols * 3 + 1)
        #   0 = global HOLD
        #   symbol_idx*3 + 1 = HOLD for symbol
        #   symbol_idx*3 + 2 = BUY for symbol
        #   symbol_idx*3 + 3 = SELL for symbol
        symbol_list = [s.upper().strip() for s in (symbols or self.config.env_config.symbols or [ticker])]
        try:
            symbol_idx = symbol_list.index(ticker.upper().strip())
        except ValueError:
            symbol_idx = 0

        normalized_action = 0  # HOLD
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
                # Confidence = probability assigned to relevant actions for this symbol
                relevant_actions = [symbol_idx * 3 + 1, symbol_idx * 3 + 2, symbol_idx * 3 + 3]
                confidence = float(np.sum(probs[a] for a in relevant_actions if 0 <= a < len(probs)))
            else:
                confidence = 0.5
        except Exception:
            confidence = 0.5

        return normalized_action, confidence

    def evaluate(self, n_episodes: int = 10) -> dict[str, float]:
        if self._trainer is None:
            raise RuntimeError("No trainer available. Call train() first.")

        return self._trainer.evaluate(n_episodes=n_episodes)

    def save(self, path: str | Path) -> None:
        if self._model is None:
            raise RuntimeError("No model to save. Train or load a model first.")
        self._model.save(path)
        logger.info(f"Model saved to {path}")
