"""Counter-thesis analysis: seeks evidence against a BUY signal's thesis.

A trade signal is a *thesis* (price will rise). This module is the skeptical
counterparty: it looks for the strongest disagreements (bearish RSI
divergence, exhaustion, regime mismatch, weak volume conviction, volatility
spikes, over-extension) and either vetoes the trade or scales the signal's
confidence down.

Design split (mirrors the V2.5 "pure logic, inject data" convention so tests
stay network-free):

- ``fetch_counter_thesis_context`` is the ONLY network-touching entry point.
  It pulls daily + intraday frames and computes the indicators the checks
  need, returning a :class:`CounterThesisContext`.
- ``build_counter_thesis_context`` is a pure builder over already-fetched
  frames (used by tests and by callers that already hold the data).
- ``evaluate_counter_thesis`` and every ``_check_*`` helper are pure
  functions of ``(context, settings)``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from trading_bot.config.settings import CounterThesisSettings, MarketDataSettings
from trading_bot.data.indicators import (
    add_atr,
    add_bollinger_bands,
    add_ema,
    add_rsi,
    add_sma,
)
from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.market_regime import (
    MarketRegime,
    RegimeMetrics,
    detect_market_regime,
)

if TYPE_CHECKING:
    import pandas as pd


SEVERITY_ORDER: tuple[str, ...] = ("none", "low", "medium", "high", "severe")
SEVERITY_WEIGHTS: dict[str, float] = {
    "none": 0.0,
    "low": 0.15,
    "medium": 0.35,
    "high": 0.60,
    "severe": 1.0,
}


def _severity_index(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return 0


@dataclass
class CounterThesisFinding:
    """A single piece of evidence against the BUY thesis."""

    check_name: str
    severity: str  # none | low | medium | high | severe
    description: str
    weight: float = 0.0  # 0.0-1.0 confidence penalty contributed


@dataclass
class CounterThesisContext:
    """Pure-data snapshot of what the counter-thesis checks inspect.

    Built once from frames (and a signal for its thesis tag) so that every
    ``_check_*`` function is a pure function of this object + settings.
    """

    symbol: str
    strategy_tag: str
    closes: list[float] = field(default_factory=list)
    rsi_series: list[float | None] = field(default_factory=list)
    latest_rsi: float | None = None
    volumes: list[float] = field(default_factory=list)
    latest_volume: float | None = None
    avg_volume: float | None = None
    bb_percent_b: float | None = None
    price_vs_ema20: float | None = None
    price_vs_sma50: float | None = None
    momentum_3: float | None = None
    volatility_percentile: float | None = None
    regime: MarketRegime | None = None
    regime_metrics: RegimeMetrics | None = None


@dataclass
class CounterThesisResult:
    """Aggregated counter-thesis outcome fed to the risk manager."""

    findings: list[CounterThesisFinding] = field(default_factory=list)
    overall_severity: str = "none"
    confidence_multiplier: float = 1.0  # 1.0 = full confidence kept, 0.0 = vetoed
    block_trade: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable summary for snapshots + decision log."""
        return {
            "findings": [
                {
                    "check": f.check_name,
                    "severity": f.severity,
                    "description": f.description,
                    "weight": round(f.weight, 3),
                }
                for f in self.findings
            ],
            "overall_severity": self.overall_severity,
            "confidence_multiplier": round(self.confidence_multiplier, 3),
            "block_trade": self.block_trade,
            "reasons": list(self.reasons),
        }


def fetch_counter_thesis_context(
    symbol: str,
    signal: TradeSignal,
    market_data_settings: MarketDataSettings,
    atr_period: int = 14,
) -> CounterThesisContext | None:
    """Fetch frames + compute indicators, returning a context (network I/O).

    Returns None if the required data cannot be fetched/validated so the
    orchestrator can skip counter-thesis rather than fail the whole scan.
    """
    from trading_bot.data import market_data

    try:
        daily_frame = market_data.fetch_bars(
            symbol,
            market_data_settings.daily_period,
            "1d",
        )
        intraday_frame = market_data.fetch_bars(
            symbol,
            market_data_settings.intraday_period,
            market_data_settings.intraday_interval,
        )
    except Exception:
        return None

    if daily_frame is None or intraday_frame is None:
        return None
    if daily_frame.empty or intraday_frame.empty:
        return None

    return build_counter_thesis_context(
        symbol=symbol,
        signal=signal,
        daily_frame=daily_frame,
        intraday_frame=intraday_frame,
        atr_period=atr_period,
    )


