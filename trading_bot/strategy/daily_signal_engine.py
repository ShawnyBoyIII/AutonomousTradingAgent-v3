from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.daily_filter import is_bullish_daily_regime


def generate_daily_signal(
    symbol: str,
    daily_frame: "pd.DataFrame",
    index: int,
) -> TradeSignal | None:
    """Generate a trade signal from daily bar data.

    Looks for:
    - Bullish daily regime (close > EMA20 > SMA50)
    - Breakout above previous day's high
    - Volume surge (> 1.5x 20-day average)
    - Pullback to EMA20 support (optional entry refinement)

    Returns TradeSignal or None if no setup.
    """
    if index < 20:  # Need warmup for volume average
        return None

    if not is_bullish_daily_regime(daily_frame.iloc[: index + 1]):
        return None

    today = daily_frame.iloc[index]
    yesterday = daily_frame.iloc[index - 1]

    # Check for breakout above yesterday's high
    if today["close"] <= yesterday["high"]:
        return None

    # Volume surge check
    volume_avg = daily_frame["volume"].iloc[index - 19 : index + 1].mean()
    if today["volume"] < volume_avg * 1.5:
        return None

    # Calculate stop and target based on ATR
    atr_col = "atr_14"
    if atr_col in today and today[atr_col] > 0:
        stop_distance = today[atr_col] * 2.0
    else:
        # Fallback: use yesterday's range
        stop_distance = yesterday["high"] - yesterday["low"]

    entry = today["close"]
    stop_loss = entry - stop_distance
    profit_target = entry + (stop_distance * 2.0)  # 2:1 R/R
    risk_reward = 2.0

    return TradeSignal(
        ticker=symbol,
        timeframe="daily",
        action="BUY",
        entry_price=entry,
        stop_loss=stop_loss,
        profit_target=profit_target,
        risk_reward_ratio=risk_reward,
        confidence=0.7,
        reasons=["daily_breakout", "volume_surge", "bullish_regime"],
        strategy_tag="daily_breakout_v1",
        timestamp=today.get("timestamp", daily_frame.index[index]),
    )
