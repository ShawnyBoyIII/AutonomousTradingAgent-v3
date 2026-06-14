from __future__ import annotations

from datetime import datetime
import math
from typing import TYPE_CHECKING

from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.daily_filter import is_bullish_daily_regime
from trading_bot.strategy.setup_rules import detect_intraday_breakout

if TYPE_CHECKING:
    import pandas as pd


def generate_signal(
    symbol: str,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
) -> TradeSignal | None:
    if not is_bullish_daily_regime(daily_frame):
        return None

    if not detect_intraday_breakout(intraday_frame):
        return None

    latest = intraday_frame.iloc[-1]
    entry_value = _to_finite_float(latest.get("close"))
    if entry_value is None:
        return None
    entry_price = round(entry_value, 4)

    if "low" in intraday_frame.columns:
        recent_lows = intraday_frame.tail(min(len(intraday_frame), 5))["low"]
        low_values = [
            numeric
            for numeric in (_to_finite_float(value) for value in recent_lows.tolist())
            if numeric is not None
        ]
        if not low_values:
            return None
        stop_loss = round(min(low_values), 4)
    else:
        stop_loss = round(entry_price * 0.99, 4)

    if stop_loss >= entry_price:
        stop_loss = round(entry_price * 0.99, 4)

    risk = entry_price - stop_loss
    if risk <= 0:
        return None

    profit_target = round(entry_price + risk * 2.0, 4)
    rounded_risk = entry_price - stop_loss
    if rounded_risk <= 0:
        return None

    confidence = 0.8
    latest_volume = _to_finite_float(latest.get("volume"))
    average_volume = _to_finite_float(latest.get("volume_avg_5"))
    if latest_volume is not None and average_volume is not None and latest_volume > average_volume * 1.5:
        confidence = 0.9

    timestamp = intraday_frame.index[-1]
    if not isinstance(timestamp, datetime):
        return None

    return TradeSignal(
        ticker=symbol,
        timeframe="intraday",
        action="BUY",
        entry_price=entry_price,
        stop_loss=stop_loss,
        profit_target=profit_target,
        risk_reward_ratio=round((profit_target - entry_price) / rounded_risk, 6),
        confidence=confidence,
        reasons=["bullish daily regime", "intraday breakout"],
        strategy_tag="intraday-signal-engine",
        timestamp=timestamp,
    )


def _to_finite_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(numeric):
        return None

    return numeric