def build_counter_thesis_context(
    symbol: str,
    signal: TradeSignal,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
    atr_period: int = 14,
) -> CounterThesisContext | None:
    """Pure builder: computes indicators from frames and returns a context.

    Falls back gracefully (None values) when a column is missing rather than
    raising, so individual checks simply skip when their inputs are absent.
    """
    required = {"close", "high", "low"}
    if not required.issubset(daily_frame.columns):
        return None
    if not required.issubset(intraday_frame.columns):
        return None

    daily = add_ema(daily_frame, period=20, column_name="ema_20")
    daily = add_sma(daily, period=50, column_name="sma_50")
    daily = add_atr(daily, period=atr_period)
    daily = add_bollinger_bands(daily, period=20)

    intraday = add_rsi(intraday_frame, period=14)
    if "volume_avg_5" not in intraday.columns and "volume" in intraday.columns:
        intraday = intraday.copy(deep=True)
        intraday["volume_avg_5"] = intraday["volume"].rolling(5).mean()

    latest_daily = daily.iloc[-1]
    close = _to_finite_float(latest_daily.get("close"))
    ema20 = _to_finite_float(latest_daily.get("ema_20"))
    sma50 = _to_finite_float(latest_daily.get("sma_50"))
    price_vs_ema20 = ((close - ema20) / ema20 * 100.0) if (close and ema20) else None
    price_vs_sma50 = ((close - sma50) / sma50 * 100.0) if (close and sma50) else None

    regime, regime_metrics = detect_market_regime(daily)
    volatility_percentile = regime_metrics.volatility_percentile

    closes = [_to_finite_float(v) for v in intraday["close"].tolist()]
    closes = [c for c in closes if c is not None]
    rsi_series: list[float | None] = []
    if "rsi_14" in intraday.columns:
        rsi_series = [_to_finite_float(v) for v in intraday["rsi_14"].tolist()]
    latest_rsi = _last_finite(rsi_series)

    volumes: list[float] = []
    latest_volume = None
    avg_volume = None
    if "volume" in intraday.columns:
        volumes = [_to_finite_float(v) for v in intraday["volume"].tolist()]
        volumes = [v for v in volumes if v is not None]
        latest_volume = volumes[-1] if volumes else None
        avg_series = intraday.get("volume_avg_5")
        if avg_series is not None and len(avg_series) > 0:
            avg_volume = _to_finite_float(avg_series.iloc[-1])

    bb_percent_b = _to_finite_float(latest_daily.get("bb_percent_b"))
    momentum_3 = _compute_momentum_3(closes)

    return CounterThesisContext(
        symbol=symbol,
        strategy_tag=getattr(signal, "strategy_tag", ""),
        closes=closes,
        rsi_series=rsi_series,
        latest_rsi=latest_rsi,
        volumes=volumes,
        latest_volume=latest_volume,
        avg_volume=avg_volume,
        bb_percent_b=bb_percent_b,
        price_vs_ema20=price_vs_ema20,
        price_vs_sma50=price_vs_sma50,
        momentum_3=momentum_3,
        volatility_percentile=volatility_percentile,
        regime=regime,
        regime_metrics=regime_metrics,
    )


