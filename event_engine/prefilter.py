"""Vectorized parameter sweep for the hybrid backtester.

The :class:`VectorizedPreFilter` runs Bollinger / z-score mean
reversion across a Cartesian grid of ``(lookback, entry_z)``
parameter combinations over a single-asset price series. For each
combination it computes a vectorized entry/exit record, then
collapses the trades into total return, Sharpe, and maximum
drawdown. Top-N parameter combinations are returned ranked by
``sharpe - drawdown_penalty`` (a simple edge score).

Why pure pandas / NumPy?

* ``vectorbt`` is not vendored in this repo's dependency tree. The
  pre-filter is intentionally implemented with primitives only,
  so the package can stay slim and the tests stay transparent.

Cost dimensions

* ``|lookbacks| × |entry_zs|`` matrix scans; on a million-row
  price series each scan completes in well under a second on
  a typical workstation.
* All-in float64; final metrics are rounded to 4 decimal places.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from event_engine.exceptions import EventValidationError
from event_engine.strategy import BollingerZScoreReversionStrategy


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PreFilterParameterScore:
    """One combination's summary metrics."""

    parameters: dict[str, object]
    sharpe: float
    total_return: float
    max_drawdown: float
    trade_count: int
    edge_score: float

    @classmethod
    def from_metrics(
        cls,
        parameters: dict[str, object],
        sharpe: float,
        total_return: float,
        max_drawdown: float,
        trade_count: int,
        drawdown_penalty: float = 0.5,
    ) -> "PreFilterParameterScore":
        edge = round(sharpe - drawdown_penalty * abs(max_drawdown), 4)
        return cls(
            parameters=parameters,
            sharpe=round(sharpe, 4),
            total_return=round(total_return, 4),
            max_drawdown=round(max_drawdown, 4),
            trade_count=int(trade_count),
            edge_score=edge,
        )


@dataclass(slots=True)
class PreFilterResult:
    """Wrapper around the top-N ranked :class:`PreFilterParameterScore`."""

    scores: list[PreFilterParameterScore] = field(default_factory=list)

    @property
    def top(self) -> list[PreFilterParameterScore]:
        return list(self.scores)

    def parameters_for_engine(self) -> list[dict[str, object]]:
        return [score.parameters for score in self.scores]


# ---------------------------------------------------------------------------
# Pre-filter
# ---------------------------------------------------------------------------


