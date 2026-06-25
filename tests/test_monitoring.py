import pytest
from datetime import datetime, timedelta
from pathlib import Path

from trading_bot.models.portfolio import PortfolioState
from trading_bot.monitoring.performance import (
    PerformanceMetrics,
    calculate_performance_metrics,
    format_performance_report,
)
from trading_bot.monitoring.health import (
    AlertThresholds,
    check_alert_conditions,
    check_system_health,
    format_health_report,
    HealthCheck,
)
from trading_bot.portfolio.ledger import PortfolioLedger


def test_calculate_performance_metrics_empty_ledger(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=10000, equity=10000))

    metrics = calculate_performance_metrics(ledger, days=30)

    assert metrics.total_trades == 0
    assert metrics.net_pnl == 0.0


def test_calculate_performance_metrics_with_trades(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)

    # Record some trades
    from trading_bot.models.order import FillResult

    now = datetime.now()

    # Winning trade: Buy AAPL at 100, sell at 110
    ledger.record_fill(
        FillResult(
            order_id="1",
            ticker="AAPL",
            quantity=10,
            fill_price=100.0,
            fees=1.0,
            filled_at=now - timedelta(days=5),
        ),
        side="BUY",
    )
    ledger.record_fill(
        FillResult(
            order_id="2",
            ticker="AAPL",
            quantity=10,
            fill_price=110.0,
            fees=1.0,
            filled_at=now - timedelta(days=4),
        ),
        side="SELL",
    )

    # Losing trade: Buy TSLA at 200, sell at 190
    ledger.record_fill(
        FillResult(
            order_id="3",
            ticker="TSLA",
            quantity=5,
            fill_price=200.0,
            fees=1.0,
            filled_at=now - timedelta(days=3),
        ),
        side="BUY",
    )
    ledger.record_fill(
        FillResult(
            order_id="4",
            ticker="TSLA",
            quantity=5,
            fill_price=190.0,
            fees=1.0,
            filled_at=now - timedelta(days=2),
        ),
        side="SELL",
    )

    metrics = calculate_performance_metrics(ledger, days=30)

    assert metrics.total_trades == 2
    assert metrics.winning_trades == 1
    assert metrics.losing_trades == 1
    assert metrics.win_rate == 0.5

    # AAPL: (110 - 100) * 10 - 2 fees = 98 profit
    # TSLA: (190 - 200) * 5 - 2 fees = -52 loss
    # Net: 98 - 52 = 46
    assert metrics.net_pnl == 46.0
    assert metrics.gross_profit == 98.0
    assert metrics.gross_loss == 52.0


def test_format_performance_report() -> None:
    metrics = PerformanceMetrics(
        period="last_30_days",
        start_date=datetime.now() - timedelta(days=30),
        end_date=datetime.now(),
        total_trades=10,
        winning_trades=6,
        losing_trades=4,
        win_rate=0.6,
        net_pnl=500.0,
        gross_profit=800.0,
        gross_loss=300.0,
        profit_factor=2.67,
        avg_win=133.33,
        avg_loss=75.0,
        largest_win=250.0,
        largest_loss=-100.0,
    )

    report = format_performance_report(metrics)

    assert "Performance Report" in report
    assert "Total Trades: 10" in report
    assert "Wins: 6" in report
    assert "Wins: 6 (60.0%)" in report
    assert "Net P&L: $500.00" in report


def test_format_performance_report_empty() -> None:
    metrics = PerformanceMetrics(
        period="last_30_days",
        start_date=None,
        end_date=None,
        total_trades=0,
    )

    report = format_performance_report(metrics)

    assert "No trades found" in report


def test_health_check_all_healthy(tmp_path: Path) -> None:
    from trading_bot.config.settings import Settings

    settings = Settings()
    settings.app.state_db_path = str(tmp_path / "state.db")
    settings.app.log_dir = str(tmp_path / "logs")

    db_path = tmp_path / "state.db"
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=10000, equity=10000))

    health = check_system_health(settings, ledger)

    assert health.is_healthy()
    assert health.checks["database"][0] is True
    assert health.checks["state_dir"][0] is True
    assert health.checks["log_dir"][0] is True


def test_health_check_database_failure(tmp_path: Path) -> None:
    from trading_bot.config.settings import Settings

    settings = Settings()
    settings.app.state_db_path = "/nonexistent/path/state.db"
    settings.app.log_dir = str(tmp_path / "logs")

    health = check_system_health(settings, None)

    assert not health.is_healthy()
    assert health.checks["database"][0] is False


def test_format_health_report() -> None:
    health = HealthCheck(
        healthy=True,
        checks={
            "database": (True, "connected"),
            "state_dir": (True, "writable"),
            "log_dir": (False, "permission denied"),
        },
    )

    report = format_health_report(health)

    assert "Health Check Report" in report
    assert "Status: UNHEALTHY" in report
    assert "✓ database: connected" in report
    assert "✗ log_dir: permission denied" in report


def test_check_alert_conditions_no_alerts(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)

    # Add winning trades
    from trading_bot.models.order import FillResult

    now = datetime.now()

    for i in range(5):
        ledger.record_fill(
            FillResult(
                order_id=f"buy{i}",
                ticker="AAPL",
                quantity=10,
                fill_price=100.0,
                fees=1.0,
                filled_at=now - timedelta(days=i + 1),
            ),
            side="BUY",
        )
        ledger.record_fill(
            FillResult(
                order_id=f"sell{i}",
                ticker="AAPL",
                quantity=10,
                fill_price=110.0,
                fees=1.0,
                filled_at=now - timedelta(days=i),
            ),
            side="SELL",
        )

    alerts = check_alert_conditions(ledger)

    assert len(alerts) == 0


def test_check_alert_conditions_win_rate_low(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)

    # Add mostly losing trades
    from trading_bot.models.order import FillResult

    now = datetime.now()

    for i in range(10):
        ledger.record_fill(
            FillResult(
                order_id=f"buy{i}",
                ticker="AAPL",
                quantity=10,
                fill_price=100.0,
                fees=1.0,
                filled_at=now - timedelta(days=i * 2 + 1),
            ),
            side="BUY",
        )
        ledger.record_fill(
            FillResult(
                order_id=f"sell{i}",
                ticker="AAPL",
                quantity=10,
                fill_price=90.0 if i < 8 else 110.0,  # 8 losses, 2 wins
                fees=1.0,
                filled_at=now - timedelta(days=i * 2),
            ),
            side="SELL",
        )

    thresholds = AlertThresholds(min_win_rate=0.40)
    alerts = check_alert_conditions(ledger, thresholds)

    win_rate_alert = [a for a in alerts if a["type"] == "win_rate_low"]
    assert len(win_rate_alert) == 1
    assert win_rate_alert[0]["level"] == "warning"
