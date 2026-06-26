from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import pandas as pd


class PolygonProvider:
    """Market data provider backed by the Polygon.io Aggregates API.

    Requires ``POLYGON_API_KEY`` in the environment.
    Free tier (Stocks Basic): 5 calls/min, end-of-day data, 2-year history.

    Docs: https://polygon.io/docs/stocks/get_v2_aggs_ticker__stocksticker__range__multiplier___timespan___from___to
    """

    BASE_URL = "https://api.polygon.io/v2/aggs/ticker"

    _MAX_RETRIES = 3
    _RETRY_DELAY = 2.0  # seconds

    _TIMESPAN_MAP: dict[str, tuple[int, str]] = {
        "1m": (1, "minute"),
        "5m": (5, "minute"),
        "15m": (15, "minute"),
        "30m": (30, "minute"),
        "1h": (1, "hour"),
        "1d": (1, "day"),
        "1wk": (1, "week"),
        "1mo": (1, "month"),
    }

    _PERIOD_DAYS: dict[str, int] = {
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
        self._api_key = api_key or os.environ.get("POLYGON_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "POLYGON_API_KEY not set. Provide it via the constructor "
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
        import time
        import requests

        multiplier, timespan = self._resolve_interval(interval)
        from_str, to_str = self._resolve_timerange(period, start, end)

        url = (
            f"{self.BASE_URL}/{symbol.upper()}"
            f"/range/{multiplier}/{timespan}/{from_str}/{to_str}"
        )
        params: dict[str, str | int] = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
            "apiKey": self._api_key,
        }

        last_status: int | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = self._RETRY_DELAY * attempt
                time.sleep(wait)
                last_status = 429
                continue
            resp.raise_for_status()
            break
        else:
            raise ValueError(
                f"Polygon rate limit exceeded for {symbol} "
                f"after {self._MAX_RETRIES} retries"
            )
        payload = resp.json()

        if payload.get("status") == "ERROR":
            raise ValueError(
                f"Polygon error for {symbol}: {payload.get('error', 'unknown')}"
            )
        results = payload.get("results")
        if not results:
            raise ValueError(f"No market data returned for {symbol}")

        return self._build_frame(results)

    def fetch_small_cap_candidates(
        self,
        limit: int = 200,
        screeners: list[str] | None = None,
    ) -> list[dict[str, object]]:
        raise NotImplementedError("Polygon does not provide a small-cap screener")

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _resolve_interval(self, interval: str) -> tuple[int, str]:
        mapped = self._TIMESPAN_MAP.get(interval)
        if mapped is not None:
            return mapped
        raise ValueError(
            f"Polygon does not support interval '{interval}'. "
            f"Supported: {list(self._TIMESPAN_MAP)}"
        )

    def _resolve_timerange(
        self,
        period: str,
        start: str | None,
        end: str | None,
    ) -> tuple[str, str]:
        if start is not None or end is not None:
            from_str = start if start else "2020-01-01"
            to_str = end if end else datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return from_str, to_str

        days = self._PERIOD_DAYS.get(period)
        if days is not None:
            to_dt = datetime.now(timezone.utc)
            from_dt = to_dt - timedelta(days=days)
            return from_dt.strftime("%Y-%m-%d"), to_dt.strftime("%Y-%m-%d")

        match = re.match(r"^(\d+)([dhmy])$", period)
        if match:
            num = int(match.group(1))
            unit = match.group(2)
            multipliers = {"d": 1, "h": 1.0 / 24, "m": 30, "y": 365}
            days_val = int(num * multipliers.get(unit, 1))
            to_dt = datetime.now(timezone.utc)
            from_dt = to_dt - timedelta(days=max(days_val, 1))
            return from_dt.strftime("%Y-%m-%d"), to_dt.strftime("%Y-%m-%d")

        raise ValueError(f"Cannot parse period '{period}'")

    def _build_frame(self, results: list[dict]) -> pd.DataFrame:
        rows = [
            {
                "timestamp": pd.Timestamp(r["t"], unit="ms", tz="UTC"),
                "open": r["o"],
                "high": r["h"],
                "low": r["l"],
                "close": r["c"],
                "volume": r.get("v", 0),
            }
            for r in results
        ]
        frame = pd.DataFrame(rows)
        frame.sort_values("timestamp", inplace=True)
        frame.reset_index(drop=True, inplace=True)
        return frame
