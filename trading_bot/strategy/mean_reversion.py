"""Mean reversion signal detection using Bollinger Bands, RSI, and VWAP."""

from __future__ import annotations

import math
import numpy as np
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def detect_oversold_bounce(frame: "pd.DataFrame") -> bool:
    """Detect oversold bounce signal using Bollinger Bands and RSI.

    Signal criteria:
    - Price touches or crosses below lower Bollinger Band (%B <= 10)
    - RSI below 40 (oversold, captures more mean-reversion setups)
    - Bullish candlestick (close > open)
    - Volume confirmation (volume >= 80% of average)
    """
    required_columns = {
        "close", "open", "high", "low", "volume", "volume_avg_5",
        "bb_lower", "bb_upper", "rsi_14"
    }
    if len(frame) < 2 or not required_columns.issubset(frame.columns):
        return False

    latest = frame.iloc[-1]
    previous = frame.iloc[-2]

    latest_close = _to_finite_float(latest["close"])
    latest_open = _to_finite_float(latest["open"])
    latest_low = _to_finite_float(latest["low"])
    latest_volume = _to_finite_float(latest["volume"])
    avg_volume = _to_finite_float(latest["volume_avg_5"])
    bb_lower = _to_finite_float(latest["bb_lower"])
    bb_upper = _to_finite_float(latest["bb_upper"])
    rsi = _to_finite_float(latest["rsi_14"])

    if any(v is None for v in [latest_close, latest_open, latest_low, latest_volume,
                               avg_volume, bb_lower, bb_upper, rsi]):
        return False

    # Check if price is near or below lower band (%B <= 5)
    bb_range = bb_upper - bb_lower
    if bb_range <= 0:
        return False

    percent_b = (latest_close - bb_lower) / bb_range * 100

    # Criteria:
    # 1. Price near or below lower band (%B <= 10)
    # 2. RSI oversold (below 40 captures more MR setups)
    # 3. Bullish candle (close > open) OR price already below lower band
    # 4. Volume confirmation
    near_lower_band = percent_b <= 10.0
    oversold_rsi = rsi < 40.0
    bullish_candle = latest_close > latest_open
    below_band = percent_b < 0.0
    volume_ok = latest_volume >= avg_volume * 0.8

    return bool(near_lower_band and oversold_rsi and (bullish_candle or below_band) and volume_ok)


def detect_vwap_reversion(frame: "pd.DataFrame") -> bool:
    """Detect VWAP mean reversion signal.

    Signal criteria:
    - Price has extended below VWAP (> 0.5% away)
    - Showing bullish reversal candle (close > open, close in upper half)
    - Volume confirmation
    """
    required_columns = {"close", "open", "high", "low", "vwap", "volume", "volume_avg_5"}
    if len(frame) < 2 or not required_columns.issubset(frame.columns):
        return False

    latest = frame.iloc[-1]

    latest_close = _to_finite_float(latest["close"])
    latest_open = _to_finite_float(latest["open"])
    latest_high = _to_finite_float(latest["high"])
    latest_low = _to_finite_float(latest["low"])
    vwap = _to_finite_float(latest["vwap"])
    latest_volume = _to_finite_float(latest["volume"])
    avg_volume = _to_finite_float(latest["volume_avg_5"])

    if any(v is None for v in [latest_close, latest_open, latest_high, latest_low,
                               vwap, latest_volume, avg_volume]):
        return False

    # Price extended below VWAP (> 0.5%)
    below_vwap = latest_close < vwap * 0.995

    # Bullish candle
    bullish = latest_close > latest_open

    # Close in upper half of range
    candle_range = latest_high - latest_low
    if candle_range <= 0:
        return False

    close_in_upper_half = latest_close >= latest_low + (candle_range * 0.5)

    # Volume confirmation
    volume_ok = latest_volume >= avg_volume * 0.8

    return bool(below_vwap and bullish and close_in_upper_half and volume_ok)


def detect_range_bound_reversal(frame: "pd.DataFrame", lookback: int = 10) -> bool:
    """Detect reversal in range-bound market.

    Signal criteria:
    - Price near recent low (within bottom 20% of lookback range)
    - Previous bar was bearish, current bar is bullish
    - Volume confirmation (>= 80% of average)
    - RSI not extreme (< 40)
    """
    required_columns = {"close", "open", "high", "low", "volume", "volume_avg_5", "rsi_14"}
    if len(frame) < lookback + 1 or not required_columns.issubset(frame.columns):
        return False

    latest = frame.iloc[-1]
    previous = frame.iloc[-2]
    lookback_period = frame.iloc[-lookback:]

    latest_close = _to_finite_float(latest["close"])
    latest_open = _to_finite_float(latest["open"])
    prev_close = _to_finite_float(previous["close"])
    prev_open = _to_finite_float(previous["open"])
    latest_volume = _to_finite_float(latest["volume"])
    avg_volume = _to_finite_float(latest["volume_avg_5"])
    rsi = _to_finite_float(latest["rsi_14"])

    # Get lookback highs and lows
    lookback_highs = [h if math.isfinite(h) else None for h in lookback_period["high"].to_numpy(dtype=float, na_value=np.nan).tolist()]
    lookback_lows = [l if math.isfinite(l) else None for l in lookback_period["low"].to_numpy(dtype=float, na_value=np.nan).tolist()]

    if any(v is None for v in [latest_close, latest_open, prev_close, prev_open,
                               latest_volume, avg_volume, rsi]):
        return False

    if any(h is None for h in lookback_highs) or any(l is None for l in lookback_lows):
        return False

    period_high = max(lookback_highs)
    period_low = min(lookback_lows)
    period_range = period_high - period_low

    if period_range <= 0:
        return False

    # Price near bottom 20% of range
    price_position = (latest_close - period_low) / period_range
    near_bottom = price_position <= 0.20

    # Reversal pattern: previous bearish, current bullish
    prev_bearish = prev_close < prev_open
    curr_bullish = latest_close > latest_open

    # Volume confirmation (>= 80% of average)
    volume_ok = latest_volume >= avg_volume * 0.8

    # RSI not extreme
    rsi_ok = rsi < 40.0

    return bool(near_bottom and prev_bearish and curr_bullish and volume_ok and rsi_ok)


def identify_mean_reversion_setup(frame: "pd.DataFrame") -> str | None:
    """Identify mean reversion setup from available signals.

    Priority order:
    1. Oversold bounce (Bollinger Bands + RSI)
    2. VWAP reversion
    3. Range-bound reversal
    """
    if detect_oversold_bounce(frame):
        return "oversold bounce"
    if detect_vwap_reversion(frame):
        return "vwap reversion"
    if detect_range_bound_reversal(frame):
        return "range reversal"
    return None


def _to_finite_float(value: object) -> float | None:
    """Convert value to finite float or None."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(numeric):
        return None

    return numeric
