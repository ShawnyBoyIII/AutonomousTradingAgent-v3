"""Market-wide stock screener for dynamic symbol discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


@dataclass
class ScreenResult:
    """Result of screening a single symbol."""

    symbol: str
    passed: bool
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


@dataclass
class MarketScreen:
    """Complete market screening results."""

    total_screened: int = 0
    passed: list[ScreenResult] = field(default_factory=list)
    failed: list[ScreenResult] = field(default_factory=list)
    top_picks: list[ScreenResult] = field(default_factory=list)


class MarketScreener:
    """Screen entire market for trading candidates.

    Uses technical and volume criteria to find symbols with
    high-probability setups.
    """

    # Universe of stocks to screen. Refreshed from current S&P 500
    # constituents (verified against yfinance on 2026-06-23) to avoid
    # delisted/renamed tickers (e.g. "SQ" which yfinance can no longer
    # resolve). Keep liquid large-caps so discovery has a stable candidate
    # pool even when smaller setups fail. Mega-caps (MSFT/META/NVDA/NFLX/
    # TSLA/etc.) are included unconditionally even if not in S&P 500 set.
    DEFAULT_UNIVERSE = [
        "A", "AAPL", "ABBV", "ABNB", "ABT", "ACGL", "ACN", "ADBE", "ADI",
        "ADM", "ADP", "ADSK", "AEE", "AEP", "AES", "AFL", "AIG", "AIZ",
        "AJG", "AKAM", "ALB", "ALGN", "ALL", "ALLE", "AMAT", "AMCR", "AMD",
        "AME", "AMGN", "AMP", "AMT", "AMZN", "ANET", "AON", "AOS", "APA",
        "APD", "APH", "APO", "APP", "APTV", "ARE", "ARES", "ATO", "AVB",
        "AVGO", "AVY", "AWK", "AXON", "AXP", "AZO", "BA", "BAC", "BALL",
        "BAX", "BBY", "BDX", "BF-B", "BG", "BIIB", "BKNG", "BKR", "BLDR",
        "BLK", "BMY", "BNY", "BR", "BRK-B", "BRO", "BSX", "BX", "BXP",
        "C", "CAG", "CAH", "CARR", "CASY", "CAT", "CB", "CBOE", "CBRE",
        "CCI", "CCL", "CDNS", "CDW", "CEG", "CF", "CFG", "CHD", "CHRW",
        "CHTR", "CI", "CIEN", "CINF", "CL", "CLX", "CMCSA", "CME", "CMG",
        "CMI", "CMS", "CNC", "CNP", "COF", "COHR", "COIN", "COO", "COP",
        "COR", "COST", "CPAY", "CPRT", "CPT", "CRL", "CRM", "CSCO", "CTAS",
        "CTSH", "CVNA", "CVX", "ED", "FIX", "GLW", "GOOG", "GOOGL", "HD",
        "JNJ", "JPM", "KO", "LNT", "MA", "META", "MMM", "MO", "MSFT",
        "NFLX", "NVDA", "PEP", "PG", "PLTR", "SCHW", "SNOW", "STZ", "T",
        "TECH", "TSLA", "UBER", "UNH", "V", "WMT", "XOM", "XYZ",
        # ETFs (kept from previous list for sector/hedging coverage)
        "SPY", "QQQ", "IWM", "SQQQ", "TQQQ", "SOXL", "FNGU",
    ]

    def __init__(
        self,
        min_price: float = 5.0,
        max_price: float = 1000.0,
        min_volume: int = 1_000_000,  # 1M average volume
        min_adx: float = 20.0,
        require_green_candle: bool = True,
    ) -> None:
        self.min_price = min_price
        self.max_price = max_price
        self.min_volume = min_volume
        self.min_adx = min_adx
        self.require_green_candle = require_green_candle

    def screen_symbol(
        self,
        symbol: str,
        daily_frame: "pd.DataFrame",
    ) -> ScreenResult:
        """Screen a single symbol.

        Returns ScreenResult with pass/fail and scoring.
        """
        result = ScreenResult(symbol=symbol, passed=False)

        # Check 1: Minimum bars
        if len(daily_frame) < 20:
            result.reasons.append("Insufficient history (< 20 bars)")
            return result

        # Check 2: Price range
        latest_close = float(daily_frame["close"].iloc[-1])
        if latest_close < self.min_price:
            result.reasons.append(f"Price ${latest_close:.2f} below minimum ${self.min_price}")
            return result
        if latest_close > self.max_price:
            result.reasons.append(f"Price ${latest_close:.2f} above maximum ${self.max_price}")
            return result

        # Check 3: Volume
        avg_volume = daily_frame["volume"].tail(20).mean()
        if avg_volume < self.min_volume:
            result.reasons.append(f"Volume {avg_volume:,.0f} below minimum {self.min_volume:,.0f}")
            return result

        # Check 4: Trend strength (ADX)
        if "atr_14" in daily_frame.columns:
            # Approximate ADX calculation or use ATR as proxy for volatility
            atr = float(daily_frame["atr_14"].iloc[-1])
            atr_pct = (atr / latest_close) * 100
            if atr_pct < 1.0:  # Too low volatility
                result.reasons.append(f"Volatility too low ({atr_pct:.2f}%)")
                return result

        # Check 5: Green candle (optional)
        if self.require_green_candle:
            latest_open = float(daily_frame["open"].iloc[-1])
            if latest_close <= latest_open:
                result.reasons.append("Not a green candle")
                return result

        # Check 6: Above key moving averages
        if "ema_20" in daily_frame.columns and "sma_50" in daily_frame.columns:
            ema20 = float(daily_frame["ema_20"].iloc[-1])
            sma50 = float(daily_frame["sma_50"].iloc[-1])

            if latest_close < ema20:
                result.reasons.append("Price below EMA20")
                return result

            # Score based on trend strength
            trend_score = 0
            if latest_close > ema20:
                trend_score += 20
            if ema20 > sma50:
                trend_score += 20
            if latest_close > sma50:
                trend_score += 10

            result.score += trend_score

        # Check 7: Volume surge
        latest_volume = float(daily_frame["volume"].iloc[-1])
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 0
        if volume_ratio > 1.5:
            result.score += min(30, volume_ratio * 10)
            result.metrics["volume_surge"] = volume_ratio

        # Check 8: Recent momentum
        if len(daily_frame) >= 5:
            price_5d = float(daily_frame["close"].iloc[-5])
            momentum = (latest_close - price_5d) / price_5d * 100
            if momentum > 0:
                result.score += min(20, momentum * 2)
                result.metrics["momentum_5d"] = momentum

        # Passed all screens
        result.passed = True
        result.reasons.append(f"Score: {result.score:.1f}")

        return result

    def screen_universe(
        self,
        data_provider: callable,
        universe: list[str] | None = None,
    ) -> MarketScreen:
        """Screen entire universe of symbols.

        Args:
            data_provider: Function that takes symbol and returns DataFrame
            universe: List of symbols to screen (uses DEFAULT_UNIVERSE if None)

        Returns:
            MarketScreen with all results
        """
        universe = universe or self.DEFAULT_UNIVERSE
        screen = MarketScreen(total_screened=len(universe))

        for symbol in universe:
            try:
                frame = data_provider(symbol)
                if frame is None or len(frame) == 0:
                    continue

                result = self.screen_symbol(symbol, frame)

                if result.passed:
                    screen.passed.append(result)
                else:
                    screen.failed.append(result)

            except Exception as e:
                # Log error but continue screening
                continue

        # Sort passed by score and get top picks
        screen.passed.sort(key=lambda x: x.score, reverse=True)
        screen.top_picks = screen.passed[:10]

        return screen


def find_gap_up_symbols(
    premarket_data: dict[str, "pd.DataFrame"],
    min_gap_pct: float = 2.0,
    min_premarket_volume: int = 100_000,
) -> list[dict]:
    """Find symbols gapping up in pre-market.

    Args:
        premarket_data: Dict of symbol -> premarket DataFrame
        min_gap_pct: Minimum gap percentage
        min_premarket_volume: Minimum premarket volume

    Returns:
        List of gap up candidates with details
    """
    gaps = []

    for symbol, frame in premarket_data.items():
        if len(frame) < 2:
            continue

        try:
            # Get previous close (from daily data context)
            # Simplified: use first premarket price as proxy
            premarket_open = float(frame["open"].iloc[0])
            premarket_high = float(frame["high"].max())
            premarket_volume = int(frame["volume"].sum())

            if premarket_volume < min_premarket_volume:
                continue

            # We'd need previous close for accurate gap calc
            # For now, use high vs open
            move_pct = (premarket_high - premarket_open) / premarket_open * 100

            if move_pct >= min_gap_pct:
                gaps.append({
                    "symbol": symbol,
                    "gap_pct": round(move_pct, 2),
                    "premarket_volume": premarket_volume,
                    "high": premarket_high,
                    "open": premarket_open,
                })
        except Exception:
            continue

    # Sort by gap percentage
    gaps.sort(key=lambda x: x["gap_pct"], reverse=True)
    return gaps


def screen_for_breakout_setups(
    symbols_data: dict[str, "pd.DataFrame"],
    lookback_days: int = 20,
) -> list[ScreenResult]:
    """Screen for breakout setups (near 20-day high).

    Args:
        symbols_data: Dict of symbol -> daily DataFrame
        lookback_days: Days to look back for highs

    Returns:
        List of breakout candidates
    """
    breakouts = []

    for symbol, frame in symbols_data.items():
        if len(frame) < lookback_days + 5:
            continue

        try:
            recent_highs = frame["high"].tail(lookback_days).tolist()
            range_high = max(recent_highs[:-1])  # Exclude today
            latest_close = float(frame["close"].iloc[-1])
            latest_high = float(frame["high"].iloc[-1])

            # Breakout = close within 1% of range high
            if latest_close >= range_high * 0.99:
                volume_avg = frame["volume"].tail(20).mean()
                volume_today = float(frame["volume"].iloc[-1])
                volume_ratio = volume_today / volume_avg if volume_avg > 0 else 0

                result = ScreenResult(
                    symbol=symbol,
                    passed=True,
                    score=50 + (volume_ratio * 10),
                    reasons=[f"Near {lookback_days}-day high", f"Volume ratio: {volume_ratio:.2f}"],
                    metrics={
                        "range_high": range_high,
                        "latest_close": latest_close,
                        "volume_ratio": volume_ratio,
                    },
                )
                breakouts.append(result)

        except Exception:
            continue

    breakouts.sort(key=lambda x: x.score, reverse=True)
    return breakouts


def screen_for_mean_reversion(
    symbols_data: dict[str, "pd.DataFrame"],
) -> list[ScreenResult]:
    """Screen for oversold mean reversion setups.

    Looks for:
    - Price near lower Bollinger Band
    - RSI oversold but turning up
    - High volume (capitulation)
    """
    oversold = []

    for symbol, frame in symbols_data.items():
        if len(frame) < 20:
            continue

        try:
            latest = frame.iloc[-1]
            prev = frame.iloc[-2]

            close = float(latest["close"])
            bb_lower = float(latest.get("bb_lower", 0))
            bb_upper = float(latest.get("bb_upper", close * 1.1))
            rsi = float(latest.get("rsi_14", 50))
            prev_rsi = float(prev.get("rsi_14", 50))

            if bb_lower <= 0:
                continue

            # Criteria
            near_lower_band = close <= bb_lower * 1.02
            rsi_oversold = rsi < 35
            rsi_turning = rsi > prev_rsi  # RSI increasing
            volume_high = float(latest["volume"]) > frame["volume"].tail(20).mean() * 1.2

            if near_lower_band and rsi_oversold and rsi_turning:
                score = 50
                if volume_high:
                    score += 20

                result = ScreenResult(
                    symbol=symbol,
                    passed=True,
                    score=score,
                    reasons=[
                        f"Near lower BBand",
                        f"RSI: {rsi:.1f} (turning up)" if rsi_turning else f"RSI: {rsi:.1f}",
                        "High volume" if volume_high else "Normal volume",
                    ],
                    metrics={
                        "rsi": rsi,
                        "bb_percent": (close - bb_lower) / (bb_upper - bb_lower) * 100,
                    },
                )
                oversold.append(result)

        except Exception:
            continue

    oversold.sort(key=lambda x: x.score, reverse=True)
    return oversold
