"""Tests for the high-level EOD fetcher orchestration (universe + persist)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from trading_bot.config.settings import EodDataStoreSettings
from trading_bot.data.eod_runner import fetch_and_persist_for_day, run_eod_fetch


@pytest.fixture
def symbols_file(tmp_path: Path) -> Path:
    p = tmp_path / "universe.txt"
    p.write_text("AAPL\nNVDA\nFOO\n# comment line\nBAR\n")
    return p


def _make_cfg(**overrides) -> EodDataStoreSettings:
    base = dict(
        enabled=True,
        intervals=["1d", "1m"],
        store_root="ignored-by-fixture",
        manifest_db="ignored-by-fixture.db",
        throttle_seconds=0,
        max_retries=1,
        backfill_years=5,
        minute_backfill_years=1,
        s3_region="us-east-1",
    )
    base.update(overrides)
    return EodDataStoreSettings(**base)


def _make_settings(cfg: EodDataStoreSettings) -> object:
    """Build a stub settings object with the eod_data_store fields the runner reads."""

    class _StubSettings:
        eod_data_store = cfg

    return _StubSettings()


class TestFetchAndPersistForDay:
    def test_writes_parquet_and_updates_manifest(
        self, tmp_path: Path, symbols_file: Path
    ) -> None:
        from trading_bot.data.data_store import DataStoreManifest

        store_root = tmp_path / "store"
        manifest = DataStoreManifest(db_path=tmp_path / "manifest.db")
        cfg = _make_cfg()

        mock_df = pd.DataFrame(
            {
                "ticker": ["AAPL", "NVDA", "FOO"],
                "close": [101.0, 202.0, 303.0],
                "window_start": [1_680_033_600_000_000_000] * 3,
            }
        )

        with patch(
            "trading_bot.data.eod_runner._fetch_for_day",
            return_value=mock_df,
        ):
            result = fetch_and_persist_for_day(
                product="day-aggregates",
                interval="1d",
                as_of_date=date(2026, 7, 7),
                universe_path=symbols_file,
                root=store_root,
                manifest=manifest,
                cfg=cfg,
            )

        # All 3 tickers are in the universe file, so all 3 are written.
        assert result.symbols_written == 3
        assert result.rows_written == 3
        assert manifest.last_fetched("AAPL", "1d") == date(2026, 7, 7)
        assert manifest.last_fetched("NVDA", "1d") == date(2026, 7, 7)
        assert manifest.last_fetched("FOO", "1d") == date(2026, 7, 7)
        aapl_partition = store_root / "parquet" / "AAPL" / "1d" / "2026"
        assert aapl_partition.exists()


class TestRunEodFetch:
    def test_writes_marker_file_when_complete(
        self, tmp_path: Path, symbols_file: Path
    ) -> None:
        store_root = tmp_path / "store"
        marker = tmp_path / "marker.txt"
        cfg = _make_cfg(store_root=str(store_root))
        settings = _make_settings(cfg)

        mock_df = pd.DataFrame(
            {
                "ticker": ["AAPL"],
                "close": [101.0],
                "window_start": [1_680_033_600_000_000_000],
            }
        )

        with patch(
            "trading_bot.data.eod_runner._fetch_for_day",
            return_value=mock_df,
        ):
            written = run_eod_fetch(
                settings=settings,
                universe_path=symbols_file,
                manifest_db=tmp_path / "manifest.db",
                as_of_date=date(2026, 7, 7),
                marker_file=marker,
                intervals=["1d"],  # narrow scope for the test
            )

        assert written >= 1
        assert marker.exists()
        assert marker.read_text().strip() == "2026-07-07"

    def test_skips_disabled_settings(
        self, tmp_path: Path, symbols_file: Path
    ) -> None:
        cfg = _make_cfg(enabled=False)
        settings = _make_settings(cfg)
        marker = tmp_path / "marker.txt"
        written = run_eod_fetch(
            settings=settings,
            universe_path=symbols_file,
            manifest_db=tmp_path / "manifest.db",
            as_of_date=date(2026, 7, 7),
            marker_file=marker,
            intervals=["1d"],
        )
        assert written == 0
        assert not marker.exists()

    def test_does_not_write_marker_on_total_failure(
        self, tmp_path: Path, symbols_file: Path
    ) -> None:
        """When every interval fails, skip the marker so retries can run."""
        cfg = _make_cfg()
        settings = _make_settings(cfg)
        marker = tmp_path / "marker.txt"
        # Mock _fetch_for_day to raise — all intervals fail.
        with patch(
            "trading_bot.data.eod_runner._fetch_for_day",
            side_effect=RuntimeError("S3 is down"),
        ):
            written = run_eod_fetch(
                settings=settings,
                universe_path=symbols_file,
                manifest_db=tmp_path / "manifest.db",
                as_of_date=date(2026, 7, 7),
                marker_file=marker,
                intervals=["1d"],
            )

        assert written == 0
        assert not marker.exists(), (
            "marker should not be written when every interval failed — "
            "would lock out future retries"
        )

    def test_picks_correct_key_template_per_product(
        self, tmp_path: Path, symbols_file: Path
    ) -> None:
        """Runner should route each product to its configured key template."""
        cfg = _make_cfg(
            day_aggregates_key_template="us_stocks_sip/day_aggs_v1/{year}/{date}.csv.gz",
            minute_aggregates_key_template="us_stocks_sip/minute_aggs_v1/{year}/{date}.csv.gz",
            quotes_key_template="us_stocks_sip/quotes_v1/{year}/{date}.csv.gz",
            trades_key_template="us_stocks_sip/trades_v1/{year}/{date}.csv.gz",
        )
        settings = _make_settings(cfg)

        captured_templates: list[str | None] = []

        def fake_fetch(client, product, as_of_date, universe, key_template=None):
            captured_templates.append(key_template)
            return pd.DataFrame(
                {"ticker": ["AAPL"], "close": [1.0], "window_start": [0]}
            )

        with patch("trading_bot.data.eod_runner._fetch_for_day", side_effect=fake_fetch):
            run_eod_fetch(
                settings=settings,
                universe_path=symbols_file,
                manifest_db=tmp_path / "manifest.db",
                as_of_date=date(2026, 7, 7),
                marker_file=tmp_path / "marker.txt",
                intervals=["1d", "1m", "quotes", "trades"],
            )

        assert captured_templates == [
            "us_stocks_sip/day_aggs_v1/{year}/{date}.csv.gz",
            "us_stocks_sip/minute_aggs_v1/{year}/{date}.csv.gz",
            "us_stocks_sip/quotes_v1/{year}/{date}.csv.gz",
            "us_stocks_sip/trades_v1/{year}/{date}.csv.gz",
        ]


def test_previous_trading_day_weekday():
    """A weekday target's previous trading day is the prior weekday."""
    from datetime import date

    from trading_bot.cli.app import _previous_trading_day

    # Mon 2026-07-27 -> Fri 2026-07-24
    assert _previous_trading_day(date(2026, 7, 27)) == date(2026, 7, 24)
    # Tue 2026-07-28 -> Mon 2026-07-27
    assert _previous_trading_day(date(2026, 7, 28)) == date(2026, 7, 27)
    # Wed 2026-07-29 -> Tue 2026-07-28
    assert _previous_trading_day(date(2026, 7, 29)) == date(2026, 7, 28)


def test_previous_trading_day_weekend():
    """Saturday/Sunday target's previous trading day is the prior Friday."""
    from datetime import date

    from trading_bot.cli.app import _previous_trading_day

    # Sat 2026-07-25 -> Fri 2026-07-24
    assert _previous_trading_day(date(2026, 7, 25)) == date(2026, 7, 24)
    # Sun 2026-07-26 -> Fri 2026-07-24
    assert _previous_trading_day(date(2026, 7, 26)) == date(2026, 7, 24)