"""Tests for ``trading_bot.analytics.evaluation_windows``.

Each test composes a tiny SQLite ledger, builds the
:class:`EvaluationWindows` snapshot, and asserts the three windows
report distinct, correct, JSON-safe numbers. The legacy $1.27M peak
must never enter the equity cohort, and pre-cohort SELLs must never
enter the trade cohort.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_bot.analytics.evaluation_windows import (
    EvaluationWindows,
    build_evaluation_windows,
    normalize_timestamp,
    _start_of_trading_day_local,
)
from trading_bot.config.settings import (
    AppSettings,
    PaperSettings,
    Settings,
)
from trading_bot.portfolio.ledger import PortfolioLedger


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE equity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            equity REAL NOT NULL,
            cash REAL NOT NULL,
            realized_pnl REAL,
            unrealized_pnl REAL
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            side TEXT,
            quantity INTEGER,
            fill_price REAL,
            fees REAL,
            filled_at TEXT,
            pnl REAL,
            strategy_tag TEXT
        );
        """
    )
    # Legacy $1.27M peak (pre-cohort, must be excluded from equity cohort)
    conn.execute(
        "INSERT INTO equity_history (timestamp, equity, cash) VALUES (?, ?, ?)",
        ("2026-07-10T10:00:00+00:00", 1270000.0, 1270000.0),
    )
    # Cohort equity after graduation
    conn.execute(
        "INSERT INTO equity_history (timestamp, equity, cash) VALUES (?, ?, ?)",
        ("2026-07-15T22:30:00+00:00", 100000.0, 100000.0),
    )
    conn.execute(
        "INSERT INTO equity_history (timestamp, equity, cash) VALUES (?, ?, ?)",
        ("2026-07-22T10:00:00+00:00", 98440.0, 98440.0),
    )
    # Cohort SELLs
    conn.execute(
        "INSERT INTO orders (ticker, side, quantity, fill_price, fees, filled_at, pnl, strategy_tag) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("AAPL", "SELL", 10, 175.0, 1.0, "2026-07-13T15:30:00+00:00", -50.0, "v3-trend"),
    )
    conn.execute(
        "INSERT INTO orders (ticker, side, quantity, fill_price, fees, filled_at, pnl, strategy_tag) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("MSFT", "SELL", 10, 410.0, 1.0, "2026-07-21T15:30:00+00:00", 75.0, "v3-mr"),
    )
    # Pre-cohort SELL (must be excluded from trade cohort)
    conn.execute(
        "INSERT INTO orders (ticker, side, quantity, fill_price, fees, filled_at, pnl, strategy_tag) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("OLD", "SELL", 10, 100.0, 1.0, "2026-07-10T15:30:00+00:00", -1000.0, "legacy"),
    )
    conn.commit()
    conn.close()


def _settings(db: Path, *, equity_eval=None) -> Settings:
    return Settings(
        app=AppSettings(state_db_path=str(db), timezone="America/New_York"),
        paper=PaperSettings(
            graduation_since=datetime(2026, 7, 11, tzinfo=timezone.utc),
            equity_evaluation_since=equity_eval,
        ),
    )


# ---------------------------------------------------------------------
# normalize_timestamp
# ---------------------------------------------------------------------


def test_normalize_timestamp_aware_passthrough() -> None:
    ts = datetime(2026, 7, 22, 19, 30, tzinfo=timezone.utc)
    assert normalize_timestamp(ts, "UTC") == ts


def test_normalize_timestamp_naive_in_configured_timezone() -> None:
    """A naive 10:30 in America/New_York becomes 14:30 UTC."""
    naive = datetime(2026, 7, 22, 10, 30)
    normalized = normalize_timestamp(naive, "America/New_York")
    assert normalized is not None
    assert normalized.tzinfo is not None
    assert normalized.hour == 14
    assert normalized.minute == 30


def test_normalize_timestamp_handles_iso_z_suffix() -> None:
    ts = normalize_timestamp("2026-07-22T19:30:00Z", "UTC")
    assert ts is not None
    assert ts.tzinfo is not None
    assert ts.hour == 19


