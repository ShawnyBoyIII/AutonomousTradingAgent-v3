"""High-level orchestration for the EOD fetcher.

Glues together :mod:`trading_bot.data.eod_fetcher` (S3 download + filter)
and :mod:`trading_bot.data.data_store` (Parquet write + manifest). The
runner is the single function the CLI / shell calls; it handles:

- reading the universe file (newline-delimited, ``#``-prefixed comments)
- iterating over the configured intervals
- persisting to the data store
- writing a marker file when complete (used by ``auto-burn-in.sh`` for
  idempotency)

Credentials are read by the S3 client from environment variables.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd

from trading_bot.config.settings import EodDataStoreSettings
from trading_bot.data.data_store import (
    DataStoreManifest,
    write_bars,
)
from trading_bot.data.eod_fetcher import (
    MassiveFlatFilesClient,
    build_s3_key,
    filter_csv_to_universe,
    parse_massive_day_aggregates_csv,
)

logger = logging.getLogger(__name__)


# Map our internal interval names to massive.com product names.
# - 1d / 1m = pre-aggregated OHLCV bars (day, minute)
# - quotes / trades = raw tick-level data (NASDAQ ITCH feed)
_INTERVAL_TO_PRODUCT = {
    "1d": "day-aggregates",
    "1m": "minute-aggregates",
    "quotes": "quotes",
    "trades": "trades",
}


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------


def load_universe(path: Path) -> set[str]:
    """Read a newline-delimited symbol file, skipping blanks and ``#`` comments."""
    symbols: set[str] = set()
    if not path.exists():
        return symbols
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        symbols.add(line.upper())
    return symbols


# ---------------------------------------------------------------------------
# Per-day fetch + persist
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """Result of fetching one day for one product."""

    product: str
    interval: str
    as_of_date: date
    symbols_written: int
    rows_written: int
    parquet_path: Path | None


def _fetch_for_day(
    client: MassiveFlatFilesClient,
    product: str,
    as_of_date: date,
    universe: set[str],
    key_template: str | None = None,
) -> pd.DataFrame:
    """Single network call: GET the day's file, parse CSV, filter to universe.

    Returns an empty DataFrame if the universe doesn't intersect the file
    (very common when backfilling — most symbols in a daily file are not
    in our scout universe).
    """
    key = build_s3_key(product, as_of_date, key_template=key_template)
    body = client.get_object_text(key)
    df = parse_massive_day_aggregates_csv(body)
    return filter_csv_to_universe(df, universe)


def _key_template_for(cfg: EodDataStoreSettings, product: str) -> str | None:
    """Return the configured per-product S3 key template, or None for the default.

    The runner uses these templates to build the S3 key for each fetch.
    Defaults to None (so :func:`build_s3_key` uses its built-in
    product-name-based template). The user can override any product
    independently via ``burn-in-config.yaml``.
    """
    return {
        "day-aggregates": cfg.day_aggregates_key_template,
        "minute-aggregates": cfg.minute_aggregates_key_template,
        "quotes": cfg.quotes_key_template,
        "trades": cfg.trades_key_template,
    }.get(product)


def _make_client(cfg: EodDataStoreSettings) -> MassiveFlatFilesClient:
    """Build an S3 client from the eod_data_store config + MASSIVE_S3_* env vars.

    ``cfg.max_retries``, ``cfg.throttle_seconds``, ``cfg.verify_tls``,
    ``cfg.tls_ca_bundle``, ``cfg.auth_mode``, and ``cfg.addressing_style``
    come from config; credentials live exclusively in env vars (see
    ``config/loader._validate_credentials_not_in_config``).
    """
    return MassiveFlatFilesClient(
        endpoint=os.environ.get("MASSIVE_S3_ENDPOINT", ""),
        access_key=os.environ.get("MASSIVE_S3_ACCESS_KEY_ID", ""),
        secret_key=os.environ.get("MASSIVE_S3_SECRET_ACCESS_KEY", ""),
        bucket=os.environ.get("MASSIVE_S3_BUCKET", ""),
        region=cfg.s3_region,
        throttle_seconds=cfg.throttle_seconds,
        max_retries=cfg.max_retries,
        verify_tls=cfg.verify_tls,
        tls_ca_bundle=cfg.tls_ca_bundle,
        auth_mode=cfg.auth_mode,
        addressing_style=cfg.addressing_style,
    )


