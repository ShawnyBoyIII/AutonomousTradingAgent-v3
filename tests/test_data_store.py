"""Tests for the long-term EOD data store (SQLite manifest + Parquet partitions).

The data store is the cold archive populated by `eod_fetcher` at end of day.
Learning loops (`tuning_overrides`, `daily_supermodel`) read from it via
`read_bars(symbol, interval, start, end)`.

It is intentionally separate from `state/market_data_cache.db` (the live hot
cache used by the scan path).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from trading_bot.config.settings import EodDataStoreSettings
from trading_bot.data.data_store import (
    DataStoreManifest,
    DataStoreSettings,
    read_bars,
    write_bars,
)


# ---------------------------------------------------------------------------
# Manifest: SQLite tracking of (symbol, interval, last_fetched_date)
# ---------------------------------------------------------------------------


class TestManifest:
    def test_creates_db_file_on_init(self, tmp_path: Path) -> None:
        db = tmp_path / "manifest.db"
        DataStoreManifest(db_path=db)
        assert db.exists()

    def test_starts_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "manifest.db"
        manifest = DataStoreManifest(db_path=db)
        assert manifest.last_fetched("AAPL", "1d") is None
        assert manifest.last_fetched("AAPL", "1m") is None

    def test_records_last_fetched_date(self, tmp_path: Path) -> None:
        db = tmp_path / "manifest.db"
        manifest = DataStoreManifest(db_path=db)
        manifest.record_fetch("AAPL", "1d", date(2026, 7, 7))
        assert manifest.last_fetched("AAPL", "1d") == date(2026, 7, 7)

    def test_distinguishes_intervals_per_symbol(self, tmp_path: Path) -> None:
        db = tmp_path / "manifest.db"
        manifest = DataStoreManifest(db_path=db)
        manifest.record_fetch("AAPL", "1d", date(2026, 7, 7))
        manifest.record_fetch("AAPL", "1m", date(2026, 7, 6))
        assert manifest.last_fetched("AAPL", "1d") == date(2026, 7, 7)
        assert manifest.last_fetched("AAPL", "1m") == date(2026, 7, 6)

    def test_record_fetch_updates_existing_entry(self, tmp_path: Path) -> None:
        db = tmp_path / "manifest.db"
        manifest = DataStoreManifest(db_path=db)
        manifest.record_fetch("AAPL", "1d", date(2026, 7, 6))
        manifest.record_fetch("AAPL", "1d", date(2026, 7, 7))
        assert manifest.last_fetched("AAPL", "1d") == date(2026, 7, 7)

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        db = tmp_path / "manifest.db"
        DataStoreManifest(db_path=db).record_fetch("AAPL", "1d", date(2026, 7, 7))
        # New instance pointing at the same file
        reloaded = DataStoreManifest(db_path=db)
        assert reloaded.last_fetched("AAPL", "1d") == date(2026, 7, 7)


# ---------------------------------------------------------------------------
# write_bars / read_bars: round-trip a DataFrame through Parquet partitions
# ---------------------------------------------------------------------------


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    """Return a tmp directory wired as the data store root (manifest + parquet)."""
    return tmp_path


def _sample_bars(rows: int = 5, base_date: date | None = None) -> pd.DataFrame:
    """Return a sample OHLCV DataFrame matching the Massive.com schema.

    ``base_date`` is the first day's window_start in nanoseconds; defaults to
    2026-07-05 so the test query range (July 2026) actually covers the rows.
    """
    if base_date is None:
        base_date = date(2026, 7, 5)
    base_ns = int(
        pd.Timestamp(base_date.isoformat(), tz="America/New_York").timestamp()
        * 1_000_000_000
    )
    return pd.DataFrame(
        {
            "ticker": ["AAPL"] * rows,
            "volume": [100 + i for i in range(rows)],
            "open": [100.0 + i for i in range(rows)],
            "close": [101.0 + i for i in range(rows)],
            "high": [102.0 + i for i in range(rows)],
            "low": [99.0 + i for i in range(rows)],
            "window_start": [base_ns + i * 86_400_000_000_000 for i in range(rows)],
            "transactions": [10 + i for i in range(rows)],
        }
    )


class TestWriteAndRead:
    def test_write_bars_creates_parquet_file(self, store_root: Path) -> None:
        manifest = DataStoreManifest(db_path=store_root / "manifest.db")
        df = _sample_bars(3)
        path = write_bars(
            df, symbol="AAPL", interval="1d",
            as_of_date=date(2026, 7, 7),
            root=store_root, manifest=manifest,
        )
        assert path.exists()
        assert path.suffix == ".parquet"

    def test_write_bars_records_manifest(self, store_root: Path) -> None:
        manifest = DataStoreManifest(db_path=store_root / "manifest.db")
        write_bars(
            _sample_bars(3), symbol="AAPL", interval="1d",
            as_of_date=date(2026, 7, 7),
            root=store_root, manifest=manifest,
        )
        assert manifest.last_fetched("AAPL", "1d") == date(2026, 7, 7)

    def test_write_bars_creates_partition_per_symbol(
        self, store_root: Path
    ) -> None:
        manifest = DataStoreManifest(db_path=store_root / "manifest.db")
        write_bars(
            _sample_bars(2), symbol="AAPL", interval="1d",
            as_of_date=date(2026, 7, 7),
            root=store_root, manifest=manifest,
        )
        write_bars(
            _sample_bars(2), symbol="NVDA", interval="1d",
            as_of_date=date(2026, 7, 7),
            root=store_root, manifest=manifest,
        )
        # Different symbols get different subdirectories.
        aapl_files = list((store_root / "parquet" / "AAPL" / "1d").rglob("*.parquet"))
        nvda_files = list((store_root / "parquet" / "NVDA" / "1d").rglob("*.parquet"))
        assert len(aapl_files) == 1
        assert len(nvda_files) == 1

    def test_read_bars_returns_empty_when_nothing_written(
        self, store_root: Path
    ) -> None:
        result = read_bars(
            "AAPL", "1d", start=date(2026, 7, 1), end=date(2026, 7, 31),
            root=store_root,
        )
        assert result.empty

    def test_read_bars_round_trips_dataframe(self, store_root: Path) -> None:
        manifest = DataStoreManifest(db_path=store_root / "manifest.db")
        original = _sample_bars(5)
        write_bars(
            original, symbol="AAPL", interval="1d",
            as_of_date=date(2026, 7, 7),
            root=store_root, manifest=manifest,
        )
        result = read_bars(
            "AAPL", "1d", start=date(2026, 7, 1), end=date(2026, 7, 31),
            root=store_root,
        )
        assert not result.empty
        assert len(result) == len(original)
        assert list(result["close"]) == list(original["close"])

    def test_read_bars_filters_by_date_range(self, store_root: Path) -> None:
        manifest = DataStoreManifest(db_path=store_root / "manifest.db")
        # Write 1 row per day so each Parquet file contains exactly one date.
        for d in (date(2026, 7, 5), date(2026, 7, 6), date(2026, 7, 7)):
            write_bars(
                _sample_bars(1, base_date=d),
                symbol="AAPL", interval="1d",
                as_of_date=d, root=store_root, manifest=manifest,
            )
        # 1-day query returns exactly the row for that day.
        one_day = read_bars(
            "AAPL", "1d", start=date(2026, 7, 6), end=date(2026, 7, 6),
            root=store_root,
        )
        assert len(one_day) == 1
        # 3-day query returns all 3 rows.
        three_days = read_bars(
            "AAPL", "1d", start=date(2026, 7, 5), end=date(2026, 7, 7),
            root=store_root,
        )
        assert len(three_days) == 3

    def test_read_bars_excludes_outside_range(self, store_root: Path) -> None:
        manifest = DataStoreManifest(db_path=store_root / "manifest.db")
        for d in (date(2026, 7, 5), date(2026, 7, 6), date(2026, 7, 7)):
            write_bars(
                _sample_bars(1, base_date=d),
                symbol="AAPL", interval="1d",
                as_of_date=d, root=store_root, manifest=manifest,
            )
        # Query before the written range — should be empty.
        before = read_bars(
            "AAPL", "1d", start=date(2026, 1, 1), end=date(2026, 1, 31),
            root=store_root,
        )
        assert before.empty
        # Query after the written range — should be empty.
        after = read_bars(
            "AAPL", "1d", start=date(2027, 1, 1), end=date(2027, 12, 31),
            root=store_root,
        )
        assert after.empty


# ---------------------------------------------------------------------------
# Settings: EodDataStoreSettings + fetch_data_store_settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_default_settings_have_sensible_values(self) -> None:
        s = EodDataStoreSettings()
        assert s.provider == "massive_flat_files"
        assert "1d" in s.intervals
        assert s.backfill_years >= 1
        assert s.minute_backfill_years >= 0
        assert s.throttle_seconds >= 0
        assert s.max_retries >= 1

    def test_settings_reject_negative_throttle(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            EodDataStoreSettings(throttle_seconds=-1)

    def test_settings_reject_zero_retries(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            EodDataStoreSettings(max_retries=0)

    def test_settings_reject_negative_backfill_years(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            EodDataStoreSettings(backfill_years=0)

    def test_settings_default_verify_tls_is_safe(self) -> None:
        """Default is verify_tls=True — strict cert verification, secure."""
        s = EodDataStoreSettings()
        assert s.verify_tls is True
        assert s.tls_ca_bundle is None

    def test_settings_accept_tls_ca_bundle(self) -> None:
        s = EodDataStoreSettings(tls_ca_bundle="/etc/ssl/massive-ca.pem")
        assert s.tls_ca_bundle == "/etc/ssl/massive-ca.pem"

    def test_settings_default_key_templates_are_none(self) -> None:
        """Null templates mean "use the built-in default" (see build_s3_key)."""
        s = EodDataStoreSettings()
        assert s.day_aggregates_key_template is None
        assert s.minute_aggregates_key_template is None

    def test_settings_accept_custom_key_templates(self) -> None:
        s = EodDataStoreSettings(
            day_aggregates_key_template="us_stocks_sip/day_aggs_v1/{year}/{date}.csv.gz",
            minute_aggregates_key_template="us_stocks_sip/minute_aggs_v1/{year}/{date}.csv.gz",
            quotes_key_template="us_stocks_sip/quotes_v1/{year}/{date}.csv.gz",
            trades_key_template="us_stocks_sip/trades_v1/{year}/{date}.csv.gz",
        )
        assert "{year}" in s.day_aggregates_key_template
        assert "{year}" in s.minute_aggregates_key_template
        assert "{year}" in s.quotes_key_template
        assert "{year}" in s.trades_key_template

    def test_settings_default_auth_mode_is_sigv4(self) -> None:
        s = EodDataStoreSettings()
        assert s.auth_mode == "sigv4"

    def test_settings_default_addressing_style_is_path(self) -> None:
        """Default path-style because massive.com routes virtual-hosted to
        its REST API gateway, which rejects SigV4 signatures."""
        s = EodDataStoreSettings()
        assert s.addressing_style == "path"