def test_normalize_timestamp_malformed_returns_none() -> None:
    assert normalize_timestamp("not a date", "UTC") is None
    assert normalize_timestamp(None, "UTC") is None
    assert normalize_timestamp("", "UTC") is None


# ---------------------------------------------------------------------
# start-of-day local
# ---------------------------------------------------------------------


def test_start_of_trading_day_local_converts_to_utc() -> None:
    now_utc = datetime(2026, 7, 22, 19, 30, tzinfo=timezone.utc)
    start_utc = _start_of_trading_day_local(now_utc, "America/New_York")
    # 2026-07-22 00:00 NY (EDT) = 2026-07-22 04:00 UTC
    assert start_utc.hour == 4
    assert start_utc.day == 22


# ---------------------------------------------------------------------
# evaluation_windows payload
# ---------------------------------------------------------------------


def test_build_evaluation_windows_excludes_legacy_peak(tmp_path: Path) -> None:
    db = tmp_path / "burn_in.db"
    _seed_db(db)
    equity_eval = datetime(2026, 7, 15, 22, 29, 45, tzinfo=timezone(timedelta(hours=-4)))
    settings = _settings(db, equity_eval=equity_eval)
    ledger = PortfolioLedger(db)
    now = datetime(2026, 7, 22, 19, 30, tzinfo=timezone.utc)
    windows = build_evaluation_windows(settings, ledger, now=now)

    payload = windows.to_dict()

    # Trade cohort excludes pre-cohort $1k loss.
    assert payload["trade_cohort_metrics"]["closed_exits"] == 2
    assert payload["trade_cohort_metrics"]["realized_pnl"] == 25.0
    assert payload["trade_cohort_metrics"]["profit_factor"] == 1.5

    # Equity cohort is insufficient (only 1 cohort snapshot, need 2).
    assert payload["equity_cohort"]["state"] == "insufficient"
    assert payload["equity_cohort_metrics"]["snapshot_count"] == 1

    # JSON-safe: no NaN or Infinity in any field.
    text = json.dumps(payload)
    assert "NaN" not in text
    assert "Infinity" not in text
    assert "inf" not in text.lower().replace("graduation_fallback", "").replace("infinite", "")


def test_build_evaluation_windows_distinguishes_today_and_cohort(
    tmp_path: Path,
) -> None:
    db = tmp_path / "burn_in.db"
    _seed_db(db)
    settings = _settings(db)
    ledger = PortfolioLedger(db)
    # Set now to 2026-07-13 16:00 UTC: only the AAPL SELL at 15:30 UTC
    # is "today"; the MSFT SELL is in the future so it is excluded from
    # today but still in the cohort.
    now = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)
    windows = build_evaluation_windows(settings, ledger, now=now)
    payload = windows.to_dict()
    assert payload["today_metrics"]["closed_exits"] == 1
    assert payload["today_metrics"]["realized_pnl"] == -50.0
    # Trade cohort keeps both cohort SELLs.
    assert payload["trade_cohort_metrics"]["closed_exits"] == 2


def test_build_evaluation_windows_unconfigured_when_no_boundaries(
    tmp_path: Path,
) -> None:
    db = tmp_path / "burn_in.db"
    _seed_db(db)
    settings = Settings(
        app=AppSettings(state_db_path=str(db), timezone="UTC"),
        paper=PaperSettings(
            graduation_since=None, equity_evaluation_since=None
        ),
    )
    ledger = PortfolioLedger(db)
    windows = build_evaluation_windows(settings, ledger)
    payload = windows.to_dict()
    assert payload["trade_cohort"]["state"] == "unconfigured"
    assert payload["equity_cohort"]["state"] == "unconfigured"


def test_build_evaluation_windows_equity_fallback_to_graduation(
    tmp_path: Path,
) -> None:
    """When only graduation is set, equity cohort uses it and labels
    the source ``graduation_fallback``."""
    db = tmp_path / "burn_in.db"
    _seed_db(db)
    settings = Settings(
        app=AppSettings(state_db_path=str(db), timezone="UTC"),
        paper=PaperSettings(
            graduation_since=datetime(2026, 7, 11, tzinfo=timezone.utc),
            equity_evaluation_since=None,
        ),
    )
    ledger = PortfolioLedger(db)
    windows = build_evaluation_windows(settings, ledger)
    payload = windows.to_dict()
    assert payload["equity_cohort"]["available"] is True
    assert payload["equity_cohort"]["boundary_source"] == "graduation_fallback"
    assert payload["equity_cohort_metrics"]["boundary_source"] == "graduation_fallback"