def fetch_and_persist_for_day(
    product: str,
    interval: str,
    as_of_date: date,
    universe_path: Path,
    root: Path,
    manifest: DataStoreManifest,
    cfg: EodDataStoreSettings,
    key_template: str | None = None,
) -> FetchResult:
    """Fetch one day for one product, persist each symbol to Parquet.

    Each row in the fetched DataFrame is its own symbol's daily file. The
    runner writes one Parquet partition per (symbol, interval, date).
    """
    universe = load_universe(universe_path)
    if not universe:
        logger.info("universe is empty; skipping %s on %s", product, as_of_date)
        return FetchResult(product, interval, as_of_date, 0, 0, None)

    # Build a client per call. Each fetch is independent and isolated.
    client = _make_client(cfg)

    df = _fetch_for_day(client, product, as_of_date, universe, key_template=key_template)
    if df.empty:
        return FetchResult(product, interval, as_of_date, 0, 0, None)

    symbols_written = 0
    rows_written = 0
    last_path: Path | None = None
    for ticker, group in df.groupby("ticker"):
        if cfg.throttle_seconds > 0:
            time.sleep(cfg.throttle_seconds)
        path = write_bars(
            group.reset_index(drop=True),
            symbol=str(ticker),
            interval=interval,
            as_of_date=as_of_date,
            root=root,
            manifest=manifest,
        )
        symbols_written += 1
        rows_written += len(group)
        last_path = path

    return FetchResult(product, interval, as_of_date, symbols_written, rows_written, last_path)


# ---------------------------------------------------------------------------
# Top-level entry point: do one day, all intervals
# ---------------------------------------------------------------------------


def run_eod_fetch(
    settings,
    universe_path: Path,
    manifest_db: Path,
    as_of_date: date,
    marker_file: Path,
    intervals: Iterable[str] | None = None,
) -> int:
    """Run the EOD fetch for ``as_of_date`` and write ``marker_file`` on success.

    Returns the number of (symbol, interval) partitions written.
    """
    cfg = settings.eod_data_store
    if not cfg.enabled:
        logger.info("eod_data_store disabled; skipping")
        return 0

    root = Path(cfg.store_root)
    manifest = DataStoreManifest(db_path=manifest_db)

    chosen = list(intervals or cfg.intervals)
    written = 0
    any_succeeded = False
    for interval in chosen:
        product = _INTERVAL_TO_PRODUCT.get(interval)
        if product is None:
            logger.warning("unknown interval %s; skipping", interval)
            continue
        key_template = _key_template_for(cfg, product)
        try:
            result = fetch_and_persist_for_day(
                product=product,
                interval=interval,
                as_of_date=as_of_date,
                universe_path=universe_path,
                root=root,
                manifest=manifest,
                cfg=cfg,
                key_template=key_template,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("eod fetch failed for %s on %s: %s", product, as_of_date, exc)
            continue
        written += result.symbols_written
        if result.symbols_written > 0:
            any_succeeded = True
        logger.info(
            "eod fetched product=%s interval=%s date=%s symbols=%d rows=%d",
            product, interval, as_of_date, result.symbols_written, result.rows_written,
        )

    # Only write the marker when at least one interval wrote something.
    # A blank marker is the wrong idempotency signal — it locks out future
    # retries on the same date after a transient failure (e.g. S3 outage).
    if any_succeeded:
        _write_marker_atomic(marker_file, as_of_date.isoformat())
    else:
        logger.warning(
            "eod fetch for %s produced zero partitions; not writing marker",
            as_of_date,
        )
    return written


def _write_marker_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically via tmp + replace."""
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent), text=True
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise