from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
import math
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.market_regime import MarketRegime, detect_market_regime

if TYPE_CHECKING:
    import pandas as pd
    from trading_bot.config.settings import RiskSettings


@dataclass(frozen=True)
class TimeframeAlignment:
    daily: bool = False
    hourly: bool = False
    five_min: bool = False
    aligned_count: int = 0
    required_count: int = 2
    passed: bool = False
    reasons: list[str] = field(default_factory=list)
    regime: str = "unknown"

    def to_details(self) -> dict[str, object]:
        return {
            "mtf_daily": self.daily,
            "mtf_hourly": self.hourly,
            "mtf_5m": self.five_min,
            "mtf_aligned": self.aligned_count,
            "mtf_required": self.required_count,
            "mtf_passed": self.passed,
            "mtf_regime": self.regime,
            "mtf_reasons": ";".join(self.reasons),
        }


@dataclass(frozen=True)
class EntryTimingVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    preferred_window: bool = False
    volume_ratio: float | None = None
    range_ratio: float | None = None

    def to_details(self) -> dict[str, object]:
        details: dict[str, object] = {
            "entry_timing_passed": self.passed,
            "entry_preferred_window": self.preferred_window,
            "entry_reasons": ";".join(self.reasons),
        }
        if self.volume_ratio is not None:
            details["entry_volume_ratio"] = round(self.volume_ratio, 4)
        if self.range_ratio is not None:
            details["entry_range_ratio"] = round(self.range_ratio, 4)
        return details


@dataclass(frozen=True)
class SignalQualityVerdict:
    passed: bool
    alignment: TimeframeAlignment
    entry_timing: EntryTimingVerdict

    @property
    def reason(self) -> str:
        if self.passed:
            return "signal quality passed"
        reasons = self.alignment.reasons + self.entry_timing.reasons
        return "; ".join(reasons) or "signal quality failed"

    def to_details(self) -> dict[str, object]:
        return {
            **self.alignment.to_details(),
            **self.entry_timing.to_details(),
            "signal_quality_passed": self.passed,
        }


def evaluate_signal_quality(
    *,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
    hourly_frame: "pd.DataFrame | None" = None,
    signal: TradeSignal | None = None,
    setup_name: str | None = None,
    quality: str | None = None,
    required_count: int = 1,
) -> SignalQualityVerdict:
    """Evaluate Phase 1 entry gates without fetching data or mutating state."""
    alignment = evaluate_timeframe_alignment(
        daily_frame=daily_frame,
        hourly_frame=hourly_frame,
        intraday_frame=intraday_frame,
        required_count=required_count,
        setup_name=setup_name,
    )
    entry_timing = evaluate_entry_timing(
        intraday_frame=intraday_frame,
        signal=signal,
        setup_name=setup_name,
        quality=quality,
    )
    return SignalQualityVerdict(
        passed=alignment.passed and entry_timing.passed,
        alignment=alignment,
        entry_timing=entry_timing,
    )


def evaluate_timeframe_alignment(
    *,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
    hourly_frame: "pd.DataFrame | None" = None,
    required_count: int = 2,
    setup_name: str | None = None,
) -> TimeframeAlignment:
    regime = _safe_regime(daily_frame)
    daily = _daily_aligned(daily_frame, regime, setup_name)
    hourly = _hourly_aligned(hourly_frame)
    five_min = _five_min_aligned(intraday_frame)
    aligned_count = sum((daily, hourly, five_min))
    reasons: list[str] = []
    if daily:
        reasons.append("daily aligned")
    else:
        reasons.append("daily not aligned")
    if hourly_frame is None or hourly_frame.empty:
        reasons.append("hourly unavailable")
    elif hourly:
        reasons.append("hourly aligned")
    else:
        reasons.append("hourly not aligned")
    if five_min:
        reasons.append("5m aligned")
    else:
        reasons.append("5m not aligned")
    if aligned_count < required_count:
        reasons.append(f"requires {required_count} aligned timeframes")

    return TimeframeAlignment(
        daily=daily,
        hourly=hourly,
        five_min=five_min,
        aligned_count=aligned_count,
        required_count=required_count,
        passed=aligned_count >= required_count,
        reasons=reasons,
        regime=regime.value,
    )


