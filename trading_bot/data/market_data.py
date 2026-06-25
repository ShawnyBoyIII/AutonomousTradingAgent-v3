from __future__ import annotations

import logging

import pandas as pd

from trading_bot.config.settings import MarketDataSettings
from trading_bot.data.validation import ValidationResult, validate_market_data

logger = logging.getLogger(__name__)


def normalize_ohlcv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    renamed = frame.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    ).reset_index(names="timestamp")
    return renamed[["timestamp", "open", "high", "low", "close", "volume"]]


def _resolve_provider(settings: MarketDataSettings | None = None):
    """Return the appropriate provider instance based on *settings*.

    * ``"yfinance"``  → ``YFinanceProvider``
    * ``"alpaca"``    → ``AlpacaProvider``
    * omitted / other → ``YFinanceProvider`` (safe default)
    """
    provider_name = settings.provider if settings is not None else ""
    if provider_name == "alpaca":
        from trading_bot.data.providers.alpaca_provider import AlpacaProvider

        return AlpacaProvider()
    from trading_bot.data.providers.yfinance_provider import YFinanceProvider

    return YFinanceProvider()


def _fallback_fetch(
    symbol: str,
    period: str,
    interval: str,
    start: str | None = None,
    end: str | None = None,
    primary_settings: MarketDataSettings | None = None,
) -> pd.DataFrame:
    """Try the configured provider first; fall back to yfinance on failure."""
    primary = _resolve_provider(primary_settings)
    try:
        return primary.fetch_bars(symbol, period, interval, start=start, end=end)
    except (ValueError, ConnectionError, OSError, TimeoutError) as exc:
        logger.warning(
            f"fetch_failed symbol={symbol} provider={getattr(primary_settings, 'provider', 'yfinance')} error={exc} "
            f"falling back to yfinance"
        )
    from trading_bot.data.providers.yfinance_provider import YFinanceProvider

    return YFinanceProvider().fetch_bars(symbol, period, interval, start=start, end=end)


def fetch_bars(
    symbol: str,
    period: str,
    interval: str,
    start: str | None = None,
    end: str | None = None,
    settings: MarketDataSettings | None = None,
) -> pd.DataFrame:
    return _fallback_fetch(symbol, period, interval, start=start, end=end, primary_settings=settings)


def fetch_small_cap_candidates(
    limit: int = 200,
    screeners: list[str] | None = None,
    settings: MarketDataSettings | None = None,
) -> list[dict[str, object]]:
    # Screener always uses yfinance — Alpaca doesn't offer one
    from trading_bot.data.providers.yfinance_provider import YFinanceProvider

    return YFinanceProvider().fetch_small_cap_candidates(limit=limit, screeners=screeners)


def fetch_and_validate_bars(
    symbol: str,
    period: str,
    interval: str,
    settings: MarketDataSettings | None = None,
) -> tuple[pd.DataFrame, ValidationResult]:
    """Fetch market data and validate it.

    Args:
        symbol: Ticker symbol
        period: Data period (e.g., "1y", "5d")
        interval: Bar interval (e.g., "1d", "5m")
        settings: Market data settings for validation

    Returns:
        Tuple of (DataFrame, ValidationResult)
        If validation fails, returns empty DataFrame with result.valid=False
    """
    settings = settings or MarketDataSettings()

    try:
        frame = fetch_bars(symbol, period, interval, settings=settings)
    except (ValueError, ConnectionError, OSError, TimeoutError) as e:
        logger.error(f"fetch_failed symbol={symbol} error={e}")
        return pd.DataFrame(), ValidationResult(
            valid=False,
            reason=f"fetch failed: {e}"
        )

    # Skip validation if disabled
    if not settings.validate_data:
        return frame, ValidationResult(valid=True)

    # Validate the data
    result = validate_market_data(
        frame,
        max_price_jump_pct=settings.max_price_jump_pct,
        max_volume_jump_pct=settings.max_volume_jump_pct,
        min_bars=settings.min_bars_for_signal,
    )

    if not result.valid:
        logger.warning(
            f"validation_failed symbol={symbol} period={period} interval={interval} "
            f"reason={result.reason}"
        )
        return pd.DataFrame(), result

    return frame, result
