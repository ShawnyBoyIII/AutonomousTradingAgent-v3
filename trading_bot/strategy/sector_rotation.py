"""Sector rotation detection and relative strength analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from trading_bot.data.indicators import add_rsi, add_sma


@dataclass
class SectorMetrics:
    """Metrics for a single sector."""

    symbol: str
    name: str
    price_change_1d: float = 0.0
    price_change_5d: float = 0.0
    price_change_20d: float = 0.0
    relative_strength: float = 0.0  # vs SPY
    momentum_score: float = 0.0
    rank: int = 0


@dataclass
class SectorRotationAnalysis:
    """Complete sector rotation analysis."""

    sectors: list[SectorMetrics] = field(default_factory=list)
    leading_sectors: list[str] = field(default_factory=list)
    lagging_sectors: list[str] = field(default_factory=list)
    rotation_detected: bool = False
    risk_on: bool = False  # True = growth/cyclical leading, False = defensive leading


# Major sector ETFs
SECTOR_ETFS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLP": "Consumer Staples",
    "XLY": "Consumer Discretionary",
    "XLB": "Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLV": "Health Care",
    "XLC": "Communication Services",
}


def analyze_sector_rotation(
    sector_data: dict[str, "pd.DataFrame"],
    spy_data: "pd.DataFrame" | None = None,
) -> SectorRotationAnalysis:
    """Analyze sector rotation and relative strength.

    Args:
        sector_data: Dict of sector ETF symbol -> price DataFrame
        spy_data: SPY DataFrame for relative strength comparison

    Returns:
        SectorRotationAnalysis with rankings and signals
    """
    sectors = []

    for symbol, frame in sector_data.items():
        if len(frame) < 20:
            continue

        metrics = _calculate_sector_metrics(symbol, frame, spy_data)
        if metrics:
            sectors.append(metrics)

    if not sectors:
        return SectorRotationAnalysis()

    # Rank sectors by momentum score
    sectors.sort(key=lambda x: x.momentum_score, reverse=True)
    for i, sector in enumerate(sectors):
        sector.rank = i + 1

    # Identify leading and lagging
    leading = [s.symbol for s in sectors[:3]]
    lagging = [s.symbol for s in sectors[-3:]]

    # Detect rotation (risk-on vs risk-off)
    risk_on_sectors = {"XLK", "XLY", "XLI", "XLB"}  # Growth/cyclical
    risk_off_sectors = {"XLP", "XLU", "XLRE", "XLV"}  # Defensive

    leading_set = set(leading)
    risk_on_score = len(leading_set & risk_on_sectors)
    risk_off_score = len(leading_set & risk_off_sectors)

    risk_on = risk_on_score > risk_off_score

    # Detect rotation (if defensive was leading yesterday but growth today)
    rotation_detected = False  # Would need historical comparison

    return SectorRotationAnalysis(
        sectors=sectors,
        leading_sectors=leading,
        lagging_sectors=lagging,
        rotation_detected=rotation_detected,
        risk_on=risk_on,
    )


def _calculate_sector_metrics(
    symbol: str,
    frame: "pd.DataFrame",
    spy_data: "pd.DataFrame" | None,
) -> SectorMetrics | None:
    """Calculate metrics for a single sector."""
    if len(frame) < 20 or "close" not in frame.columns:
        return None

    closes = frame["close"].tolist()

    # Calculate returns
    current = closes[-1]
    change_1d = (current - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
    change_5d = (current - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
    change_20d = (current - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0

    # Calculate relative strength vs SPY
    relative_strength = 0.0
    if spy_data is not None and len(spy_data) >= 20:
        spy_closes = spy_data["close"].tolist()
        spy_current = spy_closes[-1]
        spy_20d = spy_closes[-20]
        spy_return = (spy_current - spy_20d) / spy_20d * 100

        if spy_return != 0:
            relative_strength = change_20d - spy_return

    # Momentum score (weighted combination)
    momentum = (
        change_1d * 0.3 +  # 30% weight on 1-day
        change_5d * 0.3 +  # 30% weight on 5-day
        change_20d * 0.2 +  # 20% weight on 20-day
        relative_strength * 0.2  # 20% weight on relative strength
    )

    return SectorMetrics(
        symbol=symbol,
        name=SECTOR_ETFS.get(symbol, symbol),
        price_change_1d=change_1d,
        price_change_5d=change_5d,
        price_change_20d=change_20d,
        relative_strength=relative_strength,
        momentum_score=momentum,
    )


def get_best_sectors_for_trading(
    analysis: SectorRotationAnalysis,
    top_n: int = 3,
) -> list[str]:
    """Get top N sectors for trading.

    Returns sector ETF symbols that are leading.
    """
    return [s.symbol for s in analysis.sectors[:top_n]]


def should_trade_symbol_in_sector(
    symbol: str,
    sector: str,
    analysis: SectorRotationAnalysis,
) -> tuple[bool, str]:
    """Check if a symbol should be traded based on sector strength.

    Args:
        symbol: Stock symbol
        sector: Sector ETF symbol for the stock
        analysis: Sector rotation analysis

    Returns:
        (should_trade, reason)
    """
    # Find sector in analysis
    sector_metric = None
    for s in analysis.sectors:
        if s.symbol == sector:
            sector_metric = s
            break

    if not sector_metric:
        return False, f"Sector {sector} not in analysis"

    # Only trade if sector is in top 5
    if sector_metric.rank > 5:
        return False, f"Sector {sector} rank {sector_metric.rank} (not in top 5)"

    # Only trade if sector has positive momentum
    if sector_metric.momentum_score < 0:
        return False, f"Sector {sector} momentum {sector_metric.momentum_score:.2f} (negative)"

    return True, f"Sector {sector} rank {sector_metric.rank}, momentum {sector_metric.momentum_score:.2f}"


def filter_symbols_by_sector_strength(
    symbols_with_sectors: dict[str, str],
    sector_analysis: SectorRotationAnalysis,
    min_rank: int = 5,
) -> dict[str, str]:
    """Filter symbols to only those in strong sectors.

    Args:
        symbols_with_sectors: Dict of symbol -> sector ETF
        sector_analysis: Sector rotation analysis
        min_rank: Minimum sector rank to include

    Returns:
        Filtered dict of symbols in strong sectors
    """
    filtered = {}

    for symbol, sector in symbols_with_sectors.items():
        should_trade, _ = should_trade_symbol_in_sector(symbol, sector, sector_analysis)
        if should_trade:
            filtered[symbol] = sector

    return filtered
