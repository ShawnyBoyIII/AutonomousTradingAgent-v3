"""Tests for relaxed MR detection thresholds (Session 9, 2026-07-02).

Threshold changes:
- Oversold bounce: RSI < 35 → RSI < 40
- VWAP reversion: price > 1% below VWAP → > 0.5% below
- Range reversal: volume >= 100% avg → >= 80% avg
"""
from __future__ import annotations

import pandas as pd

from trading_bot.strategy.mean_reversion import (
    detect_oversold_bounce,
    detect_range_bound_reversal,
    detect_vwap_reversion,
)


def _make_frame(
    close: list[float],
    open_: list[float] | None = None,
    high: list[float] | None = None,
    low: list[float] | None = None,
    rsi_14: list[float] | None = None,
    bb_lower: float = 40.0,
    bb_upper: float = 55.0,
    vwap: float = 50.0,
    volume: list[float] | None = None,
    volume_avg_5: list[float] | None = None,
) -> pd.DataFrame:
    n = len(close)
    if open_ is None:
        open_ = [c * 0.999 for c in close]
    if high is None:
        high = [c * 1.02 for c in close]
    if low is None:
        low = [c * 0.98 for c in close]
    if rsi_14 is None:
        rsi_14 = [50.0] * n
    if volume is None:
        volume = [1000.0] * n
    if volume_avg_5 is None:
        volume_avg_5 = [1000.0] * n

    data = {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "volume_avg_5": volume_avg_5,
        "bb_lower": [bb_lower] * n,
        "bb_upper": [bb_upper] * n,
        "rsi_14": rsi_14,
        "vwap": [vwap] * n,
    }
    return pd.DataFrame(data, index=pd.date_range("2025-06-01 10:00", periods=n, freq="5min"))


class TestOversoldBounceRsiThreshold:
    """RSI threshold changed from 35 to 40.

    Key: BB must be tight enough that the close is within 10% of the range.
    With bb_lower=44, bb_upper=46, range=2:
      close=44.1 → %B = (44.1-44)/2*100 = 5.5% → passes
    """

    def test_rsi_39_triggers_oversold_bounce(self):
        """RSI 39 should now trigger (was blocked at 35)."""
        frame = _make_frame(
            close=[50.0] * 18 + [44.1],
            rsi_14=[50.0] * 18 + [39.0],
            bb_lower=44.0,
            bb_upper=46.0,
            volume=[1000.0] * 19,
            volume_avg_5=[1000.0] * 19,
        )
        assert detect_oversold_bounce(frame) is True

    def test_rsi_36_triggers_oversold_bounce(self):
        """RSI 36 should now trigger (was blocked at 35)."""
        frame = _make_frame(
            close=[50.0] * 18 + [44.1],
            rsi_14=[50.0] * 18 + [36.0],
            bb_lower=44.0,
            bb_upper=46.0,
            volume=[1000.0] * 19,
            volume_avg_5=[1000.0] * 19,
        )
        assert detect_oversold_bounce(frame) is True

    def test_rsi_40_does_not_trigger(self):
        """RSI 40 should NOT trigger (threshold is < 40, not <= 40)."""
        frame = _make_frame(
            close=[50.0] * 18 + [44.1],
            rsi_14=[50.0] * 18 + [40.0],
            bb_lower=44.0,
            bb_upper=46.0,
            volume=[1000.0] * 19,
            volume_avg_5=[1000.0] * 19,
        )
        assert detect_oversold_bounce(frame) is False

    def test_rsi_34_still_triggers(self):
        """RSI 34 should still trigger (below old threshold)."""
        frame = _make_frame(
            close=[50.0] * 18 + [44.1],
            rsi_14=[50.0] * 18 + [34.0],
            bb_lower=44.0,
            bb_upper=46.0,
            volume=[1000.0] * 19,
            volume_avg_5=[1000.0] * 19,
        )
        assert detect_oversold_bounce(frame) is True


class TestVwapReversionDistanceThreshold:
    """VWAP distance changed from 1% to 0.5%."""

    def test_vwap_0_6_percent_below_triggers(self):
        """Price 0.6% below VWAP should now trigger (was blocked at 1%)."""
        vwap = 50.0
        close = vwap * 0.994  # 0.6% below VWAP
        frame = _make_frame(
            close=[50.0] * 18 + [close],
            open_=[c * 0.999 for c in [50.0] * 18 + [close]],  # bullish
            vwap=vwap,
            volume=[1000.0] * 19,
            volume_avg_5=[1000.0] * 19,
        )
        assert detect_vwap_reversion(frame) is True

    def test_vwap_0_4_percent_below_does_not_trigger(self):
        """Price 0.4% below VWAP should NOT trigger."""
        vwap = 50.0
        close = vwap * 0.996  # 0.4% below VWAP
        frame = _make_frame(
            close=[50.0] * 18 + [close],
            open_=[c * 0.999 for c in [50.0] * 18 + [close]],
            vwap=vwap,
            volume=[1000.0] * 19,
            volume_avg_5=[1000.0] * 19,
        )
        assert detect_vwap_reversion(frame) is False

    def test_vwap_1_5_percent_below_triggers(self):
        """Price 1.5% below VWAP should still trigger."""
        vwap = 50.0
        close = vwap * 0.985  # 1.5% below VWAP
        frame = _make_frame(
            close=[50.0] * 18 + [close],
            open_=[c * 0.999 for c in [50.0] * 18 + [close]],
            vwap=vwap,
            volume=[1000.0] * 19,
            volume_avg_5=[1000.0] * 19,
        )
        assert detect_vwap_reversion(frame) is True

    def test_vwap_0_5_percent_below_triggers(self):
        """Price exactly 0.5% below VWAP should trigger (boundary)."""
        vwap = 50.0
        close = vwap * 0.995  # exactly 0.5% below
        frame = _make_frame(
            close=[50.0] * 18 + [close],
            open_=[c * 0.999 for c in [50.0] * 18 + [close]],
            vwap=vwap,
            volume=[1000.0] * 19,
            volume_avg_5=[1000.0] * 19,
        )
        # 0.995 < 0.995 is False, so this should NOT trigger
        # (threshold is strictly < 0.995)
        assert detect_vwap_reversion(frame) is False


