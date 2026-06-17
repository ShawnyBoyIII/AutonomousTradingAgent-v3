from __future__ import annotations

from datetime import datetime
import math
from typing import TYPE_CHECKING

from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.daily_filter import is_bullish_daily_regime
from trading_bot.strategy.setup_rules import identify_intraday_setup

if TYPE_CHECKING:
    import pandas as pd


def generate_signal(
    symbol: str,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
) -> TradeSignal | None:
    signal, _ = generate_signal_with_reason(symbol, daily_frame, intraday_frame)
    return signal


def generate_signal_with_reason(
    symbol: str,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
) -> tuple[TradeSignal | None, str]:
    if not is_bullish_daily_regime(daily_frame):
        return None, "daily regime not bullish"

    setup_reason = identify_intraday_setup(intraday_frame)
    if setup_reason is None:
        return None, "no intraday setup"

    latest = intraday_frame.iloc[-1]
    entry_value = _to_finite_float(latest.get("close"))
    if entry_value is None:
        return None, "invalid entry price"
    entry_price = round(entry_value, 4)

    if "low" in intraday_frame.columns:
        recent_lows = intraday_frame.tail(min(len(intraday_frame), 5))["low"]
        low_values = [
            numeric
            for numeric in (_to_finite_float(value) for value in recent_lows.tolist())
            if numeric is not None
        ]
        if not low_values:
            return None, "invalid stop price"
        stop_loss = round(min(low_values), 4)
    else:
        stop_loss = round(entry_price * 0.99, 4)

    if stop_loss >= entry_price:
        stop_loss = round(entry_price * 0.99, 4)

    risk = entry_price - stop_loss
    if risk <= 0:
        return None, "invalid risk"

    profit_target = round(entry_price + risk * 2.0, 4)
    rounded_risk = entry_price - stop_loss
    if rounded_risk <= 0:
        return None, "invalid risk"

    confidence = 0.8 if setup_reason == "intraday breakout" else 0.75
    latest_volume = _to_finite_float(latest.get("volume"))
    average_volume = _to_finite_float(latest.get("volume_avg_5"))
    if latest_volume is not None and average_volume is not None and latest_volume > average_volume * 1.5:
        confidence = 0.9

    timestamp = _resolve_signal_timestamp(intraday_frame)
    if timestamp is None:
        return None, "missing signal timestamp"

    return (
        TradeSignal(
            ticker=symbol,
            timeframe="intraday",
            action="BUY",
            entry_price=entry_price,
            stop_loss=stop_loss,
            profit_target=profit_target,
            risk_reward_ratio=round((profit_target - entry_price) / rounded_risk, 6),
            confidence=confidence,
            reasons=["bullish daily regime", setup_reason],
            strategy_tag="intraday-signal-engine",
            timestamp=timestamp,
        ),
        "approved",
    )


def generate_recent_signal(
    symbol: str,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
    lookback_bars: int = 6,
) -> TradeSignal | None:
    signal, _ = generate_recent_signal_with_reason(
        symbol, daily_frame, intraday_frame, lookback_bars=lookback_bars
    )
    return signal


def generate_recent_signal_with_reason(
    symbol: str,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
    lookback_bars: int = 6,
) -> tuple[TradeSignal | None, str]:
    if lookback_bars <= 0:
        return generate_signal_with_reason(symbol, daily_frame, intraday_frame)

    reason = "no intraday setup"
    start_index = max(5, len(intraday_frame) - lookback_bars + 1)
    for end_index in range(len(intraday_frame), start_index - 1, -1):
        signal, reason = generate_signal_with_reason(
            symbol,
            daily_frame,
            intraday_frame.iloc[:end_index].copy(),
        )
        if signal is not None:
            return signal, "approved"
    return None, reason


def _to_finite_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(numeric):
        return None

    return numeric


def _resolve_signal_timestamp(intraday_frame: "pd.DataFrame") -> datetime | None:
    if intraday_frame.empty:
        return None

    candidate = None
    if "timestamp" in intraday_frame.columns:
        candidate = intraday_frame.iloc[-1].get("timestamp")
    else:
        candidate = intraday_frame.index[-1]

    if isinstance(candidate, datetime):
        return candidate

    return None
