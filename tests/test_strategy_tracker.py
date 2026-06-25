from __future__ import annotations

from datetime import datetime
from pathlib import Path

from trading_bot.strategy.strategy_tracker import (
    allocation_multiplier,
    record_entry,
    record_exit,
    rolling_win_rate,
    strategy_summary,
)


def test_record_and_read_entry(tmp_path: Path) -> None:
    record_entry(tmp_path, "v3-trend_following", "AAPL", 150.0, datetime(2026, 6, 1))
    events = strategy_summary(tmp_path)
    assert len(events) == 0  # entries don't count toward summary (exits only)


def test_record_and_read_exit(tmp_path: Path) -> None:
    record_exit(
        tmp_path,
        "v3-trend_following",
        "AAPL",
        entry_price=150.0,
        exit_price=160.0,
        quantity=10,
        fees=1.0,
        pnl=99.0,
        reason="target",
        timestamp=datetime(2026, 6, 5),
    )
    rate = rolling_win_rate(tmp_path, "v3-trend_following", window=20)
    assert rate == 1.0  # 1 win / 1 exit

    summary = strategy_summary(tmp_path)
    assert len(summary) == 1
    assert summary[0]["strategy"] == "v3-trend_following"
    assert summary[0]["recent_exits"] == 1
    assert summary[0]["recent_wins"] == 1


def test_rolling_win_rate_multiple_exits(tmp_path: Path) -> None:
    for i in range(10):
        win = i < 6  # 6 wins, 4 losses
        pnl = 10.0 if win else -10.0
        record_exit(
            tmp_path,
            "daily_breakout_v1",
            "SPY",
            entry_price=400.0,
            exit_price=400.0 + (1.0 if win else -1.0),
            quantity=10,
            fees=1.0,
            pnl=pnl,
            reason="stop" if not win else "target",
            timestamp=datetime(2026, 6, 1 + i),
        )
    rate = rolling_win_rate(tmp_path, "daily_breakout_v1", window=20)
    assert rate == 0.6


def test_allocation_multiplier_insufficient_data(tmp_path: Path) -> None:
    # Only 5 exits, window=20 means insufficient data → full allocation
    for i in range(5):
        record_exit(
            tmp_path,
            "v3-breakout",
            "AAPL",
            entry_price=100.0,
            exit_price=101.0,
            quantity=10,
            fees=1.0,
            pnl=9.0,
            reason="target",
            timestamp=datetime(2026, 6, 1 + i),
        )
    mult = allocation_multiplier(tmp_path, "v3-breakout", window=20)
    assert mult == 1.0


def test_allocation_multiplier_below_min_win_rate(tmp_path: Path) -> None:
    # 20 exits, 5 wins (25%) → below min_win_rate (40%) → allocation 0
    for i in range(20):
        win = i < 5
        pnl = 10.0 if win else -10.0
        record_exit(
            tmp_path,
            "bad_strategy",
            "SPY",
            entry_price=100.0,
            exit_price=100.0 + (1.0 if win else -1.0),
            quantity=10,
            fees=1.0,
            pnl=pnl,
            reason="stop",
            timestamp=datetime(2026, 6, 1 + i),
        )
    mult = allocation_multiplier(tmp_path, "bad_strategy", window=20)
    assert mult == 0.0


def test_allocation_multiplier_half_rate(tmp_path: Path) -> None:
    # 20 exits, 9 wins (45%) → between min (40%) and full (50%) → half
    for i in range(20):
        win = i < 9
        pnl = 10.0 if win else -10.0
        record_exit(
            tmp_path,
            "mid_strategy",
            "AAPL",
            entry_price=100.0,
            exit_price=100.0 + (1.0 if win else -1.0),
            quantity=10,
            fees=1.0,
            pnl=pnl,
            reason="target" if win else "stop",
            timestamp=datetime(2026, 6, 1 + i),
        )
    mult = allocation_multiplier(tmp_path, "mid_strategy", window=20)
    assert mult == 0.5


def test_allocation_multiplier_full_rate(tmp_path: Path) -> None:
    # 20 exits, 12 wins (60%) → above full_allocation_rate (50%) → full
    for i in range(20):
        win = i < 12
        pnl = 10.0 if win else -10.0
        record_exit(
            tmp_path,
            "good_strategy",
            "MSFT",
            entry_price=200.0,
            exit_price=200.0 + (1.0 if win else -1.0),
            quantity=10,
            fees=1.0,
            pnl=pnl,
            reason="target" if win else "stop",
            timestamp=datetime(2026, 6, 1 + i),
        )
    mult = allocation_multiplier(tmp_path, "good_strategy", window=20)
    assert mult == 1.0


def test_strategy_summary_multiple_strategies(tmp_path: Path) -> None:
    for tag in ("strat_a", "strat_b"):
        for i in range(10):
            win = i < 7
            pnl = 10.0 if win else -10.0
            record_exit(
                tmp_path,
                tag,
                "SPY",
                entry_price=100.0,
                exit_price=100.0 + (1.0 if win else -1.0),
                quantity=10,
                fees=1.0,
                pnl=pnl,
                reason="target" if win else "stop",
                timestamp=datetime(2026, 6, 1 + i),
            )
    summary = strategy_summary(tmp_path, window=20)
    assert len(summary) == 2
    names = [r["strategy"] for r in summary]
    assert "strat_a" in names
    assert "strat_b" in names


def test_unknown_strategy_win_rate(tmp_path: Path) -> None:
    rate = rolling_win_rate(tmp_path, "nonexistent", window=20)
    assert rate == 0.0
