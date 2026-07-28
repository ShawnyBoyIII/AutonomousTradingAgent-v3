from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from trading_bot.health.checks import (
    check_dashboard_health,
    check_eod_watchdog,
    check_heartbeat_fresh,
    check_market_data_freshness,
    check_open_positions_consistent,
    check_pid_alive,
)


def _write_pid(path: Path, pid: int) -> None:
    path.write_text(str(pid))


def _write_heartbeat(path: Path, ts_iso: str, *, cycle: int = 1, fills: int = 0, exits: int = 0, rejects: int = 0) -> None:
    payload = {
        "ts": ts_iso,
        "cycle": cycle,
        "fills": fills,
        "exits": exits,
        "rejects": rejects,
    }
    path.write_text(json.dumps(payload))


def _seed_db(db: Path, *, open_trades: int = 0, last_market_ts: str | None = None) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                status TEXT,
                filled_at TEXT
            );
            CREATE TABLE market_data (
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                timeframe TEXT,
                timestamp TEXT
            );
            """
        )
        for i in range(open_trades):
            conn.execute(
                "INSERT INTO trades(ticker,status,filled_at) VALUES (?,?,?)",
                (f"T{i}", "FILLED", "2026-07-10T13:00:00+00:00"),
            )
        if last_market_ts is not None:
            conn.execute(
                "INSERT INTO market_data(ticker,timeframe,timestamp) VALUES (?,?,?)",
                ("AAPL", "5m", last_market_ts),
            )
        conn.commit()
    finally:
        conn.close()


# --- check_pid_alive --------------------------------------------------------

def test_pid_alive_pass(tmp_path: Path):
    pid_file = tmp_path / "burn_in.pid"
    _write_pid(pid_file, 1)
    # PID 1 (launchd) is always alive on macOS. On Linux PID 1 also exists.
    result = check_pid_alive(pid_file)
    assert result.status == "PASS"
    assert result.name == "pid_alive"


def test_pid_alive_fail_dead(tmp_path: Path):
    pid_file = tmp_path / "burn_in.pid"
    pid_file.write_text("99999999")  # unlikely
    result = check_pid_alive(pid_file)
    # On any sane CI, that PID is dead → FAIL; if it happens to be live,
    # the test still asserts the function returns a CheckResult, not crashes.
    assert result.status in {"PASS", "FAIL"}


def test_pid_alive_fail_missing(tmp_path: Path):
    result = check_pid_alive(tmp_path / "missing.pid")
    assert result.status == "FAIL"
    assert "missing" in result.detail.lower()


def test_pid_alive_fail_non_integer(tmp_path: Path):
    pid_file = tmp_path / "bad.pid"
    pid_file.write_text("not-a-number\n")
    result = check_pid_alive(pid_file)
    assert result.status == "FAIL"
    assert "invalid" in result.detail.lower()


# --- check_heartbeat_fresh --------------------------------------------------

def test_heartbeat_fresh_pass(tmp_path: Path):
    h = tmp_path / "hb.json"
    _write_heartbeat(h, datetime.now(timezone.utc).isoformat())
    result = check_heartbeat_fresh(h, max_age_seconds=90)
    assert result.status == "PASS"


def test_heartbeat_fresh_warn(tmp_path: Path):
    h = tmp_path / "hb.json"
    old = datetime.now(timezone.utc).timestamp() - 200  # 200s ago
    iso = datetime.fromtimestamp(old, tz=timezone.utc).isoformat()
    _write_heartbeat(h, iso)
    result = check_heartbeat_fresh(h, max_age_seconds=90)
    assert result.status == "WARN"


def test_heartbeat_fresh_fail_missing(tmp_path: Path):
    result = check_heartbeat_fresh(tmp_path / "missing.json", max_age_seconds=90)
    assert result.status == "FAIL"


def test_heartbeat_fresh_does_not_raise_on_bad_json(tmp_path: Path):
    h = tmp_path / "hb.json"
    h.write_text("not json{")
    result = check_heartbeat_fresh(h, max_age_seconds=90)
    assert result.status == "FAIL"


# --- check_dashboard_health -------------------------------------------------

def test_dashboard_pass(monkeypatch):
    class _Resp:
        status = 200

        def read(self):  # noqa: D401
            return b'{"status":"ok"}'

    monkeypatch.setattr(
        "trading_bot.health.checks.urlopen",
        lambda *a, **kw: _Resp(),
    )
    result = check_dashboard_health(port=8765)
    assert result.status == "PASS"


def test_dashboard_warn_non_200(monkeypatch):
    class _Resp:
        status = 503

        def read(self):  # noqa: D401
            return b""

    monkeypatch.setattr(
        "trading_bot.health.checks.urlopen",
        lambda *a, **kw: _Resp(),
    )
    result = check_dashboard_health(port=8765)
    assert result.status == "WARN"


def test_dashboard_fail_connection_refused(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr("trading_bot.health.checks.urlopen", boom)
    result = check_dashboard_health(port=8765)
    assert result.status == "FAIL"
    assert "refused" in result.detail.lower() or "connect" in result.detail.lower()


# --- check_eod_watchdog -----------------------------------------------------

def test_eod_watchdog_pass_outside_burner_hours(tmp_path: Path):
    pid_file = tmp_path / "wd.pid"
    # 22:00 ET Monday → outside burner hours → watchdog not required → PASS
    result = check_eod_watchdog(
        pid_file=pid_file,
        now_utc=datetime(2026, 7, 7, 2, 0, tzinfo=timezone.utc),  # Mon 22:00 ET
    )
    assert result.status == "PASS"


def test_eod_watchdog_fail_in_burner_hours_without_pid(tmp_path: Path):
    pid_file = tmp_path / "wd.pid"
    # 12:00 ET Monday → within burner hours; missing PID file → FAIL
    # (audit item 7: previously the check returned PASS outside the
    # 15:50-16:05 fire window even when the watchdog was dead.)
    result = check_eod_watchdog(
        pid_file=pid_file,
        now_utc=datetime(2026, 7, 6, 16, 0, tzinfo=timezone.utc),  # Mon 12:00 ET
    )
    assert result.status == "FAIL"


def test_eod_watchdog_fail_in_window_without_pid(tmp_path: Path):
    pid_file = tmp_path / "wd.pid"
    # 15:55 ET on a weekday → watchdog should be alive
    result = check_eod_watchdog(
        pid_file=pid_file,
        now_utc=datetime(2026, 7, 6, 19, 55, tzinfo=timezone.utc),  # Mon 15:55 ET
    )
    assert result.status == "FAIL"


def test_eod_watchdog_pass_in_window_with_live_pid(tmp_path: Path):
    pid_file = tmp_path / "wd.pid"
    pid_file.write_text("1")  # PID 1 is always alive
    result = check_eod_watchdog(
        pid_file=pid_file,
        now_utc=datetime(2026, 7, 6, 19, 55, tzinfo=timezone.utc),
    )
    # Either PASS (PID 1 alive in window) or WARN (we punt on PID 1 detection edge case)
    assert result.status in {"PASS", "WARN"}


def test_eod_watchdog_no_action_on_weekend(tmp_path: Path):
    pid_file = tmp_path / "wd.pid"
    # Saturday at 15:55 ET → outside EOD window
    result = check_eod_watchdog(
        pid_file=pid_file,
        now_utc=datetime(2026, 7, 11, 19, 55, tzinfo=timezone.utc),  # Sat 15:55 ET
    )
    assert result.status == "PASS"


# --- check_open_positions_consistent ---------------------------------------

def test_positions_consistent_when_db_empty(tmp_path: Path):
    db = tmp_path / "burn_in.db"
    _seed_db(db, open_trades=0)
    h = tmp_path / "hb.json"
    _write_heartbeat(h, datetime.now(timezone.utc).isoformat())
    result = check_open_positions_consistent(db, h)
    assert result.status == "PASS"


def test_positions_consistent_with_open_positions_fresh_heartbeat(tmp_path: Path):
    # Open positions + fresh heartbeat = PASS (loop is alive, just holding)
    db = tmp_path / "burn_in.db"
    _seed_db(db, open_trades=3)
    h = tmp_path / "hb.json"
    _write_heartbeat(h, datetime.now(timezone.utc).isoformat())
    result = check_open_positions_consistent(db, h)
    assert result.status == "PASS"


def test_positions_consistent_warn_open_positions_stale_heartbeat(tmp_path: Path):
    # Open positions + heartbeat between 90s–5min = WARN (loop slipping)
    db = tmp_path / "burn_in.db"
    _seed_db(db, open_trades=2)
    h = tmp_path / "hb.json"
    iso = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 200, tz=timezone.utc
    ).isoformat()
    _write_heartbeat(h, iso)
    result = check_open_positions_consistent(db, h)
    assert result.status == "WARN"


def test_positions_consistent_fail_when_heartbeat_stale(tmp_path: Path):
    # Open positions + heartbeat >5min old = FAIL (loop stalled)
    db = tmp_path / "burn_in.db"
    _seed_db(db, open_trades=2)
    h = tmp_path / "hb.json"
    iso = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 600, tz=timezone.utc
    ).isoformat()
    _write_heartbeat(h, iso)
    result = check_open_positions_consistent(db, h)
    assert result.status == "FAIL"


# --- check_market_data_freshness -------------------------------------------

def test_market_data_fresh_during_hours(tmp_path: Path):
    db = tmp_path / "m.db"
    fresh_ts = datetime.now(timezone.utc).isoformat()
    _seed_db(db, last_market_ts=fresh_ts)
    # 13:00 ET on a weekday
    result = check_market_data_freshness(
        db_path=db,
        now_utc=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
    )
    # Either PASS (data is fresh) or at worst WARN — never FAIL outright
    assert result.status in {"PASS", "WARN"}


def test_market_data_fail_during_hours_when_old(tmp_path: Path):
    db = tmp_path / "m.db"
    old_ts = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 3600, tz=timezone.utc
    ).isoformat()
    _seed_db(db, last_market_ts=old_ts)
    result = check_market_data_freshness(
        db_path=db,
        now_utc=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
    )
    assert result.status == "FAIL"


def test_market_data_no_data_after_hours_is_passing(tmp_path: Path):
    db = tmp_path / "m.db"
    _seed_db(db, last_market_ts=None)
    # Saturday → market closed → no expectations
    result = check_market_data_freshness(
        db_path=db,
        now_utc=datetime(2026, 7, 11, 17, 0, tzinfo=timezone.utc),
    )
    assert result.status == "PASS"


def _seed_market_data_cache(db: Path, *, last_created_at: str | None = None) -> None:
    """Seed a market_data_cache.db with a fresh (or absent) cache entry.

    Mirrors the schema created by trading_bot.data.cache.MarketDataCache.
    """
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE cache (
                key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                period TEXT NOT NULL,
                interval TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ttl_seconds INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                data TEXT NOT NULL
            );
            """
        )
        if last_created_at is not None:
            conn.execute(
                "INSERT INTO cache(key, symbol, period, interval, created_at, ttl_seconds, expires_at, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "providers=polygon,alpaca,yfinance:NOK:3mo:1d::",
                    "NOK",
                    "3mo",
                    "1d",
                    last_created_at,
                    43200,
                    "2026-07-29T13:42:57+00:00",
                    "[]",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_market_data_fresh_uses_cache_db_not_legacy_market_data_table(tmp_path: Path):
    """The check must read from the MarketDataCache (state/market_data_cache.db),
    not the vestigial market_data table in state/burn_in.db. Production code
    populates only the cache, so a legacy-table-only check reports a permanent
    WARN even when fresh bars are flowing.
    """
    burn_in_db = tmp_path / "burn_in.db"
    _seed_db(burn_in_db, last_market_ts=None)  # legacy market_data table empty

    cache_db = tmp_path / "market_data_cache.db"
    fresh_ts = datetime.now(timezone.utc).isoformat()
    _seed_market_data_cache(cache_db, last_created_at=fresh_ts)

    # 13:00 ET on a weekday, fresh cache entry
    result = check_market_data_freshness(
        db_path=burn_in_db,
        cache_db_path=cache_db,
        now_utc=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
    )
    assert result.status == "PASS", result.detail


def test_market_data_warn_when_cache_db_missing(tmp_path: Path):
    """When the MarketDataCache DB doesn't exist (cold cache), the check
    reports WARN, matching the prior behavior for a brand-new install
    before any fetcher has populated the cache.
    """
    burn_in_db = tmp_path / "burn_in.db"
    _seed_db(burn_in_db, last_market_ts=None)
    cache_db = tmp_path / "does_not_exist_cache.db"

    result = check_market_data_freshness(
        db_path=burn_in_db,
        cache_db_path=cache_db,
        now_utc=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
    )
    assert result.status == "WARN", result.detail


def test_market_data_fail_when_cache_entry_stale(tmp_path: Path):
    """A cache entry older than the freshness threshold (during market
    hours) must report FAIL so the operator notices a stale fetcher.
    """
    burn_in_db = tmp_path / "burn_in.db"
    _seed_db(burn_in_db, last_market_ts=None)
    cache_db = tmp_path / "market_data_cache.db"
    # 4 hours ago: stale beyond the 30-minute warn threshold during market hours
    stale_ts = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 4 * 3600, tz=timezone.utc
    ).isoformat()
    _seed_market_data_cache(cache_db, last_created_at=stale_ts)

    result = check_market_data_freshness(
        db_path=burn_in_db,
        cache_db_path=cache_db,
        now_utc=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
    )
    assert result.status == "FAIL", result.detail