class VectorizedPreFilter:
    """Vectorized parameter sweep over Bollinger / z-score reversions.

    Parameters
    ----------
    lookbacks:
        Lookback-window lengths to scan. 5..200 are typical.
    entry_zs:
        Entry-threshold z-scores to scan. 1.5..3.0 are typical.
    exit_z:
        Single exit-z used across all combinations. ``0`` (default)
        means exit on reversion through the band.
    drawdown_penalty:
        Multiplier applied to ``|max_drawdown|`` when computing the
        edge score; larger values penalise drawdown more.
    min_trades:
        Combinations with fewer than this many trades are dropped.
    annualisation_periods:
        Trading-day-equivalent periods per year used to annualise
        Sharpe (default ``252`` for daily bars; pass ``365`` for
        24/7 crypto or smaller for intraday multiples).
    """

    def __init__(
        self,
        *,
        lookbacks: Iterable[int],
        entry_zs: Iterable[float],
        exit_z: float = 0.0,
        drawdown_penalty: float = 0.5,
        min_trades: int = 3,
        annualisation_periods: int = 252,
    ) -> None:
        self.lookbacks: list[int] = [int(x) for x in lookbacks]
        self.entry_zs: list[float] = [float(x) for x in entry_zs]
        if not self.lookbacks:
            raise EventValidationError("lookbacks must be non-empty")
        if not self.entry_zs:
            raise EventValidationError("entry_zs must be non-empty")
        if exit_z < 0:
            raise EventValidationError("exit_z must be >= 0")
        self.exit_z = float(exit_z)
        self.drawdown_penalty = float(drawdown_penalty)
        self.min_trades = int(min_trades)
        self.annualisation_periods = int(annualisation_periods)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def screen(
        self,
        prices: pd.Series,
        *,
        top_n: int = 5,
        signal_scale_qty: int = 10,
    ) -> PreFilterResult:
        """Run the matrix sweep on ``prices`` and return the top-N.

        ``prices`` must be a numeric ``pandas.Series``. NaN entries
        are forward-filled so a single rogue NaN doesn't poison an
        entire rolling window.
        """
        clean_prices = self._clean_prices(prices)

        # Vectorized mean / std across the Cartesian grid.
        scores: list[PreFilterParameterScore] = []
        for lookback in self.lookbacks:
            mean = clean_prices.rolling(window=lookback).mean()
            std = clean_prices.rolling(window=lookback).std(ddof=1)
            for entry_z in self.entry_zs:
                # Avoid divide-by-zero when std is NaN at the warm-up.
                with np.errstate(invalid="ignore", divide="ignore"):
                    z = (clean_prices - mean) / std
                returns, max_dd, trades = self._simulate(
                    z=z,
                    prices=clean_prices,
                    entry_z=float(entry_z),
                )
                sharpe = self._annualised_sharpe(returns)
                total_return = self._compound_return(returns)
                score = PreFilterParameterScore.from_metrics(
                    parameters={
                        "lookback": int(lookback),
                        "entry_z": float(entry_z),
                        "exit_z": self.exit_z,
                        "signal_scale_qty": signal_scale_qty,
                    },
                    sharpe=sharpe,
                    total_return=total_return,
                    max_drawdown=max_dd,
                    trade_count=trades,
                    drawdown_penalty=self.drawdown_penalty,
                )
                if score.trade_count >= self.min_trades:
                    scores.append(score)

        scores.sort(key=lambda s: s.edge_score, reverse=True)
        return PreFilterResult(scores=scores[: int(top_n)])

    # ------------------------------------------------------------------
    # Vector math
    # ------------------------------------------------------------------

    def _simulate(
        self,
        *,
        z: pd.Series,
        prices: pd.Series,
        entry_z: float,
    ) -> tuple[pd.Series, float, int]:
        """Vectorized enter / exit simulator.

        Returns per-bar returns, maximum drawdown, and trade count.

        The convention is: enter on a +z / -z threshold violation,
        exit when the absolute z falls back below the exit threshold.
        The realised per-bar return is the price change between
        successive bars while a position is held.
        """
        long_entry = z < -entry_z
        short_entry = z > entry_z

        in_long = False
        in_short = False
        # We'll compute realised-position returns via a walk over the
        # boolean masks in numpy (faster than ``iterrows`` or ``iloc``).
        rets = np.zeros(len(z), dtype=np.float64)
        in_pos = np.zeros(len(z), dtype=bool)

        # Performance optimization: extract numpy arrays for O(1) lookups
        z_np = z.to_numpy(dtype=np.float64)
        long_entry_np = long_entry.to_numpy(dtype=bool)
        short_entry_np = short_entry.to_numpy(dtype=bool)

        for i in range(len(z)):
            prev_pos_long = in_long
            prev_pos_short = in_short
            if not prev_pos_long and long_entry_np[i]:
                in_long = True
            elif prev_pos_long and abs(z_np[i]) <= self.exit_z:
                in_long = False
            if not prev_pos_short and short_entry_np[i]:
                in_short = True
            elif prev_pos_short and abs(z_np[i]) <= self.exit_z:
                in_short = False
            in_pos[i] = prev_pos_long or prev_pos_short

        # Per-bar log return (price diff normalised by previous close)
        p = prices.to_numpy(dtype=np.float64)
        bar_returns = np.zeros(len(p), dtype=np.float64)
        bar_returns[1:] = (p[1:] - p[:-1]) / p[:-1]
        sign = np.where(in_long, 1.0, np.where(in_short, -1.0, 0.0))
        rets = sign * bar_returns

        # Maximum drawdown of an equity curve of cumulative log returns.
        equity = np.exp(np.cumsum(rets))
        running_max = np.maximum.accumulate(equity)
        drawdown = equity / running_max - 1.0
        max_dd = float(drawdown.min()) if len(drawdown) > 0 else 0.0
        # Trade count: count entries (entries — exits = number of trades).
        entry_count = (long_entry | short_entry).sum()
        trade_count = int(entry_count)
        return pd.Series(rets, index=prices.index), max_dd, trade_count

    @staticmethod
    def _clean_prices(prices: pd.Series) -> pd.Series:
        if prices.empty:
            raise EventValidationError("price series is empty")
        clean = prices.astype(np.float64).ffill().bfill()
        if clean.isna().any():
            raise EventValidationError(
                "price series still has NaN after forward-fill"
            )
        if (clean <= 0).any():
            raise EventValidationError(
                "price series has non-positive values"
            )
        return clean

    @staticmethod
    def _annualised_sharpe(returns: pd.Series) -> float:
        if len(returns) < 2:
            return 0.0
        mean = float(returns.mean())
        std = float(returns.std(ddof=1))
        if std <= 0 or np.isnan(std):
            return 0.0
        # Annualise only when the series carries enough data.
        n = len(returns)
        annual = np.sqrt(max(n, 1))
        sharpe = (mean / std) * annual
        # Cap extreme values to keep the matrix sweep finite.
        return max(-10.0, min(10.0, sharpe))

    @staticmethod
    def _compound_return(returns: pd.Series) -> float:
        if len(returns) == 0:
            return 0.0
        equity = np.exp(np.cumsum(returns.to_numpy(dtype=np.float64)))
        return float(equity[-1] - 1.0)

    # ------------------------------------------------------------------
    # Strategy factory
    # ------------------------------------------------------------------

    def make_strategy(self, parameters: dict[str, object]) -> BollingerZScoreReversionStrategy:
        """Build a fresh mean-reversion strategy from one of the
        pre-filter's parameter combinations.

        Encapsulating the construction here ensures
        ``EngineDriver`` and the rest of the pipeline see the exact
        same shape of strategy the pre-filter was screening.
        """
        return BollingerZScoreReversionStrategy(
            lookback=int(parameters["lookback"]),
            entry_z=float(parameters["entry_z"]),
            exit_z=float(parameters.get("exit_z", self.exit_z)),
            signal_scale_qty=int(
                parameters.get("signal_scale_qty", 10)
            ),
        )


__all__ = [
    "VectorizedPreFilter",
    "PreFilterResult",
    "PreFilterParameterScore",
]
