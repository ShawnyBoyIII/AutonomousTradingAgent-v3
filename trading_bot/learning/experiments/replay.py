from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from trading_bot.data.data_store import DataStoreManifest, read_bars

# When the caller asks for one of these intervals, the loader may need to
# read finer-grained data from the store (e.g. 1m bars for a 5m request) and
# aggregate it on the fly. The 1h case is symmetrically a resample target.
_RESAMPLE_RULES = {
    "5m": "5min",
    "1h": "1h",
}

# When a resample target interval is requested but no data is stored at
# that resolution, probe these candidate intervals in order and use the
# first one that has data. Finest first, since finer source data yields
# a faithful resample.
_RESAMPLE_SOURCE_CANDIDATES: tuple[str, ...] = ("1m", "5m", "1h", "1d")

_RESAMPLE_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


class StoredBarLoader:
    """Read bars from the local EOD store; never hits the network.

    Mirrors the ``market_data.fetch_bars`` signature so it can be used as
    a drop-in ``bar_loader`` for ``run_backtest``. If the requested
    interval has no data in the store but a finer-grained interval does
    (e.g. only 1m bars stored when 5m was requested), reads the source and
    resamples.
    """

    def __init__(self, root: Path, manifest_db: Path) -> None:
        self.root = Path(root)
        self.manifest = DataStoreManifest(db_path=Path(manifest_db))

    def _resolve_window(
        self,
        start: str | None,
        end: str | None,
    ) -> tuple[date, date]:
        end_date = date.fromisoformat(end) if end else date.today()
        start_date = (
            date.fromisoformat(start)
            if start
            else end_date.replace(year=end_date.year - 2)
        )
        return start_date, end_date

    def fetch_bars(
        self,
        symbol: str,
        period: str | None = None,
        interval: str = "1d",
        start: str | None = None,
        end: str | None = None,
        settings: Any = None,
    ) -> pd.DataFrame:
        start_d, end_d = self._resolve_window(start, end)
        df = read_bars(symbol, interval, start_d, end_d, self.root)

        # Resample-target intervals (e.g. 5m) may not be stored directly;
        # probe the manifest for a finer source interval.
        if df.empty and interval in _RESAMPLE_RULES:
            df = self._read_finest_available(symbol, interval, start_d, end_d)

        if df.empty:
            raise ValueError(
                f"No local bars for {symbol} {interval} between {start_d} and {end_d}"
            )

        if interval in _RESAMPLE_RULES:
            df = self._resample(df, interval)

        df = df.reset_index(names="timestamp")
        return df

    def available_symbols(self) -> list[str]:
        return list(self.manifest.symbols())

    def _read_finest_available(
        self,
        symbol: str,
        requested_interval: str,
        start_d: date,
        end_d: date,
    ) -> pd.DataFrame:
        partition_root = self.root / "parquet" / symbol
        if not partition_root.exists():
            return pd.DataFrame()
        for src in _RESAMPLE_SOURCE_CANDIDATES:
            if src == requested_interval:
                continue
            if not (partition_root / src).exists():
                continue
            df = read_bars(symbol, src, start_d, end_d, self.root)
            if not df.empty:
                return df
        return pd.DataFrame()

    def _resample(self, df: pd.DataFrame, interval: str) -> pd.DataFrame:
        rule = _RESAMPLE_RULES[interval]
        ts_index = pd.to_datetime(
            pd.to_numeric(df["window_start"], errors="coerce"), unit="ns"
        )
        df = df.set_index(ts_index)
        return df.resample(rule).agg(_RESAMPLE_AGG).dropna(subset=["open"])