def evaluate_counter_thesis(
    context: CounterThesisContext | None,
    signal: TradeSignal | None,
    settings: CounterThesisSettings | None = None,
) -> CounterThesisResult:
    """Run all enabled checks and aggregate into a decision.

    A None context (data unavailable) yields an empty result that does not
    block: the orchestrator must keep trading when counter-thesis data is
    missing, otherwise a data outage becomes a silent kill switch.
    """
    cfg = settings or CounterThesisSettings()
    result = CounterThesisResult()

    if context is None:
        return result

    strategy_tag = (getattr(signal, "strategy_tag", "") or context.strategy_tag or "").lower()

    checks = (
        ("check_overbought", lambda: _check_overbought(context, cfg, strategy_tag)),
        ("check_volume_non_confirmation", lambda: _check_volume_non_confirmation(context, cfg)),
        ("check_rsi_divergence", lambda: _check_rsi_divergence(context, cfg)),
        ("check_resistance_proximity", lambda: _check_resistance_proximity(context, cfg)),
        ("check_regime_misalignment", lambda: _check_regime_misalignment(context, cfg)),
        ("check_waning_momentum", lambda: _check_waning_momentum(context, cfg)),
        ("check_volatility_spike", lambda: _check_volatility_spike(context, cfg)),
        ("check_extension", lambda: _check_extension(context, cfg)),
    )

    for toggle, check_fn in checks:
        if not getattr(cfg, toggle, True):
            continue
        finding = check_fn()
        if finding is not None and finding.severity != "none":
            result.findings.append(finding)

    if result.findings:
        result.overall_severity = max(
            (f.severity for f in result.findings),
            key=_severity_index,
        )
        total_penalty = sum(SEVERITY_WEIGHTS.get(f.severity, 0.0) for f in result.findings)
        result.confidence_multiplier = max(0.0, 1.0 - min(1.0, total_penalty))
        result.reasons = [f"{f.check_name}:{f.severity}" for f in result.findings]

    block_index = _severity_index(cfg.block_on_severity)
    severity_blocks = any(_severity_index(f.severity) >= block_index for f in result.findings)
    aggregate_blocks = (1.0 - result.confidence_multiplier) >= cfg.aggregate_block_threshold
    result.block_trade = bool(severity_blocks or aggregate_blocks)

    return result


def _check_overbought(
    context: CounterThesisContext,
    settings: CounterThesisSettings,
    strategy_tag: str,
) -> CounterThesisFinding | None:
    """RSI deep into overbought territory -> exhaustion risk against a long.

    Worse for trend/breakout/momentum theses (chasing) than for reversion
    setups where some strength is expected after the bounce.
    """
    if context.latest_rsi is None:
        return None
    if context.latest_rsi < settings.overbought_rsi_threshold:
        return None

    is_trend = any(tag in strategy_tag for tag in ("breakout", "momentum", "trend", "v3"))
    severity = "high" if is_trend else "medium"
    return CounterThesisFinding(
        check_name="overbought",
        severity=severity,
        description=f"RSI {context.latest_rsi:.1f} exceeds {settings.overbought_rsi_threshold:.0f} (exhaustion risk)",
        weight=SEVERITY_WEIGHTS[severity],
    )


def _check_volume_non_confirmation(
    context: CounterThesisContext,
    settings: CounterThesisSettings,
) -> CounterThesisFinding | None:
    """Price action without volume -> weak-conviction move."""
    if context.latest_volume is None or context.avg_volume is None:
        return None
    if context.avg_volume <= 0:
        return None
    ratio = context.latest_volume / context.avg_volume
    if ratio >= settings.volume_confirmation_floor:
        return None
    return CounterThesisFinding(
        check_name="volume_non_confirmation",
        severity="medium",
        description=f"volume ratio {ratio:.2f} below {settings.volume_confirmation_floor:.2f}",
        weight=SEVERITY_WEIGHTS["medium"],
    )


def _check_rsi_divergence(
    context: CounterThesisContext,
    settings: CounterThesisSettings,
) -> CounterThesisFinding | None:
    """Bearish divergence: price up while RSI down -> momentum dying."""
    closes = context.closes[-5:]
    rsi = context.rsi_series[-5:]
    aligned = [
        (c, r)
        for c, r in zip(closes, rsi)
        if c is not None and r is not None
    ]
    if len(aligned) < 3:
        return None
    first_close, first_rsi = aligned[0]
    last_close, last_rsi = aligned[-1]
    price_up = last_close > first_close
    rsi_down = last_rsi < first_rsi
    if price_up and rsi_down:
        return CounterThesisFinding(
            check_name="rsi_divergence",
            severity="high",
            description=(
                f"bearish divergence: price {first_close:.2f}->{last_close:.2f} "
                f"while RSI {first_rsi:.1f}->{last_rsi:.1f}"
            ),
            weight=SEVERITY_WEIGHTS["high"],
        )
    return None


