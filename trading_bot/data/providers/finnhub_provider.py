from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import pandas as pd


class FinnhubProvider:
    """Market data provider backed by the Finnhub Stock Candles API.

    Requires ``FINNHUB_API_KEY`` in the environment.
    Free tier: 60 calls/min, daily resolution only (1, 5, 15, 30, 60 min
    resolutions require a paid plan).  When intraday data is unavailable the
    provider raises ``ValueError`` so the fallback chain can switch to
    yfinance.

    Docs: https://finnhub.io/docs/api/stock-candles
    """

    BASE_URL = "https://finnhub.io/api/v1/stock/candle"

    _RESOLUTION_MAP: dict[str, str] = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "1d": "D",
        "1wk": "W",
        "1mo": "M",
    }

    _PERIOD_MAP: dict[str, int] = {
        "1d": 1,
        "5d": 5,
        "1mo": 30,
        "3mo": 90,
        "6mo": 180,
        "1y": 365,
        "2y": 730,
        "5y": 1826,
        "10y": 3650,
        "max": 3650,
    }

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "FINNHUB_API_KEY not set. Provide it via the constructor "
                "or set the environment variable."
            )

    # ------------------------------------------------------------------ #
    #  Public API                                                        #
    # ------------------------------------------------------------------ #

    def fetch_bars(
        self,
        symbol: str,
        period: str,
        interval: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        import requests

        resolution = self._resolve_interval(interval)
        from_ts, to_ts = self._resolve_timerange(period, start, end)

        params: dict[str, str | int] = {
            "symbol": symbol.upper(),
            "resolution": resolution,
            "from": from_ts,
            "to": to_ts,
            "token": self._api_key,
        }

        resp = requests.get(self.BASE_URL, params=params, timeout=30)
        if resp.status_code in (401, 403):
            raise ValueError(
                f"Finnhub access denied for {symbol} — check API key or plan "
                f"at https://finnhub.io/dashboard"
            )
        resp.raise_for_status()
        payload = resp.json()

        status = payload.get("s", "")
        if status == "no_data":
            raise ValueError(f"No market data returned for {symbol}")
        if status != "ok":
            raise ValueError(f"Finnhub returned status '{status}' for {symbol}")

        return self._build_frame(payload)

    def fetch_small_cap_candidates(
        self,
        limit: int = 200,
        screeners: list[str] | None = None,
    ) -> list[dict[str, object]]:
        raise NotImplementedError("Finnhub does not provide a small-cap screener")

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _resolve_interval(self, interval: str) -> str:
        mapped = self._RESOLUTION_MAP.get(interval)
        if mapped is not None:
            return mapped
        raise ValueError(
            f"Finnhub does not support interval '{interval}'. "
            f"Supported: {list(self._RESOLUTION_MAP)}"
        )

    def _resolve_timerange(
        self,
        period: str,
        start: str | None,
        end: str | None,
    ) -> tuple[int, int]:
        if start is not None or end is not None:
            from_ts = self._parse_date(start) if start else 0
            to_ts = self._parse_date(end) if end else int(datetime.now(timezone.utc).timestamp())
            return from_ts, to_ts

        days = self._PERIOD_MAP.get(period)
        if days is not None:
            to_dt = datetime.now(timezone.utc)
            from_dt = to_dt - timedelta(days=days)
            return int(from_dt.timestamp()), int(to_dt.timestamp())

        match = re.match(r"^(\d+)([dhmy])$", period)
        if match:
            num = int(match.group(1))
            unit = match.group(2)
            multipliers = {"d": 1, "h": 1.0 / 24, "m": 30, "y": 365}
            days = int(num * multipliers.get(unit, 1))
            to_dt = datetime.now(timezone.utc)
            from_dt = to_dt - timedelta(days=max(days, 1))
            return int(from_dt.timestamp()), int(to_dt.timestamp())

        raise ValueError(f"Cannot parse period '{period}'")

    def _parse_date(self, date_str: str) -> int:
        return int(datetime.fromisoformat(date_str).timestamp())

    def _build_frame(self, payload: dict) -> pd.DataFrame:
        timestamps = payload.get("t", [])
        if not timestamps:
            raise ValueError("Finnhub returned empty candle data")

        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
                "open": payload.get("o", []),
                "high": payload.get("h", []),
                "low": payload.get("l", []),
                "close": payload.get("c", []),
                "volume": payload.get("v", []),
            }
        )
        frame.sort_values("timestamp", inplace=True)
        frame.reset_index(drop=True, inplace=True)
        return frame
