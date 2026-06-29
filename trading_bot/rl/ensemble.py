from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from trading_bot.rl.agent import RLAgent
from trading_bot.rl.utils import rl_model_symbols

logger = logging.getLogger(__name__)


@dataclass
class ModelSignal:
    """Signal from a single RL model."""
    model_name: str
    ticker: str
    action: int  # 0=HOLD, 1=BUY, 2=SELL
    confidence: float
    trained_symbols: list[str]


@dataclass
class EnsembleSignal:
    """Aggregated signal from multiple RL models."""
    ticker: str
    model_signals: list[ModelSignal]
    # Agreement: fraction of models that agree on the same action
    agreement: float
    # Majority action
    majority_action: int
    # Majority confidence (avg confidence of agreeing models)
    majority_confidence: float
    # Whether all models agree
    unanimous: bool


class RLEnsemble:
    """Run multiple RL models in parallel and aggregate signals.

    Each model is trained on its own symbol set. During inference,
    the ensemble runs all models and aggregates their signals.
    """

    def __init__(self, model_paths: list[str | Path]) -> None:
        self._model_paths = [Path(p) for p in model_paths]
        self._agents: list[tuple[RLAgent, str]] = []

    def load(self) -> list[str]:
        """Load all models. Returns list of model names."""
        self._agents = []
        for path in self._model_paths:
            try:
                agent = RLAgent.load(path)
                meta_symbols = rl_model_symbols(path) or []
                model_name = path.name
                self._agents.append((agent, model_name))
                logger.info(f"Ensemble loaded: {model_name} (symbols={','.join(meta_symbols)})")
            except Exception as e:
                logger.warning(f"Ensemble failed to load {path}: {e}")
        return [name for _, name in self._agents]

    def predict(
        self,
        ticker: str,
        market_frames: dict[str, pd.DataFrame],
        portfolio_weight: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
        cash_ratio: float = 1.0,
    ) -> EnsembleSignal:
        """Run all models on a ticker. Returns aggregated signal."""
        model_signals: list[ModelSignal] = []

        for agent, model_name in self._agents:
            try:
                trained_symbols = rl_model_symbols(agent.config.model_path) or []
                action, confidence = agent.predict_signal(
                    daily_frame=market_frames.get(ticker, pd.DataFrame()),
                    ticker=ticker,
                    portfolio_weight=portfolio_weight,
                    unrealized_pnl_pct=unrealized_pnl_pct,
                    cash_ratio=cash_ratio,
                    symbols=trained_symbols,
                    market_frames=market_frames,
                )
                model_signals.append(ModelSignal(
                    model_name=model_name,
                    ticker=ticker,
                    action=action,
                    confidence=confidence,
                    trained_symbols=trained_symbols,
                ))
            except Exception as e:
                logger.warning(f"Model {model_name} prediction failed for {ticker}: {e}")

        return self._aggregate(ticker, model_signals)

    def predict_batch(
        self,
        tickers: list[str],
        market_frames: dict[str, pd.DataFrame],
        portfolio_weight: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
        cash_ratio: float = 1.0,
    ) -> dict[str, EnsembleSignal]:
        """Run all models on multiple tickers."""
        results = {}
        for ticker in tickers:
            results[ticker] = self.predict(
                ticker, market_frames,
                portfolio_weight=portfolio_weight,
                unrealized_pnl_pct=unrealized_pnl_pct,
                cash_ratio=cash_ratio,
            )
        return results

    def _aggregate(
        self,
        ticker: str,
        model_signals: list[ModelSignal],
    ) -> EnsembleSignal:
        """Aggregate multiple model signals into one."""
        if not model_signals:
            return EnsembleSignal(
                ticker=ticker,
                model_signals=[],
                agreement=0.0,
                majority_action=0,
                majority_confidence=0.0,
                unanimous=False,
            )

        # Count actions
        action_counts: dict[int, list[ModelSignal]] = {}
        for ms in model_signals:
            action_counts.setdefault(ms.action, []).append(ms)

        # Majority action = most common
        majority_action = max(action_counts, key=lambda a: len(action_counts[a]))
        agreeing = action_counts[majority_action]

        # Agreement = fraction of models that agree
        agreement = len(agreeing) / len(model_signals)

        # Majority confidence = avg of agreeing models
        majority_confidence = sum(m.confidence for m in agreeing) / len(agreeing)

        # Unanimous = all models agree
        unanimous = len(agreeing) == len(model_signals)

        return EnsembleSignal(
            ticker=ticker,
            model_signals=model_signals,
            agreement=agreement,
            majority_action=majority_action,
            majority_confidence=majority_confidence,
            unanimous=unanimous,
        )

    @property
    def model_count(self) -> int:
        return len(self._agents)

    @property
    def model_names(self) -> list[str]:
        return [name for _, name in self._agents]


def discover_rl_models(rl_dir: str = "state/rl_logs") -> list[str]:
    """Find all trained RL models in the RL directory."""
    rl_path = Path(rl_dir)
    models: list[str] = []
    if not rl_path.exists():
        return models

    for item in rl_path.iterdir():
        if item.is_dir():
            # Check for model files in subdirectory
            for sub in item.iterdir():
                if sub.suffix == ".zip" and sub.stem.startswith("PPO_"):
                    models.append(str(sub))
        elif item.suffix == ".zip" and item.stem.startswith("PPO_"):
            models.append(str(item))

    return models


def save_discovered_symbols(symbols: list[str], path: str = "state/rl_logs/discovered_symbols.txt") -> None:
    """Save discovered symbols for RL retraining."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(s.upper().strip() for s in symbols))


def load_discovered_symbols(path: str = "state/rl_logs/discovered_symbols.txt") -> list[str]:
    """Load discovered symbols for RL retraining."""
    p = Path(path)
    if not p.exists():
        return []
    return [s.strip().upper() for s in p.read_text().splitlines() if s.strip()]
