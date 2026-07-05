from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from trading_bot.config.settings import (
    SUPPORTED_MARKET_DATA_PROVIDERS,
    MarketDataSettings,
)
from trading_bot.data.cache import MarketDataCache
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


def _prioritize_provider_names(
    settings: MarketDataSettings | None = None,
    interval: str | None = None,
) -> list[str]:
    names = list(settings.provider_stack if settings is not None else ["yfinance"])
    if not interval:
        return names

    token = str(interval).strip().lower()
    # For sub-daily bars, prefer lower-latency providers before yfinance when
    # the user configured a stack containing both.
    is_intraday = token.endswith("m") or token.endswith("h")
    if not is_intraday:
        return names

    priority = {"alpaca": 0, "polygon": 1, "finnhub": 2, "yfinance": 3}
    return sorted(names, key=lambda name: (priority.get(str(name).strip().lower(), 99), names.index(name)))


def _resolve_provider_stack(settings: MarketDataSettings | None = None, interval: str | None = None) -> list:
    """Return an ordered list of provider instances from the effective provider stack."""
    names = _prioritize_provider_names(settings, interval)
    providers: list = []
    for name in names:
        providers.append(_resolve_provider_by_name(name))
    return providers


def _cache_namespace(settings: MarketDataSettings | None = None, interval: str | None = None) -> str:
    names = _prioritize_provider_names(settings, interval)
    return "providers=" + ",".join(str(name).strip().lower() for name in names)


def _resolve_provider_by_name(name: str) -> Any:
    """Return a single provider instance for the given *name* string."""
    name = str(name).strip().lower()
    if name == "alpaca":
        from trading_bot.data.providers.alpaca_provider import AlpacaProvider
        return AlpacaProvider()
    if name == "finnhub":
        from trading_bot.data.providers.finnhub_provider import FinnhubProvider
        return FinnhubProvider()
    if name == "polygon":
        from trading_bot.data.providers.polygon_provider import PolygonProvider
        return PolygonProvider()
    if name != "yfinance":
        supported = ", ".join(sorted(SUPPORTED_MARKET_DATA_PROVIDERS))
        raise ValueError(
            f"Unsupported market data provider '{name}'. "
            f"Supported providers: {supported}"
        )
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
    """Try each provider in the configured stack; first success wins."""
    stack_names = _prioritize_provider_names(primary_settings, interval)
    if not stack_names:
        stack_names = ["yfinance"]
    logger.info(f"_fallback_fetch symbol={symbol} stack={stack_names}")
    last_error: Exception | None = None
    for name in stack_names:
        try:
            provider = _resolve_provider_by_name(name)
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"fetch_failed symbol={symbol} provider={name} error=init_failed:{exc}"
            )
            continue
        try:
            return provider.fetch_bars(symbol, period, interval, start=start, end=end)
        except (ValueError, ConnectionError, OSError, TimeoutError) as exc:
            last_error = exc
            logger.warning(
                f"fetch_failed symbol={symbol} provider={type(provider).__name__} error={exc}"
            )
    raise ValueError(
        f"All providers failed for {symbol} ({period}/{interval}): {last_error}"
    )


def fetch_bars(
    symbol: str,
    period: str,
    interval: str,
    start: str | None = None,
    end: str | None = None,
    settings: MarketDataSettings | None = None,
) -> pd.DataFrame:
    cache = _get_cache()
    namespace = _cache_namespace(settings, interval)
    cached = cache.get(symbol, period, interval, start, end, namespace=namespace)
    if cached is not None:
        return cached

    result = _fallback_fetch(symbol, period, interval, start=start, end=end, primary_settings=settings)
    if result is not None and not result.empty:
        cache.put(symbol, period, interval, result, start=start, end=end, namespace=namespace)
    return result


_cache_instance: MarketDataCache | None = None


def _get_cache() -> MarketDataCache:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = MarketDataCache()
    return _cache_instance


def reset_cache() -> None:
    global _cache_instance
    _cache_instance = None


def clear_cache(symbol: str | None = None) -> dict:
    cache = _get_cache()
    if symbol:
        count = cache.invalidate(symbol=symbol.upper().strip())
        logger.info(f"Cleared {count} cache entries for {symbol}")
    else:
        cache.clear_expired()
        count = 0
    return cache.status()


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
