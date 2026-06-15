from trading_bot.reports.summaries import build_daily_summary


def test_build_daily_summary_returns_expected_metrics() -> None:
    summary = build_daily_summary(
        realized_pnl=125.5,
        unrealized_pnl=40.0,
        open_positions=2,
    )

    assert summary == {
        "realized_pnl": 125.5,
        "unrealized_pnl": 40.0,
        "open_positions": 2,
        "net_pnl": 165.5,
    }
