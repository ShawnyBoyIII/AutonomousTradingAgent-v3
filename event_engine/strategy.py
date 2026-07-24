"""Strategy interface and a high-frequency/intraday mean-reversion sample.

Conventions:

* Strategies are *stateless across runs*; they may keep per-symbol
  state (rolling windows, last entry price) but those buffers
  start empty for each ``EngineDriver.run()`` invocation.
* Strategies emit :class:`SignalEvent` objects; the
  :class:`~event_engine.portfolio.Portfolio` converts them to
  ``OrderEvent`` instances after capital checks.
* ``AbstractStrategy.calculate_signals`` is called once per
  :class:`MarketEvent`. A strategy returning an empty list emits no
  opinion on that bar; returning a non-empty list emits one entry
  per element. (A future multi-leg strategy could batch legs into
  one SignalEvent with custom fields, but the current dataclass
  encodes one leg.)
* Strategy subclasses are encouraged to expose their tunable
  parameters as plain attributes so :class:`PreFilterParameter` and
  :class:`EngineDriver` can introspect them.

Numerical discipline: rolling z-scores use ``float64`` NumPy
arrays with a final ``round(..., 4)`` on every signal strength so
the deterministic event-driven backtester stays reproducible.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Optional

import numpy as np

from event_engine.events import (
    MarketEvent,
    OrderDirection,
    SignalDirection,
    SignalEvent,
)
from event_engine.exceptions import EventValidationError


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class AbstractStrategy(ABC):
    """Contract every concrete strategy must satisfy."""

    name: str = "abstract"

    @abstractmethod
    def calculate_signals(
        self,
        event: MarketEvent,
    ) -> list[SignalEvent]:
        """Consume ``event`` and emit zero or more signals.

        Returning an empty list is the canonical "no opinion"
        response. Concrete subclasses should *not* mutate global
        state through this call; state lives on ``self``.
        """

    def on_bar(self, event: MarketEvent) -> None:  # noqa: D401
        """Default: pass-through. Override if the strategy wants
        a hook every bar regardless of whether a signal fires.
        """
        return None

    def reset(self) -> None:
        """Drop any per-run state. Called by ``EngineDriver.run``
        before and after each run."""
        return None


# ---------------------------------------------------------------------------
# Bollinger/Z-score mean reversion
# ---------------------------------------------------------------------------


class BollingerZScoreReversionStrategy(AbstractStrategy):
    """Intraday mean reversion with Bollinger bands and a z-score exit.

    Parameters
    ----------
    lookback:
        Number of closes used to estimate the rolling mean & std.
    entry_z:
        |z| above this triggers an entry (long when negative, short
        when positive).
    exit_z:
        |z| must fall below this before an exit fires. Default 0.0
        means exit on the first bar the cross retraces through the
        middle.
    signal_scale_qty:
        Per-symbol integer position size the strategy requests when
        it enters. The ``Portfolio`` may scale this down under
        ``PortfolioPolicy.max_position_value`` /
        ``max_symbol_weight``.
    max_bars_in_trade:
        Optional time-based exit. ``None`` disables.
    symbols:
        Optional allow-list. When set, only bars for these symbols
        produce signals.
    """

    name: str = "bollinger_z_reversion"

    def __init__(
        self,
        lookback: int = 20,
        entry_z: float = 2.0,
        exit_z: float = 0.0,
        signal_scale_qty: int = 10,
        max_bars_in_trade: Optional[int] = None,
        symbols: Optional[list[str]] = None,
    ) -> None:
        if lookback < 2:
            raise EventValidationError(
                f"lookback must be >= 2; got {lookback}"
            )
        if entry_z <= 0:
            raise EventValidationError(
                f"entry_z must be > 0; got {entry_z}"
            )
        if exit_z < 0:
            raise EventValidationError("exit_z must be >= 0")

        self.lookback = int(lookback)
        self.entry_z = float(entry_z)
        self.exit_z = float(exit_z)
        self.signal_scale_qty = int(signal_scale_qty)
        self.max_bars_in_trade = (
            int(max_bars_in_trade) if max_bars_in_trade is not None else None
        )
        self.symbols: Optional[set[str]] = (
            set(symbols) if symbols else None
        )

        self._history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.lookback)
        )
        # Track whether we're currently long/short/flat per symbol so
        # the exit path knows what to close.
        self._position: dict[str, SignalDirection] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        self._history.clear()
        self._position.clear()

    # ------------------------------------------------------------------
    # Signal computation
    # ------------------------------------------------------------------

    def calculate_signals(
        self,
        event: MarketEvent,
    ) -> list[SignalEvent]:
        if self.symbols is not None and event.symbol not in self.symbols:
            return []

        window = self._history[event.symbol]
        window.append(event.close)
        if len(window) < self.lookback:
            return []

        # Rolling mean and std over the last ``lookback`` samples.
        closes = np.fromiter(window, dtype=np.float64, count=len(window))
        mean = closes.mean()
        std = closes.std(ddof=1)
        if std <= 0:
            return []

        z = (event.close - mean) / std
        current = self._position.get(event.symbol)
        signals: list[SignalEvent] = []

        # Exit path: position is open and |z| has reverted enough.
        if current is not None and abs(z) <= self.exit_z:
            signals.append(
                SignalEvent(
                    timestamp_ns=event.timestamp_ns,
                    symbol=event.symbol,
                    signal_type=SignalDirection.EXIT,
                    strength=0.0,
                    target_quantity=0,
                )
            )
            self._position[event.symbol] = None  # type: ignore[assignment]
            return signals

        # Entry path: flat and |z| above the entry threshold.
        if current is None and abs(z) >= self.entry_z:
            signal_type = (
                SignalDirection.LONG if z < 0 else SignalDirection.SHORT
            )
            # Strength is the |z| normalised into [0, 1] using
            # 2 × entry_z as the saturation ceiling so a deeper
            # deviation produces a stronger signal.
            strength = min(1.0, abs(z) / max(self.entry_z * 2.0, 1e-9))
            signals.append(
                SignalEvent(
                    timestamp_ns=event.timestamp_ns,
                    symbol=event.symbol,
                    signal_type=signal_type,
                    strength=round(strength, 4),
                    target_quantity=self.signal_scale_qty,
                )
            )
            self._position[event.symbol] = signal_type  # type: ignore[assignment]
        return signals

    # ------------------------------------------------------------------
    # Introspection helpers (used by VectorizedPreFilter)
    # ------------------------------------------------------------------

    def parameters(self) -> dict[str, object]:
        """Snapshot of tunable parameters for the pre-filter."""
        return {
            "lookback": self.lookback,
            "entry_z": self.entry_z,
            "exit_z": self.exit_z,
            "signal_scale_qty": self.signal_scale_qty,
        }

    def __repr__(self) -> str:
        p = self.parameters()
        return (
            f"{self.name}(lookback={p['lookback']}, entry_z={p['entry_z']}, "
            f"exit_z={p['exit_z']}, qty={p['signal_scale_qty']})"
        )


__all__ = [
    "AbstractStrategy",
    "BollingerZScoreReversionStrategy",
]
