"""Stage 4a: strategy interface and Bollinger / z-score mean reversion."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from event_engine.events import (
    BarType,
    MarketEvent,
    SignalDirection,
)
from event_engine.exceptions import EventValidationError
from event_engine.strategy import (
    AbstractStrategy,
    BollingerZScoreReversionStrategy,
)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


def test_abstract_strategy_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        AbstractStrategy()


def test_bollinger_strategy_subclass_is_an_abstract_strategy():
    s = BollingerZScoreReversionStrategy()
    assert isinstance(s, AbstractStrategy)
    assert s.name == "bollinger_z_reversion"


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_lookup_below_2():
    with pytest.raises(EventValidationError):
        BollingerZScoreReversionStrategy(lookback=1)


def test_constructor_rejects_nonpositive_entry_z():
    with pytest.raises(EventValidationError):
        BollingerZScoreReversionStrategy(entry_z=0.0)
    with pytest.raises(EventValidationError):
        BollingerZScoreReversionStrategy(entry_z=-1.0)


def test_constructor_rejects_negative_exit_z():
    with pytest.raises(EventValidationError):
        BollingerZScoreReversionStrategy(exit_z=-0.5)


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------


def _bar(symbol: str, ts_offset: int, price: float, base_ts: int) -> MarketEvent:
    return MarketEvent(
        timestamp_ns=base_ts + ts_offset,
        symbol=symbol,
        open=price, high=price + 0.1, low=price - 0.1,
        close=price, volume=10_000.0,
        bid_ask_spread=0.02,
        bar_type=BarType.BAR_1M,
    )


def test_no_signal_during_warmup_until_window_filled():
    s = BollingerZScoreReversionStrategy(lookback=5, entry_z=2.0)
    base = int(datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    for i in range(4):  # only 4 of the 5 required closes
        signals = s.calculate_signals(_bar("AAPL", i, 100.0 + i, base))
    assert signals == []


def test_long_signal_when_price_drops_below_band():
    s = BollingerZScoreReversionStrategy(lookback=10, entry_z=1.0)
    base = int(datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    # Build the warm-up with values that cluster around 100.
    for i in range(9):
        s.calculate_signals(_bar("AAPL", i, 100.0 + (i % 3) * 0.01, base))
    # Big drop on bar 9: z ~ -6 / std, well below entry_z=1.0.
    sigs = s.calculate_signals(_bar("AAPL", 9, 90.0, base))
    assert len(sigs) == 1
    assert sigs[0].signal_type is SignalDirection.LONG
    assert sigs[0].target_quantity == 10
    assert 0 < sigs[0].strength <= 1.0


def test_short_signal_when_price_spikes_above_band():
    s = BollingerZScoreReversionStrategy(lookback=10, entry_z=1.5)
    base = int(datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    for i in range(9):
        s.calculate_signals(_bar("AAPL", i, 100.0 + (i % 3) * 0.01, base))
    sigs = s.calculate_signals(_bar("AAPL", 9, 110.0, base))
    assert len(sigs) == 1
    assert sigs[0].signal_type is SignalDirection.SHORT


def test_exit_signal_when_z_reverts_inside_dead_zone():
    """When |z| falls within ``exit_z``, the strategy emits EXIT.
    exit_z=0.5 means a [−0.5, +0.5] band around the rolling mean."""
    s = BollingerZScoreReversionStrategy(lookback=10, entry_z=1.5, exit_z=0.5)
    base = int(datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    for i in range(9):
        s.calculate_signals(_bar("AAPL", i, 100.0 + (i % 3) * 0.01, base))
    s.calculate_signals(_bar("AAPL", 9, 90.0, base))   # enter LONG
    # Bar 10 at $100 — z is small because the dropped bar $90 has
    # rolled out of the 10-bar window and the rest cluster near 100.
    exit_signal = s.calculate_signals(_bar("AAPL", 10, 100.0, base))
    assert len(exit_signal) == 1
    assert exit_signal[0].signal_type is SignalDirection.EXIT


def test_no_exit_outside_dead_zone():
    """When z is *not* inside the [−exit_z, +exit_z] band, no
    exit fires. Here z stays negative, so the LONG is held."""
    s = BollingerZScoreReversionStrategy(lookback=10, entry_z=1.0, exit_z=0.5)
    base = int(datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    # Build a clear down-trend so a long stays long
    for i in range(9):
        s.calculate_signals(_bar("AAPL", i, 100.0 - i * 0.1, base))
    s.calculate_signals(_bar("AAPL", 9, 80.0, base))   # enter LONG
    # Bar 10 slightly higher but still far below rolling mean
    out = s.calculate_signals(_bar("AAPL", 10, 80.5, base))
    assert out == []  # |z| >> exit_z, position holds


def test_no_signal_when_symbol_filter_excludes():
    s = BollingerZScoreReversionStrategy(lookback=5, entry_z=1.0, symbols=["AAPL"])
    base = int(datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    for i in range(5):
        s.calculate_signals(_bar("MSFT", i, 100.0 + i, base))
    assert s.calculate_signals(_bar("MSFT", 9, 50.0, base)) == []


def test_reset_clears_per_symbol_state():
    s = BollingerZScoreReversionStrategy(lookback=10, entry_z=1.0)
    base = int(datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    for i in range(9):
        s.calculate_signals(_bar("AAPL", i, 100.0 + (i % 3) * 0.01, base))
    s.calculate_signals(_bar("AAPL", 9, 90.0, base))  # enter long
    s.reset()
    assert s.calculate_signals(_bar("AAPL", 0, 100.0, base)) == []  # warmup again


def test_signal_strength_normalised_to_unit_interval():
    s = BollingerZScoreReversionStrategy(lookback=10, entry_z=1.0)
    base = int(datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    for i in range(9):
        s.calculate_signals(_bar("AAPL", i, 100.0 + (i % 3) * 0.01, base))
    sigs = s.calculate_signals(_bar("AAPL", 9, 50.0, base))  # huge deviation
    assert 0 <= sigs[0].strength <= 1.0


def test_zero_std_does_not_emit_signal():
    """If the rolling std is zero (perfectly flat window) the strategy
    has nothing to compare against and emits no signal."""
    s = BollingerZScoreReversionStrategy(lookback=5, entry_z=1.0)
    base = int(datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    for _ in range(5):
        out = s.calculate_signals(_bar("AAPL", 0, 100.0, base))
    assert out == []


def test_parameters_helper_returns_canonical_dict():
    s = BollingerZScoreReversionStrategy(lookback=30, entry_z=2.0, exit_z=0.5, signal_scale_qty=50)
    p = s.parameters()
    assert p == {
        "lookback": 30,
        "entry_z": 2.0,
        "exit_z": 0.5,
        "signal_scale_qty": 50,
    }


def test_repr_includes_lookback_and_entry_z():
    s = BollingerZScoreReversionStrategy(lookback=42, entry_z=2.5)
    text = repr(s)
    assert "lookback=42" in text
    assert "entry_z=2.5" in text