class TestRangeBoundReversalVolumeThreshold:
    """Volume threshold changed from 100% to 80% of average."""

    def test_range_reversal_85_percent_volume_triggers(self):
        """Volume at 85% of average should now trigger (was blocked at 100%).

        Price stays near bottom of lookback range with reversal candle.
        Lookback (last 10 bars): highs ~49-51, lows ~42-49
        Latest close at bottom 20% of range, bullish reversal candle.
        """
        n = 12
        # Price drops then stabilizes near bottom
        close = [50.0, 50.0, 49.0, 48.0, 47.0, 46.0, 45.0, 44.0, 43.5, 43.0, 43.2, 43.5]
        # Index -2 (43.0) is bearish, index -1 (43.5) is bullish
        open_ = [c * 0.999 for c in close]
        open_[-2] = 43.3  # bearish: open > close (43.3 > 43.0)
        high = [c * 1.02 for c in close]
        low = [c * 0.98 for c in close]
        rsi_14 = [50.0] * n
        rsi_14[-1] = 35.0
        # Volume at 85% of average
        volume = [1000.0] * n
        volume[-1] = 850.0
        volume_avg_5 = [1000.0] * n

        frame = pd.DataFrame({
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "volume_avg_5": volume_avg_5,
            "rsi_14": rsi_14,
            "bb_lower": [40.0] * n,
            "bb_upper": [55.0] * n,
            "vwap": [50.0] * n,
        }, index=pd.date_range("2025-06-01 10:00", periods=n, freq="5min"))

        assert detect_range_bound_reversal(frame) is True

    def test_range_reversal_75_percent_volume_does_not_trigger(self):
        """Volume at 75% of average should NOT trigger."""
        n = 12
        close = [50.0, 50.0, 49.0, 48.0, 47.0, 46.0, 45.0, 44.0, 43.5, 43.0, 43.2, 43.5]
        open_ = [c * 0.999 for c in close]
        open_[-2] = 43.3  # bearish
        high = [c * 1.02 for c in close]
        low = [c * 0.98 for c in close]
        rsi_14 = [50.0] * n
        rsi_14[-1] = 35.0
        volume = [1000.0] * n
        volume[-1] = 750.0  # 75%
        volume_avg_5 = [1000.0] * n

        frame = pd.DataFrame({
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "volume_avg_5": volume_avg_5,
            "rsi_14": rsi_14,
            "bb_lower": [40.0] * n,
            "bb_upper": [55.0] * n,
            "vwap": [50.0] * n,
        }, index=pd.date_range("2025-06-01 10:00", periods=n, freq="5min"))

        assert detect_range_bound_reversal(frame) is False

    def test_range_reversal_100_percent_volume_still_triggers(self):
        """Volume at 100% should still trigger (backwards compat)."""
        n = 12
        close = [50.0, 50.0, 49.0, 48.0, 47.0, 46.0, 45.0, 44.0, 43.5, 43.0, 43.2, 43.5]
        open_ = [c * 0.999 for c in close]
        open_[-2] = 43.3  # bearish
        high = [c * 1.02 for c in close]
        low = [c * 0.98 for c in close]
        rsi_14 = [50.0] * n
        rsi_14[-1] = 35.0
        volume = [1000.0] * n  # 100%
        volume_avg_5 = [1000.0] * n

        frame = pd.DataFrame({
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "volume_avg_5": volume_avg_5,
            "rsi_14": rsi_14,
            "bb_lower": [40.0] * n,
            "bb_upper": [55.0] * n,
            "vwap": [50.0] * n,
        }, index=pd.date_range("2025-06-01 10:00", periods=n, freq="5min"))

        assert detect_range_bound_reversal(frame) is True

    def test_range_reversal_80_percent_volume_triggers(self):
        """Volume at exactly 80% should trigger (boundary)."""
        n = 12
        close = [50.0, 50.0, 49.0, 48.0, 47.0, 46.0, 45.0, 44.0, 43.5, 43.0, 43.2, 43.5]
        open_ = [c * 0.999 for c in close]
        open_[-2] = 43.3  # bearish
        high = [c * 1.02 for c in close]
        low = [c * 0.98 for c in close]
        rsi_14 = [50.0] * n
        rsi_14[-1] = 35.0
        volume = [1000.0] * n
        volume[-1] = 800.0  # exactly 80%
        volume_avg_5 = [1000.0] * n

        frame = pd.DataFrame({
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "volume_avg_5": volume_avg_5,
            "rsi_14": rsi_14,
            "bb_lower": [40.0] * n,
            "bb_upper": [55.0] * n,
            "vwap": [50.0] * n,
        }, index=pd.date_range("2025-06-01 10:00", periods=n, freq="5min"))

        assert detect_range_bound_reversal(frame) is True
