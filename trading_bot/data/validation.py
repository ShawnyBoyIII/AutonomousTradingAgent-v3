from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


@dataclass
class ValidationResult:
    """Result of data validation check."""

    valid: bool
    reason: str | None = None


def validate_price_sanity(
    frame: "pd.DataFrame",
    max_price_jump_pct: float = 1000.0,  # 10x = 1000%
) -> ValidationResult:
    """Validate that prices are within reasonable bounds.

    Checks:
    1. All prices > 0 (no negative or zero prices)
    2. Price jump from previous bar < max_price_jump_pct

    Args:
        frame: DataFrame with price data
        max_price_jump_pct: Maximum allowed price jump percentage

    Returns:
        ValidationResult with valid=True if prices are sane
    """
    if frame.empty:
        return ValidationResult(valid=False, reason="empty frame")

    # Check required columns
    if "close" not in frame.columns:
        return ValidationResult(valid=False, reason="missing close column")

    closes = frame["close"].dropna()
    if len(closes) == 0:
        return ValidationResult(valid=False, reason="no valid close prices")

    # Check 1: All prices > 0
    if (closes <= 0).any():
        invalid_count = (closes <= 0).sum()
        return ValidationResult(
            valid=False,
            reason=f"{invalid_count} non-positive prices found"
        )

    # Check 2: Price jump not excessive
    if len(closes) >= 2:
        prev_close = closes.iloc[-2]
        last_close = closes.iloc[-1]

        if prev_close > 0:
            price_change_pct = abs((last_close - prev_close) / prev_close) * 100
            if price_change_pct > max_price_jump_pct:
                return ValidationResult(
                    valid=False,
                    reason=f"price jump of {price_change_pct:.1f}% exceeds limit of {max_price_jump_pct}%"
                )

    return ValidationResult(valid=True)


def validate_ohlc_coherence(frame: "pd.DataFrame") -> ValidationResult:
    """Validate OHLC bar coherence.

    Checks:
    1. high >= low for all bars
    2. high >= close >= low for all bars
    3. high >= open >= low for all bars (if open column exists)
    4. No NaN values in required columns

    Args:
        frame: DataFrame with OHLC data

    Returns:
        ValidationResult with valid=True if OHLC is coherent
    """
    if frame.empty:
        return ValidationResult(valid=False, reason="empty frame")

    required = {"high", "low", "close"}
    if not required.issubset(frame.columns):
        missing = required - set(frame.columns)
        return ValidationResult(valid=False, reason=f"missing columns: {missing}")

    # Check for NaN values
    for col in required:
        if frame[col].isna().any():
            nan_count = frame[col].isna().sum()
            return ValidationResult(
                valid=False,
                reason=f"{nan_count} NaN values in {col}"
            )

    # Check high >= low
    invalid_hl = frame[frame["high"] < frame["low"]]
    if len(invalid_hl) > 0:
        return ValidationResult(
            valid=False,
            reason=f"{len(invalid_hl)} bars where high < low"
        )

    # Check high >= close >= low
    invalid_close_high = frame[frame["close"] > frame["high"]]
    invalid_close_low = frame[frame["close"] < frame["low"]]
    if len(invalid_close_high) > 0:
        return ValidationResult(
            valid=False,
            reason=f"{len(invalid_close_high)} bars where close > high"
        )
    if len(invalid_close_low) > 0:
        return ValidationResult(
            valid=False,
            reason=f"{len(invalid_close_low)} bars where close < low"
        )

    # Check high >= open >= low (if open exists)
    if "open" in frame.columns:
        invalid_open_high = frame[frame["open"] > frame["high"]]
        invalid_open_low = frame[frame["open"] < frame["low"]]
        if len(invalid_open_high) > 0:
            return ValidationResult(
                valid=False,
                reason=f"{len(invalid_open_high)} bars where open > high"
            )
        if len(invalid_open_low) > 0:
            return ValidationResult(
                valid=False,
                reason=f"{len(invalid_open_low)} bars where open < low"
            )

    return ValidationResult(valid=True)


def validate_volume_sanity(
    frame: "pd.DataFrame",
    max_volume_jump_pct: float = 1000.0,
) -> ValidationResult:
    """Validate volume data sanity.

    Checks:
    1. Volume >= 0 (no negative volume)
    2. Volume jump from previous bar < max_volume_jump_pct

    Args:
        frame: DataFrame with volume data
        max_volume_jump_pct: Maximum allowed volume jump percentage

    Returns:
        ValidationResult with valid=True if volume is sane
    """
    if "volume" not in frame.columns:
        # Volume column optional, so no volume is valid
        return ValidationResult(valid=True)

    volumes = frame["volume"].dropna()
    if len(volumes) == 0:
        return ValidationResult(valid=True)  # No volume data is OK

    # Check 1: Volume >= 0
    if (volumes < 0).any():
        invalid_count = (volumes < 0).sum()
        return ValidationResult(
            valid=False,
            reason=f"{invalid_count} negative volume values"
        )

    # Check 2: Volume jump not excessive
    if len(volumes) >= 2:
        prev_vol = volumes.iloc[-2]
        last_vol = volumes.iloc[-1]

        if prev_vol > 0:
            vol_change_pct = abs((last_vol - prev_vol) / prev_vol) * 100
            if vol_change_pct > max_volume_jump_pct:
                return ValidationResult(
                    valid=False,
                    reason=f"volume jump of {vol_change_pct:.1f}% exceeds limit"
                )

    return ValidationResult(valid=True)


def validate_market_data(
    frame: "pd.DataFrame",
    max_price_jump_pct: float = 1000.0,
    max_volume_jump_pct: float = 1000.0,
    min_bars: int = 1,
) -> ValidationResult:
    """Comprehensive market data validation.

    Runs all validation checks:
    - Price sanity
    - OHLC coherence
    - Volume sanity
    - Minimum bar count

    Args:
        frame: DataFrame with market data
        max_price_jump_pct: Maximum allowed price jump percentage
        max_volume_jump_pct: Maximum allowed volume jump percentage
        min_bars: Minimum number of bars required

    Returns:
        ValidationResult with valid=True if all checks pass
    """
    # Check minimum bars
    if len(frame) < min_bars:
        return ValidationResult(
            valid=False,
            reason=f"insufficient bars: {len(frame)} < {min_bars}"
        )

    # Check price sanity
    price_result = validate_price_sanity(frame, max_price_jump_pct)
    if not price_result.valid:
        return price_result

    # Check OHLC coherence (if OHLC columns present)
    if {"high", "low", "close"}.issubset(frame.columns):
        ohlc_result = validate_ohlc_coherence(frame)
        if not ohlc_result.valid:
            return ohlc_result

    # Check volume sanity
    vol_result = validate_volume_sanity(frame, max_volume_jump_pct)
    if not vol_result.valid:
        return vol_result

    return ValidationResult(valid=True)
