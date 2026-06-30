from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def detect_intraday_breakout(frame: "pd.DataFrame", lookback: int = 4) -> bool:
    required_columns = {"close", "high", "volume", "volume_avg_5"}
    if lookback <= 0 or len(frame) <= lookback or not required_columns.issubset(frame.columns):
        return False

    latest = frame.iloc[-1]
    prior = frame.iloc[-(lookback + 1) : -1]
    prior_highs_raw = [_to_finite_float(high) for high in prior["high"].tolist()]
    latest_close = _to_finite_float(latest["close"])
    latest_volume = _to_finite_float(latest["volume"])
    average_volume = _to_finite_float(latest["volume_avg_5"])

    if (
        any(value is None for value in prior_highs_raw)
        or latest_close is None
        or latest_volume is None
        or average_volume is None
    ):
        return False

    prior_highs = [value for value in prior_highs_raw if value is not None]
    range_high = max(prior_highs)
    return bool(latest_close > range_high and latest_volume > average_volume)


def detect_intraday_momentum_continuation(frame: "pd.DataFrame") -> bool:
    required_columns = {"close", "high", "low", "volume", "volume_avg_5"}
    if len(frame) < 5 or not required_columns.issubset(frame.columns):
        return False

    latest = frame.iloc[-1]
    previous = frame.iloc[-2]
    recent = frame.iloc[-5:]
    latest_close = _to_finite_float(latest["close"])
    latest_high = _to_finite_float(latest["high"])
    latest_low = _to_finite_float(latest["low"])
    previous_close = _to_finite_float(previous["close"])
    latest_volume = _to_finite_float(latest["volume"])
    average_volume = _to_finite_float(latest["volume_avg_5"])
    recent_closes = [_to_finite_float(close) for close in recent["close"].tolist()]

    if (
        latest_close is None
        or latest_high is None
        or latest_low is None
        or previous_close is None
        or latest_volume is None
        or average_volume is None
        or any(value is None for value in recent_closes)
    ):
        return False

    candle_range = latest_high - latest_low
    if candle_range <= 0:
        return False

    close_near_high = latest_close >= latest_low + (candle_range * 0.65)
    close_above_recent_average = latest_close > sum(value for value in recent_closes if value is not None) / len(recent_closes)
    return bool(
        latest_close > previous_close
        and close_near_high
        and close_above_recent_average
        and latest_volume >= average_volume * 0.8
    )


def identify_intraday_setup(frame: "pd.DataFrame") -> str | None:
    if detect_intraday_breakout(frame):
        return "intraday breakout"
    if detect_intraday_momentum_continuation(frame):
        return "intraday momentum continuation"
    return None


def is_valid_mean_reversion_setup(frame: "pd.DataFrame") -> bool:
    """Check if the intraday frame shows a valid mean-reversion setup.

    Delegates to the three mean-reversion detectors and returns True if
    any one of them fires. Used by the YELLOW acceptance gate when
    ``allow_yellow_mean_reversion`` is enabled.
    """
    from trading_bot.strategy.mean_reversion import (
        detect_oversold_bounce,
        detect_range_bound_reversal,
        detect_vwap_reversion,
    )

    return bool(
        detect_oversold_bounce(frame)
        or detect_vwap_reversion(frame)
        or detect_range_bound_reversal(frame)
    )


def compute_v25_confluence_score(details: dict) -> float:
    """Compute a simplified 0-12 confluence score for V2.5 signals.

    Combines volume, breakout strength, and daily regime into a single
    quality metric. Used as an entry gate when ``min_entry_confluence_score``
    is configured.

    Components (each 0-4):
      - volume_score: scaled by volume_ratio (0 → 0, >=2.0 → 4)
      - breakout_score: how far close is above range_high in percent
      - regime_score: daily_close vs ema_20 vs sma_50 alignment
    """
    score = 0.0

    vr = float(details.get("volume_ratio", 0) or 0)
    score += min(4.0, max(0.0, vr * 2.0))

    close = float(details.get("intraday_close", 0) or 0)
    range_high = float(details.get("range_high", 0) or 0)
    if range_high > 0 and close > range_high:
        pct_above = (close - range_high) / range_high * 100.0
        score += min(4.0, max(0.0, pct_above * 2.0))

    dc = float(details.get("daily_close", 0) or 0)
    ema = float(details.get("ema_20", 0) or 0)
    sma = float(details.get("sma_50", 0) or 0)
    if dc > ema > sma:
        score += 4.0
    elif dc > ema or ema > sma:
        score += 2.0
    elif dc > sma:
        score += 1.0

    return round(score, 2)


def _to_finite_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(numeric):
        return None

    return numeric