def _check_resistance_proximity(
    context: CounterThesisContext,
    settings: CounterThesisSettings,
) -> CounterThesisFinding | None:
    """Price at/above upper Bollinger band -> mean-reversion risk."""
    if context.bb_percent_b is None:
        return None
    if context.bb_percent_b < settings.resistance_bb_percent_b:
        return None
    return CounterThesisFinding(
        check_name="resistance_proximity",
        severity="medium",
        description=f"price at upper band (%B={context.bb_percent_b:.1f})",
        weight=SEVERITY_WEIGHTS["medium"],
    )


def _check_regime_misalignment(
    context: CounterThesisContext,
    settings: CounterThesisSettings,
) -> CounterThesisFinding | None:
    """Buying into a downtrend or chaos regime -> thesis fights the tape."""
    if context.regime is None:
        return None
    if context.regime == MarketRegime.STRONG_DOWNTREND:
        return CounterThesisFinding(
            check_name="regime_misalignment",
            severity="severe",
            description=f"strong downtrend regime ({context.regime.value})",
            weight=SEVERITY_WEIGHTS["severe"],
        )
    if context.regime == MarketRegime.HIGH_VOLATILITY:
        return CounterThesisFinding(
            check_name="regime_misalignment",
            severity="medium",
            description=f"high-volatility regime ({context.regime.value})",
            weight=SEVERITY_WEIGHTS["medium"],
        )
    if context.regime == MarketRegime.WEAK_DOWNTREND:
        return CounterThesisFinding(
            check_name="regime_misalignment",
            severity="medium",
            description=f"weak downtrend regime ({context.regime.value})",
            weight=SEVERITY_WEIGHTS["medium"],
        )
    return None


def _check_waning_momentum(
    context: CounterThesisContext,
    settings: CounterThesisSettings,
) -> CounterThesisFinding | None:
    """3-bar momentum negative while BUY thesis expects upside."""
    if context.momentum_3 is None:
        return None
    if context.momentum_3 >= 0.0:
        return None
    return CounterThesisFinding(
        check_name="waning_momentum",
        severity="medium",
        description=f"3-bar momentum {context.momentum_3*100:.2f}% (price declining)",
        weight=SEVERITY_WEIGHTS["medium"],
    )


def _check_volatility_spike(
    context: CounterThesisContext,
    settings: CounterThesisSettings,
) -> CounterThesisFinding | None:
    """ATR in the top percentile -> whipsaw risk."""
    if context.volatility_percentile is None:
        return None
    if context.volatility_percentile < settings.volatility_percentile_threshold:
        return None
    return CounterThesisFinding(
        check_name="volatility_spike",
        severity="medium",
        description=(
            f"volatility percentile {context.volatility_percentile:.2f} "
            f">= {settings.volatility_percentile_threshold:.2f}"
        ),
        weight=SEVERITY_WEIGHTS["medium"],
    )


def _check_extension(
    context: CounterThesisContext,
    settings: CounterThesisSettings,
) -> CounterThesisFinding | None:
    """Price far above EMA20 -> over-extended, snap-back risk."""
    if context.price_vs_ema20 is None:
        return None
    if context.price_vs_ema20 <= settings.extension_pct:
        return None
    severity = "high" if context.price_vs_ema20 >= settings.extension_pct * 2 else "medium"
    return CounterThesisFinding(
        check_name="extension",
        severity=severity,
        description=f"price {context.price_vs_ema20:.2f}% above EMA20",
        weight=SEVERITY_WEIGHTS[severity],
    )


def _compute_momentum_3(closes: list[float]) -> float | None:
    if len(closes) < 3:
        return None
    old = closes[-3]
    new = closes[-1]
    if old <= 0:
        return None
    return (new - old) / old


def _to_finite_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _last_finite(series: list[float | None]) -> float | None:
    for value in reversed(series):
        if value is not None:
            return value
    return None
