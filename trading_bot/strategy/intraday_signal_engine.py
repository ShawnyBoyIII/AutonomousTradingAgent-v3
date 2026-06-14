from __future__ import annotations

from datetime import datetime, timezone
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
    entry_price = float(latest["close"])

    if "low" in intraday_frame.columns:
        recent_lows = intraday_frame.tail(min(len(intraday_frame), 5))["low"]
        stop_loss = float(recent_lows.min())
    else:
        stop_loss = round(entry_price * 0.99, 4)

    if stop_loss >= entry_price:
        stop_loss = round(entry_price * 0.99, 4)

    risk = entry_price - stop_loss
    if risk <= 0:
        return None

    profit_target = round(entry_price + risk * 2.0, 4)
    confidence = 0.8
    if latest.get("volume_avg_5") and latest["volume"] > latest["volume_avg_5"] * 1.5:
        confidence = 0.9

    timestamp = intraday_frame.index[-1]
    if not isinstance(timestamp, datetime):
        timestamp = datetime.now(timezone.utc)

    return TradeSignal(
        ticker=symbol,
        timeframe="intraday",
        action="BUY",
        entry_price=round(entry_price, 4),
        stop_loss=round(stop_loss, 4),
        profit_target=profit_target,
        risk_reward_ratio=round((profit_target - entry_price) / risk, 6),
        confidence=confidence,
        reasons=["bullish daily regime", "intraday breakout"],
        strategy_tag="intraday-signal-engine",
        timestamp=timestamp,
    )
