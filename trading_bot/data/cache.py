from __future__ import annotations

import json
import logging
import struct
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def _interval_to_ttl_seconds(interval: str) -> int:
    """Return cache TTL in seconds based on bar interval.

    Shorter intervals get shorter TTLs so data stays fresh.
    """
    interval = interval.strip().lower()
    if interval.endswith("mo"):
        value = int(interval[:-2])
        unit = "mo"
    else:
        value = int(interval[:-1])
        unit = interval[-1]

    if unit == "m":
        return max(value * 30, 60)
    if unit == "h":
        return max(value * 1800, 300)
    if unit == "d":
        return max(value * 43200, 3600)
    if unit == "w":
        return max(value * 302400, 86400)
    if unit == "mo":
        return max(value * 1_296_000, 604800)
    if unit == "y":
        return max(value * 15_552_000, 2_592_000)
    return 300


def _make_cache_key(
    symbol: str,
    period: str,
    interval: str,
    start: str | None,
    end: str | None,
    namespace: str | None = None,
) -> str:
    s = symbol.upper().strip()
    p = period.strip().lower()
    i = interval.strip().lower()
    st = start.strip().lower() if start else ""
    en = end.strip().lower() if end else ""
    ns = namespace.strip().lower() if namespace else ""
    return f"{ns}:{s}:{p}:{i}:{st}:{en}"


class MarketDataCache:
    """SQLite-backed cache for OHLCV market data with TTL expiration.

    Stores bars as JSON in a single row per cache key.
    TTL is derived from the bar interval: shorter intervals expire faster.
    Thread-safe via a reentrant lock.
    """

    def __init__(self, db_path: str | Path | None = None, max_entries: int = 10_000) -> None:
        self._db_path = Path(db_path) if db_path else Path(__file__).parent.parent.parent / "state" / "market_data_cache.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                os.chmod(self._db_path, 0o600)
            except OSError:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    period TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    ttl_seconds INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    data TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_symbol ON cache(symbol)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)
            """)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.commit()
            conn.close()

    def _connect(self) -> Any:
        import sqlite3
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def get(
        self,
        symbol: str,
        period: str,
        interval: str,
        start: str | None = None,
        end: str | None = None,
        namespace: str | None = None,
    ) -> pd.DataFrame | None:
        key = _make_cache_key(symbol, period, interval, start, end, namespace)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT data FROM cache WHERE key = ? AND expires_at > ?",
                    (key, datetime.now(timezone.utc).isoformat()),
                ).fetchone()
                if row is None:
                    return None
                return self._deserialize(row["data"])
            finally:
                conn.close()

    def put(
        self,
        symbol: str,
        period: str,
        interval: str,
        data: pd.DataFrame,
        start: str | None = None,
        end: str | None = None,
        namespace: str | None = None,
    ) -> None:
        key = _make_cache_key(symbol, period, interval, start, end, namespace)
        ttl = _interval_to_ttl_seconds(interval)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl)
        serialized = self._serialize(data)

        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO cache
                       (key, symbol, period, interval, created_at, ttl_seconds, expires_at, data)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, symbol.upper().strip(), period, interval,
                     now.isoformat(), ttl, expires.isoformat(), serialized),
                )
                conn.commit()
                self._evict_old(conn)
            finally:
                conn.close()

    def invalidate(self, symbol: str | None = None, period: str | None = None, interval: str | None = None) -> int:
        with self._lock:
            conn = self._connect()
            try:
                if not symbol and not period and not interval:
                    cursor = conn.execute("DELETE FROM cache")
                    conn.commit()
                    return cursor.rowcount
                conditions = []
                params = []
                if symbol:
                    conditions.append("symbol = ?")
                    params.append(symbol.upper().strip())
                if period:
                    conditions.append("period = ?")
                    params.append(period)
                if interval:
                    conditions.append("interval = ?")
                    params.append(interval)
                sql = f"DELETE FROM cache WHERE {' AND '.join(conditions)}"
                cursor = conn.execute(sql, params)
                count = cursor.rowcount
                conn.commit()
                return count
            finally:
                conn.close()

    def clear_expired(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM cache WHERE expires_at <= ?",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                count = cursor.rowcount
                conn.commit()
                return count
            finally:
                conn.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
                expired = conn.execute(
                    "SELECT COUNT(*) FROM cache WHERE expires_at <= ?",
                    (datetime.now(timezone.utc).isoformat(),),
                ).fetchone()[0]
                symbols = conn.execute(
                    "SELECT DISTINCT symbol FROM cache"
                ).fetchall()
                unique_symbols = [r["symbol"] for r in symbols]
                return {
                    "total_entries": total,
                    "expired_entries": expired,
                    "unique_symbols": len(unique_symbols),
                    "symbols": unique_symbols,
                    "db_path": str(self._db_path),
                    "db_size_bytes": self._db_path.stat().st_size if self._db_path.exists() else 0,
                }
            finally:
                conn.close()

    def _evict_old(self, conn: Any) -> None:
        count = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        if count > self._max_entries:
            to_delete = count - self._max_entries // 2
            conn.execute(
                "DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY expires_at ASC LIMIT ?)",
                (to_delete,),
            )
            conn.commit()

    @staticmethod
    def _serialize(df: pd.DataFrame) -> str:
        if df.empty:
            return json.dumps({"empty": True})

        def _convert_value(v: Any) -> Any:
            if isinstance(v, (pd.Timestamp, datetime)):
                return v.isoformat() if hasattr(v, "isoformat") else str(v)
            return v

        # Optimization: use itertuples(index=False) which is much faster than iterrows
        data = []
        for row in df.itertuples(index=False, name=None):
            data.append([_convert_value(v) for v in row])

        index_data = []
        if hasattr(df.index, "tolist"):
            index_data = df.index.tolist()
        else:
            index_data = list(df.index)
        index_data = [_convert_value(v) for v in index_data]

        return json.dumps({
            "columns": df.columns.tolist(),
            "index": index_data,
            "data": data,
            "dtype": str(df.dtypes.to_dict()),
        })

    @staticmethod
    def _deserialize(payload: str) -> pd.DataFrame:
        obj = json.loads(payload)
        if obj.get("empty"):
            return pd.DataFrame()
        df = pd.DataFrame(obj["data"], columns=obj["columns"])
        if "index" in obj:
            try:
                df.index = pd.to_datetime(obj["index"])
            except Exception:
                df.index = range(len(df))
        return df

    def __repr__(self) -> str:
        s = self.status()
        return f"MarketDataCache(entries={s['total_entries']}, symbols={s['unique_symbols']})"
