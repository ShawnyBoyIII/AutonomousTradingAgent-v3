"""Tests for ``trading_bot.analytics.paper_performance``.

These tests pin the multi-dimensional P&L view the operator needs to
debug why PF < 1.3 on a given day. They read straight SQL out of the
burn-in DB or a fixture, so the analytics path is deterministic and
network-free (per AGENTS.md safety contract).
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlite3

from typer.testing import CliRunner

from trading_bot.analytics.paper_performance import (
    PaperPerformanceReport,
    format_paper_performance_report,
    summarize_paper_performance,
)
from trading_bot.cli.app import app


def _fill_orders_table(conn: sqlite3.Connection) -> None:
    """Build a tiny two-day trade fixture covering two strategies."""
    rows = [
        # Day 1: trend strategy wins/loses alternating
        ("o1", "AAA", "BUY", 10, 100.0, "2026-07-10T09:32:00", 0.0, "v3-trend_following"),
        ("o2", "BBB", "BUY", 5, 200.0, "2026-07-10T09:33:00", 0.0, "v3-mean_reversion"),
        ("o3", "AAA", "SELL", 10, 110.0, "2026-07-10T10:10:00", 100.0, "v3-trend_following"),
        ("o4", "BBB", "SELL", 5, 180.0, "2026-07-10T10:11:00", -100.0, "v3-mean_reversion"),
        # Day 1 evening: mean_reversion stops twice
        ("o5", "CCC", "BUY", 20, 50.0, "2026-07-10T14:00:00", 0.0, "v3-mean_reversion"),
        ("o6", "CCC", "SELL", 20, 51.0, "2026-07-10T14:30:00", 20.0, "v3-mean_reversion"),
        ("o7", "DDD", "BUY", 100, 10.0, "2026-07-10T15:00:00", 0.0, "v3-mean_reversion"),
        ("o8", "DDD", "SELL", 100, 9.5, "2026-07-10T15:10:00", -50.0, "v3-mean_reversion"),
    ]
    conn.executemany(
        """
        INSERT INTO orders (id, ticker, side, quantity, fill_price, fees, filled_at, pnl, strategy_tag)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(r[0], r[1], r[2], r[3], r[4], 1.0, r[5], r[6], r[7]) for r in rows],
    )
    conn.commit()


def _seed_burn_in_db(tmp_path) -> str:
    db = tmp_path / "burn_in.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE orders (
            id TEXT PRIMARY KEY,
            ticker TEXT,
            side TEXT,
            quantity INTEGER,
            fill_price REAL,
            fees REAL,
            filled_at TEXT,
            pnl REAL DEFAULT 0,
            strategy_tag TEXT DEFAULT ''
        );
        """
    )
    _fill_orders_table(conn)
    conn.close()
    return str(db)


def test_summarize_paper_performance_returns_strategy_hour_and_ticker_views(tmp_path) -> None:
    db_path = _seed_burn_in_db(tmp_path)
    report: PaperPerformanceReport = summarize_paper_performance(db_path=db_path)

    # Overall aggregate
    assert report.total_trades == 4
    assert report.winning_trades == 2
    assert report.losing_trades == 2
    assert round(report.realized_pnl, 2) == -30.0
    assert report.gross_wins == 120.0
    assert report.gross_losses == 150.0
    assert round(report.profit_factor, 4) == round(120.0 / 150.0, 4)

    # Strategy view: trend wins 100, mean_reversion loses (o4=-100 + o8=-50 + o6=20 = -130)
    by_strategy = {row.label: row for row in report.by_strategy}
    assert set(by_strategy) == {"v3-trend_following", "v3-mean_reversion"}
    assert by_strategy["v3-trend_following"].trades == 1
    assert by_strategy["v3-trend_following"].net_pnl == 100.0
    assert by_strategy["v3-mean_reversion"].trades == 3
    assert by_strategy["v3-mean_reversion"].net_pnl == -130.0

    # Hour view: hour-10 had the cluster (o3 +$100 AAA win pairs with o4 -$100 BBB loss)
    by_hour = {int(row.label): row for row in report.by_hour}
    assert by_hour[10].trades == 2
    assert by_hour[10].wins == 1
    assert by_hour[10].losses == 1
    assert by_hour[10].gross_wins == 100.0
    assert by_hour[10].gross_losses == 100.0
    assert by_hour[10].net_pnl == 0.0
    assert by_hour[14].net_pnl == 20.0
    assert by_hour[15].net_pnl == -50.0

    # Ticker view: AAA +100, BBB -100, CCC +20, DDD -50
    by_ticker = {row.label: row for row in report.by_ticker}
    assert by_ticker["AAA"].net_pnl == 100.0
    assert by_ticker["BBB"].net_pnl == -100.0
    assert by_ticker["DDD"].net_pnl == -50.0


def test_summarize_paper_performance_empty_db_returns_zero_report(tmp_path) -> None:
    db = tmp_path / "burn_in.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE orders (
            id TEXT PRIMARY KEY,
            ticker TEXT,
            side TEXT,
            quantity INTEGER,
            fill_price REAL,
            fees REAL,
            filled_at TEXT,
            pnl REAL DEFAULT 0,
            strategy_tag TEXT DEFAULT ''
        );
        """
    )
    conn.close()

    report = summarize_paper_performance(db_path=str(db))
    assert report.total_trades == 0
    assert report.winning_trades == 0
    assert report.losing_trades == 0
    assert report.realized_pnl == 0.0
    assert report.profit_factor == 0.0
    assert report.by_strategy == []
    assert report.by_hour == []
    assert report.by_ticker == []
    assert report.evaluation_window.start is None
    assert report.evaluation_window.end is None


