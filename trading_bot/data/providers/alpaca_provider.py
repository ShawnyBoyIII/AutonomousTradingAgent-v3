from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd


def _normalize_symbol(symbol: str) -> str:
    """Translate a yfinance-style symbol to Alpaca's format.

    yfinance uses dashes for share classes (e.g. ``BRK-B``, ``BF-B``),
    while Alpaca's API uses dots (``BRK.B``, ``BF.B``). Regular stock
    symbols contain no dashes, so this substitution is safe.
    """
    return symbol.replace("-", ".")


def _split_period(period: str) -> tuple[int, str]:
    """Split a yfinance-style period/interval into (value, unit).

    Handles multi-char units like ``"mo"`` (month) as well as single-char
    units (``d``, ``w``, ``m``, ``h``, ``y``).

    Examples:
        ``"1mo"`` → ``(1, "mo")``
        ``"5m"``   → ``(5, "m")``
        ``"1y"``   → ``(1, "y")``
    """
    if period.endswith("mo"):
        return int(period[:-2]), "mo"
    return int(period[:-1]), period[-1]


def _parse_interval(interval: str) -> tuple[int, str]:
    """Parse a yfinance-style interval into (multiplier, unit) for Alpaca.

    Examples:
        ``"1d"`` → ``(1, "Day")``
        ``"5m"`` → ``(5, "Minute")``
        ``"15m"`` → ``(15, "Minute")``
        ``"1h"`` → ``(1, "Hour")``
        ``"1mo"`` → ``(1, "Month")``
    """
    multiplier, unit_char = _split_period(interval)
    unit_map = {"m": "Minute", "h": "Hour", "d": "Day", "mo": "Month"}
    unit = unit_map.get(unit_char, "Day")
    return multiplier, unit


def _period_to_start_end(period: str) -> tuple[datetime, datetime]:
    """Convert a yfinance-style period string to start/end datetimes.

    Both boundaries are returned as tz-aware UTC datetimes. The Alpaca SDK
    interprets naive datetimes as UTC, which silently cuts off the request by
    the bot server's local UTC offset (e.g., 4 hours during EDT) — see
    ``TestFetchBarsTimezone`` for the regression test.
    """
    end = datetime.now(timezone.utc)
    value, unit_char = _split_period(period)
    if unit_char == "d":
        start = end - timedelta(days=value)
    elif unit_char == "w":
        start = end - timedelta(weeks=value)
    elif unit_char == "m":
        start = end - timedelta(days=value * 30)
    elif unit_char == "mo":
        start = end - timedelta(days=value * 30)
    elif unit_char == "y":
        start = end - timedelta(days=value * 365)
    else:
        start = end - timedelta(days=30)
    return start, end


class AlpacaProvider:
    """Market data provider backed by Alpaca's historical data API.

    Requires ``alpaca-py`` to be installed and the following environment
    variables (or config values):

    * ``APCA_API_KEY_ID``
    * ``APCA_API_SECRET_KEY``
    """

    def __init__(self, api_key_id: str | None = None, api_secret_key: str | None = None) -> None:
        self._api_key_id = api_key_id
        self._api_secret_key = api_secret_key
        self._client_instance: object = None

    def _get_client(self) -> object:
        if self._client_instance is not None:
            return self._client_instance
        from alpaca.data.historical import StockHistoricalDataClient

        key_id = self._api_key_id
        api_secret = self._api_secret_key
        if not key_id:
            import os
            key_id = os.environ.get("APCA_API_KEY_ID", "")
        if not api_secret:
            import os
            api_secret = os.environ.get("APCA_API_SECRET_KEY", "")
        if not key_id or not api_secret:
            raise ValueError(
                "Alpaca API credentials not found. "
                "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY env vars "
                "or pass them to the constructor."
            )
        self._client_instance = StockHistoricalDataClient(key_id, api_secret)
        return self._client_instance

    def fetch_bars(
        self,
        symbol: str,
        period: str,
        interval: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        alpaca_symbol = _normalize_symbol(symbol)
        multiplier, unit = _parse_interval(interval)
        tf = TimeFrame(multiplier, TimeFrameUnit[unit])

        if start is not None:
            start_dt = datetime.fromisoformat(start) if "T" in start else datetime.combine(
                date.fromisoformat(start), datetime.min.time()
            )
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        else:
            start_dt, _ = _period_to_start_end(period)

        if end is not None:
            end_dt = datetime.fromisoformat(end) if "T" in end else datetime.combine(
                date.fromisoformat(end), datetime.min.time()
            )
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        else:
            end_dt = datetime.now(timezone.utc)

        request = StockBarsRequest(
            symbol_or_symbols=alpaca_symbol,
            timeframe=tf,
            start=start_dt,
            end=end_dt,
            adjustment="split",
        )

        client = self._get_client()
        response = client.get_stock_bars(request)

        raw = response.data.get(alpaca_symbol.upper(), [])
        if not raw:
            raise ValueError(f"No market data returned for {symbol}")

        records = []
        for bar in raw:
            records.append({
                "timestamp": bar.timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
            })

        frame = pd.DataFrame(records)
        if frame.empty:
            raise ValueError(f"No market data returned for {symbol}")
        return frame

    def fetch_small_cap_candidates(
        self,
        limit: int = 200,
        screeners: list[str] | None = None,
    ) -> list[dict[str, object]]:
        raise NotImplementedError(
            "Alpaca does not provide a built-in stock screener. "
            "Use YFinanceProvider for universe discovery."
        )
