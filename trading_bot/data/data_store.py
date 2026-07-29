"""Long-term EOD data store: Parquet partitions + SQLite manifest.

This module is the cold archive populated nightly by ``eod_fetcher``. The
manifest tracks (symbol, interval, last_fetched_date) so the fetcher is
idempotent. Offline tuning and analytics consumers read from here via
:func:`read_bars`.

**Separation guarantee**: this module never touches the live hot cache at
``state/market_data_cache.db``. The two stores have different access
patterns (live trade-fresh vs. cold archive), different TTLs, and
different consumers.

Configuration via :class:`EodDataStoreSettings` in
``trading_bot.config.settings``. Credentials come from env vars
(``MASSIVE_S3_*``), never from YAML (see
``trading_bot.config.loader._validate_credentials_not_in_config``).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


# Re-export so existing imports of `DataStoreSettings` keep working.
from trading_bot.config.settings import (  # noqa: E402  (import after logger)
    EodDataStoreSettings as DataStoreSettings,
)


# ---------------------------------------------------------------------------
# Manifest: SQLite tracking of (symbol, interval, last_fetched_date)
# ---------------------------------------------------------------------------


class DataStoreManifest:
    """SQLite-backed manifest tracking what data has been written.

    Schema: one row per (symbol, interval) with the latest ``as_of_date``
    successfully written. The fetcher consults this to skip days that are
    already in the store.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path else Path("state/data_store.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fetched (
                        symbol TEXT NOT NULL,
                        interval TEXT NOT NULL,
                        last_fetched_date TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (symbol, interval)
                    )
                    """
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.commit()
            finally:
                conn.close()
            import os
            try:
                if self._db_path.exists():
                    os.chmod(self._db_path, 0o600)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def record_fetch(
        self, symbol: str, interval: str, as_of_date: date
    ) -> None:
        """Mark that ``(symbol, interval, as_of_date)`` has been written."""
        sym = symbol.upper().strip()
        iv = interval.strip().lower()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO fetched (symbol, interval, last_fetched_date, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(symbol, interval) DO UPDATE SET
                        last_fetched_date = excluded.last_fetched_date,
                        updated_at = excluded.updated_at
                    """,
                    (
                        sym,
                        iv,
                        as_of_date.isoformat(),
                        pd.Timestamp.utcnow().isoformat(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def last_fetched(self, symbol: str, interval: str) -> date | None:
        """Return the latest ``as_of_date`` written for this symbol+interval, or None."""
        sym = symbol.upper().strip()
        iv = interval.strip().lower()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT last_fetched_date FROM fetched WHERE symbol = ? AND interval = ?",
                    (sym, iv),
                ).fetchone()
                if row is None:
                    return None
                return date.fromisoformat(row["last_fetched_date"])
            finally:
                conn.close()

    def first_symbol(self) -> str | None:
        """Return any symbol the manifest knows about, or None if empty.

        Used by callers that want a representative cohort to read from the
        store without having to enumerate the full universe.
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT symbol FROM fetched ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                return row["symbol"] if row else None
            finally:
                conn.close()

    def symbols(self) -> list[str]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT symbol FROM fetched ORDER BY symbol"
                ).fetchall()
            finally:
                conn.close()
        return [row["symbol"] for row in rows] if rows else []


def _all_known_symbols(db_path: Path) -> list[str]:
    """Helper used by tests/legacy callers; delegates to manifest.symbols()."""
    return DataStoreManifest(db_path=db_path).symbols()


# ---------------------------------------------------------------------------
# Partition layout: {root}/parquet/{symbol}/{interval}/{YYYY}/{MM}/{YYYY-MM-DD}.parquet
# ---------------------------------------------------------------------------


def _partition_path(
    root: Path, symbol: str, interval: str, as_of_date: date
) -> Path:
    sym = symbol.upper().strip()
    iv = interval.strip().lower()
    return (
        root
        / "parquet"
        / sym
        / iv
        / f"{as_of_date.year:04d}"
        / f"{as_of_date.month:02d}"
        / f"{as_of_date.year:04d}-{as_of_date.month:02d}-{as_of_date.day:02d}.parquet"
    )


def write_bars(
    df: pd.DataFrame,
    symbol: str,
    interval: str,
    as_of_date: date,
    root: Path,
    manifest: DataStoreManifest,
) -> Path:
    """Write ``df`` to its partition and update the manifest.

    Returns the Parquet file path written.
    """
    path = _partition_path(root, symbol, interval, as_of_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    manifest.record_fetch(symbol, interval, as_of_date)
    return path


def read_bars(
    symbol: str,
    interval: str,
    start: date,
    end: date,
    root: Path,
) -> pd.DataFrame:
    """Read bars for ``(symbol, interval)`` inside ``[start, end]``.

    Reads all Parquet files in the symbol's partition directory and filters
    rows whose ``window_start`` falls inside the date range. Returns an
    empty DataFrame if nothing has been written yet.
    """
    import pyarrow.parquet as pq  # local import: keeps module load fast

    sym = symbol.upper().strip()
    iv = interval.strip().lower()
    partition_dir = root / "parquet" / sym / iv
    if not partition_dir.exists():
        return pd.DataFrame()

    files = sorted(partition_dir.rglob("*.parquet"))
    if not files:
        return pd.DataFrame()

    frames = [pq.read_table(f).to_pandas() for f in files]
    df = pd.concat(frames, ignore_index=True)
    if df.empty or "window_start" not in df.columns:
        return df

    # Convert window_start (nanoseconds) to dates for filtering.
    ns = pd.to_numeric(df["window_start"], errors="coerce")
    dates = pd.to_datetime(ns, unit="ns", errors="coerce").dt.date
    mask = dates.between(start, end)
    return df.loc[mask].reset_index(drop=True)