def test_summarize_paper_performance_filters_to_window(tmp_path) -> None:
    db_path = _seed_burn_in_db(tmp_path)
    report = summarize_paper_performance(
        db_path=db_path,
        since=datetime(2026, 7, 10, 14, 0, 0, tzinfo=timezone.utc),
        until=datetime(2026, 7, 10, 15, 30, 0, tzinfo=timezone.utc),
    )
    # Window keeps only the o5-o8 cluster
    assert report.total_trades == 2
    assert round(report.realized_pnl, 2) == -30.0  # +20 -50

    by_hour = {int(row.label): row for row in report.by_hour}
    assert set(by_hour.keys()) == {14, 15}


def test_format_paper_performance_report_shows_top_losers_and_strategy_table(tmp_path) -> None:
    db_path = _seed_burn_in_db(tmp_path)
    report = summarize_paper_performance(db_path=db_path)
    text = format_paper_performance_report(report)
    # Required sections
    assert "PAPER PERFORMANCE" in text
    assert "Overall" in text
    assert "PF=" in text
    assert "By strategy" in text
    assert "By hour" in text
    assert "Top 5 losers" in text

    # Worst loser surfaced as the headline
    assert "DDD" in text
    assert "v3-mean_reversion" in text
    assert "-50" in text or "-50.0" in text


def _write_graduation_config(tmp_path, db_path: str, graduation_since: str) -> str:
    config = tmp_path / "config.yaml"
    config.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "paper:\n"
        f"  graduation_since: '{graduation_since}'\n",
        encoding="utf-8",
    )
    return str(config)


def test_graduation_check_uses_configured_evidence_cohort(tmp_path) -> None:
    db_path = _seed_burn_in_db(tmp_path)
    config_path = _write_graduation_config(
        tmp_path,
        db_path,
        "2026-07-10T14:00:00+00:00",
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", config_path, "graduation-check"],
    )

    assert result.exit_code == 1
    assert "2026-07-10T14:00:00+00:00" in result.stdout
    assert "Overall: trades=2" in result.stdout
    assert "only 2/100 closed trades" in result.stdout


def test_graduation_check_explicit_since_overrides_configured_cohort(tmp_path) -> None:
    db_path = _seed_burn_in_db(tmp_path)
    config_path = _write_graduation_config(
        tmp_path,
        db_path,
        "2026-07-10T14:00:00+00:00",
    )

    result = CliRunner().invoke(
        app,
        [
            "--config-path",
            config_path,
            "graduation-check",
            "--since",
            "2026-07-10T15:00:00+00:00",
        ],
    )

    assert result.exit_code == 1
    assert "2026-07-10T15:00:00+00:00" in result.stdout
    assert "Overall: trades=1" in result.stdout
    assert "only 1/100 closed trades" in result.stdout