def test_build_evaluation_windows_sufficient_equity_evidence(
    tmp_path: Path,
) -> None:
    db = tmp_path / "burn_in.db"
    _seed_db(db)
    equity_eval = datetime(2026, 7, 15, 22, 29, 45, tzinfo=timezone(timedelta(hours=-4)))
    settings = _settings(db, equity_eval=equity_eval)
    ledger = PortfolioLedger(db)
    # Add a second cohort equity snapshot so the equity cohort has
    # enough evidence.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO equity_history (timestamp, equity, cash) VALUES (?, ?, ?)",
        ("2026-07-22T14:00:00+00:00", 98500.0, 98500.0),
    )
    conn.commit()
    conn.close()
    now = datetime(2026, 7, 22, 19, 30, tzinfo=timezone.utc)
    windows = build_evaluation_windows(settings, ledger, now=now)
    payload = windows.to_dict()
    assert payload["equity_cohort"]["state"] == "ready"
    assert payload["equity_cohort_metrics"]["snapshot_count"] == 2
    # The 22:30 UTC cohort snapshot is 1 minute BEFORE the 22:29:45-04
    # equity boundary (which equals 02:29:45 UTC the next day), so the
    # first included snapshot is the 22-10:00 one at $98,440.
    assert payload["equity_cohort_metrics"]["starting_equity"] == 98440.0
    assert payload["equity_cohort_metrics"]["current_equity"] == 98500.0


def test_build_evaluation_windows_infinite_pf_serialized(tmp_path: Path) -> None:
    """A cohort with wins but zero losses must report profit_factor=None
    with state='infinite', and the JSON payload must round-trip cleanly
    so the dashboard can render ∞."""
    db = tmp_path / "burn_in.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            side TEXT,
            quantity INTEGER,
            fill_price REAL,
            fees REAL,
            filled_at TEXT,
            pnl REAL,
            strategy_tag TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO orders (ticker, side, quantity, fill_price, fees, filled_at, pnl, strategy_tag) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("A", "SELL", 1, 10.0, 0.0, "2026-07-12T15:30:00+00:00", 50.0, "test"),
            ("B", "SELL", 1, 10.0, 0.0, "2026-07-13T15:30:00+00:00", 75.0, "test"),
            ("C", "SELL", 1, 10.0, 0.0, "2026-07-14T15:30:00+00:00", 25.0, "test"),
        ],
    )
    conn.commit()
    conn.close()

    settings = Settings(
        app=AppSettings(state_db_path=str(db), timezone="UTC"),
        paper=PaperSettings(
            graduation_since=datetime(2026, 7, 11, tzinfo=timezone.utc),
            equity_evaluation_since=None,
        ),
    )
    ledger = PortfolioLedger(db)
    windows = build_evaluation_windows(
        settings, ledger, now=datetime(2026, 7, 22, 19, 30, tzinfo=timezone.utc)
    )
    payload = windows.to_dict()
    text = json.dumps(payload)
    assert "NaN" not in text
    assert "Infinity" not in text
    assert payload["trade_cohort_metrics"]["closed_exits"] == 3
    assert payload["trade_cohort_metrics"]["wins"] == 3
    assert payload["trade_cohort_metrics"]["losses"] == 0
    assert payload["trade_cohort_metrics"]["profit_factor"] is None
    assert payload["trade_cohort_metrics"]["profit_factor_state"] == "infinite"
    assert payload["trade_cohort_metrics"]["realized_pnl"] == 150.0
    assert payload["trade_cohort_metrics"]["average_exit_pnl"] == 50.0


def test_evaluation_windows_dataclass_is_json_safe() -> None:
    """Top-level snapshot must serialize to JSON without errors."""
    windows = EvaluationWindows(generated_at=datetime.now(timezone.utc).isoformat())
    text = json.dumps(windows.to_dict())
    assert isinstance(text, str)