def evaluate_entry_timing(
    *,
    intraday_frame: "pd.DataFrame",
    signal: TradeSignal | None = None,
    setup_name: str | None = None,
    quality: str | None = None,
) -> EntryTimingVerdict:
    reasons: list[str] = []
    latest = _latest_row(intraday_frame)
    timestamp = _signal_timestamp(signal, intraday_frame)
    if timestamp is not None and _is_avoid_time(timestamp):
        reasons.append("entry outside allowed intraday window")

    preferred = _is_preferred_time(timestamp) if timestamp is not None else False
    volume_ratio = _volume_ratio(latest)
    is_breakout = _is_breakout_setup(setup_name, signal)
    if is_breakout and (volume_ratio is None or volume_ratio < 1.2):
        reasons.append("breakout volume below 1.2x average")

    range_ratio = _latest_range_ratio(intraday_frame)
    if range_ratio is not None and range_ratio > 3.0:
        reasons.append("5m range exceeds 3x average range")

    if str(quality or getattr(signal, "quality", "")).upper() == "YELLOW":
        if not _has_two_bar_confirmation(intraday_frame):
            reasons.append("YELLOW signal lacks 2-bar confirmation")

    return EntryTimingVerdict(
        passed=not reasons,
        reasons=reasons or ["entry timing passed"],
        preferred_window=preferred,
        volume_ratio=volume_ratio,
        range_ratio=range_ratio,
    )


def adapt_signal_to_volatility_regime(
    signal: TradeSignal,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
    risk_settings: "RiskSettings",
) -> TradeSignal:
    """Return a signal with ATR stop/target geometry adapted to regime."""
    regime = _safe_regime(daily_frame)
    atr = _latest_atr(intraday_frame, getattr(risk_settings, "atr_period", 14))
    if atr is None or atr <= 0:
        return signal

    atr_multiplier, rr = _adaptive_stop_target(regime)
    entry = float(signal.entry_price)
    min_stop_distance_pct = float(getattr(risk_settings, "min_stop_distance_pct", 0.0) or 0.0)
    stop_distance = atr * atr_multiplier
    if min_stop_distance_pct > 0:
        stop_distance = max(stop_distance, entry * (min_stop_distance_pct / 100.0))

    stop_loss = round(entry - stop_distance, 4)
    if stop_loss <= 0 or stop_loss >= entry:
        return signal
    profit_target = round(entry + stop_distance * rr, 4)
    risk = entry - stop_loss
    if risk <= 0:
        return signal

    reasons = [
        reason
        for reason in signal.reasons
        if not str(reason).startswith("adaptive regime=")
    ]
    reasons.append(
        f"adaptive regime={regime.value} atr_mult={atr_multiplier:.2f} rr={rr:.2f}"
    )
    return TradeSignal(
        ticker=signal.ticker,
        timeframe=signal.timeframe,
        action=signal.action,
        entry_price=signal.entry_price,
        stop_loss=stop_loss,
        profit_target=profit_target,
        risk_reward_ratio=round((profit_target - entry) / risk, 6),
        confidence=signal.confidence,
        reasons=reasons,
        strategy_tag=signal.strategy_tag,
        timestamp=signal.timestamp,
        quality=signal.quality,
    )


def _adaptive_stop_target(regime: MarketRegime) -> tuple[float, float]:
    if regime == MarketRegime.STRONG_UPTREND:
        return 1.5, 1.5
    if regime == MarketRegime.WEAK_UPTREND:
        return 2.0, 1.5
    if regime == MarketRegime.HIGH_VOLATILITY:
        return 3.75, 2.5
    if regime == MarketRegime.RANGE_BOUND:
        return 3.0, 1.3
    return 3.0, 2.0


def _daily_aligned(frame: "pd.DataFrame", regime: MarketRegime, setup_name: str | None) -> bool:
    if _is_mean_reversion_setup(setup_name):
        return regime in {
            MarketRegime.RANGE_BOUND,
            MarketRegime.WEAK_DOWNTREND,
            MarketRegime.HIGH_VOLATILITY,
        }
    latest = _latest_row(frame)
    close = _finite_float(latest.get("close"))
    ema20 = _finite_float(latest.get("ema_20"))
    sma50 = _finite_float(latest.get("sma_50"))
    if close is not None and ema20 is not None and sma50 is not None:
        if close > ema20 > sma50:
            return True
    return regime in {MarketRegime.STRONG_UPTREND, MarketRegime.WEAK_UPTREND}


def _is_mean_reversion_setup(setup_name: str | None) -> bool:
    token = str(setup_name or "").lower()
    return any(value in token for value in ("mean", "reversion", "oversold", "vwap", "range"))


def _hourly_aligned(frame: "pd.DataFrame | None") -> bool:
    if frame is None or len(frame) < 3:
        return False
    latest = frame.iloc[-1]
    close = _finite_float(latest.get("close"))
    ema20 = _finite_float(latest.get("ema_20"))
    sma50 = _finite_float(latest.get("sma_50"))
    previous_close = _finite_float(frame.iloc[-3].get("close"))
    if close is None or previous_close is None:
        return False
    momentum_ok = close > previous_close
    if ema20 is not None and sma50 is not None:
        return bool(close > ema20 and ema20 >= sma50 and momentum_ok)
    return momentum_ok


def _five_min_aligned(frame: "pd.DataFrame") -> bool:
    if len(frame) < 2:
        return False
    latest = _finite_float(frame.iloc[-1].get("close"))
    previous = _finite_float(frame.iloc[-2].get("close"))
    if latest is None or previous is None:
        return False
    return latest >= previous


def _safe_regime(frame: "pd.DataFrame") -> MarketRegime:
    try:
        return detect_market_regime(frame)[0]
    except Exception:
        return MarketRegime.RANGE_BOUND


def _signal_timestamp(signal: TradeSignal | None, frame: "pd.DataFrame") -> datetime | None:
    frame_timestamp: datetime | None = None
    if not frame.empty:
        if "timestamp" in frame.columns:
            candidate = frame.iloc[-1].get("timestamp")
        else:
            candidate = frame.index[-1]
        frame_timestamp = candidate if isinstance(candidate, datetime) else None
    if signal is not None and frame_timestamp is not None:
        signal_wall_time = signal.timestamp
        if signal_wall_time.tzinfo is not None:
            signal_wall_time = signal_wall_time.replace(tzinfo=None)
        frame_wall_time = frame_timestamp
        if frame_wall_time.tzinfo is not None:
            frame_wall_time = frame_wall_time.replace(tzinfo=None)
        if frame_wall_time == signal_wall_time:
            return frame_timestamp
    elif frame_timestamp is not None:
        return frame_timestamp
    if signal is not None:
        return signal.timestamp
    return None


def _is_avoid_time(timestamp: datetime) -> bool:
    local = _to_eastern_time(timestamp)
    return (
        time(9, 30) <= local < time(9, 45)
        or time(15, 45) <= local < time(16, 0)
        or local < time(9, 30)
        or local >= time(16, 0)
    )


def _is_preferred_time(timestamp: datetime) -> bool:
    local = _to_eastern_time(timestamp)
    return time(9, 45) <= local <= time(11, 30) or time(14, 0) <= local <= time(15, 30)


def _to_eastern_time(timestamp: datetime) -> time:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("America/New_York"))
    return timestamp.astimezone(ZoneInfo("America/New_York")).time()


def _volume_ratio(row) -> float | None:
    volume = _finite_float(row.get("volume"))
    average = _finite_float(row.get("volume_avg_5"))
    if volume is None or average is None or average <= 0:
        return None
    return volume / average


def _latest_range_ratio(frame: "pd.DataFrame") -> float | None:
    if len(frame) < 6 or not {"high", "low"}.issubset(frame.columns):
        return None
    latest = frame.iloc[-1]
    latest_high = _finite_float(latest.get("high"))
    latest_low = _finite_float(latest.get("low"))
    if latest_high is None or latest_low is None:
        return None
    latest_range = latest_high - latest_low
    prior_ranges = []
    for row in frame.iloc[:-1].tail(20).itertuples(index=False):
        high = _finite_float(getattr(row, "high", None))
        low = _finite_float(getattr(row, "low", None))
        if high is not None and low is not None and high > low:
            prior_ranges.append(high - low)
    if not prior_ranges:
        return None
    average_range = sum(prior_ranges) / len(prior_ranges)
    if average_range <= 0:
        return None
    return latest_range / average_range


def _has_two_bar_confirmation(frame: "pd.DataFrame") -> bool:
    if len(frame) < 3:
        return False
    closes = [_finite_float(value) for value in frame["close"].tail(3).tolist()]
    if any(value is None for value in closes):
        return False
    first, second, third = [value for value in closes if value is not None]
    return first <= second <= third


def _latest_atr(frame: "pd.DataFrame", period: int) -> float | None:
    if frame.empty:
        return None
    for column in (f"atr_{period}", "atr_14"):
        if column in frame.columns:
            return _finite_float(frame.iloc[-1].get(column))
    return None


def _is_breakout_setup(setup_name: str | None, signal: TradeSignal | None) -> bool:
    tokens = [setup_name or ""]
    if signal is not None:
        tokens.extend(signal.reasons)
    return any("breakout" in str(token).lower() for token in tokens)


def _latest_row(frame: "pd.DataFrame"):
    if frame.empty:
        return {}
    return frame.iloc[-1]


def _finite_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None
