from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_bot.config.settings import Settings
    from trading_bot.portfolio.ledger import PortfolioLedger


@dataclass
class HealthCheck:
    """Health check result."""

    healthy: bool
    checks: dict[str, tuple[bool, str]] = field(default_factory=dict)

    def is_healthy(self) -> bool:
        return self.healthy and all(status for status, _ in self.checks.values())


@dataclass
class AlertThresholds:
    """Alert thresholds for monitoring."""

    min_win_rate: float = 0.40
    max_sharpe_drop: float = 0.5
    max_consecutive_losses: int = 5
    max_daily_loss_pct: float = 0.03
    min_profit_factor: float = 1.0


def check_system_health(
    settings: Settings,
    ledger: PortfolioLedger | None = None,
) -> HealthCheck:
    """Perform comprehensive health check.

    Checks:
    1. Database connectivity
    2. State directory writable
    3. Log directory writable
    4. Recent trade activity (not stuck)
    5. No excessive recent losses
    """
    checks = {}

    # Check 1: Database connectivity
    checks["database"] = _check_database(ledger, settings)

    # Check 2: State directory
    checks["state_dir"] = _check_directory(Path(settings.app.state_db_path).parent)

    # Check 3: Log directory
    checks["log_dir"] = _check_directory(Path(settings.app.log_dir))

    # Check 4: Recent activity
    checks["recent_activity"] = _check_recent_activity(ledger)

    # Check 5: Performance degradation
    checks["performance"] = _check_performance(ledger)

    healthy = all(status for status, _ in checks.values())
    return HealthCheck(healthy=healthy, checks=checks)


def _check_database(ledger, settings) -> tuple[bool, str]:
    """Check database connectivity."""
    try:
        if ledger is None:
            from trading_bot.portfolio.ledger import PortfolioLedger
            ledger = PortfolioLedger(Path(settings.app.state_db_path))
        ledger.ensure_portfolio_state()
        return True, "connected"
    except Exception as e:
        return False, f"error: {e}"


def _check_directory(path: Path) -> tuple[bool, str]:
    """Check if directory is writable."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".health_check"
        test_file.touch()
        test_file.unlink()
        return True, "writable"
    except Exception as e:
        return False, f"error: {e}"


def _check_recent_activity(ledger) -> tuple[bool, str]:
    """Check if there's been recent trade activity."""
    if ledger is None:
        return True, "no ledger"

    try:
        rows = ledger.list_order_rows()
        if not rows:
            return True, "no trades yet"

        # Check last trade time
        last_trade = datetime.fromisoformat(rows[-1]["filled_at"])
        hours_since = (datetime.now() - last_trade).total_seconds() / 3600

        if hours_since > 48:
            return False, f"no trades for {hours_since:.0f} hours"

        return True, f"last trade {hours_since:.1f} hours ago"
    except Exception as e:
        return False, f"error: {e}"


def _check_performance(ledger) -> tuple[bool, str]:
    """Check for performance degradation."""
    if ledger is None:
        return True, "no ledger"

    try:
        from trading_bot.monitoring.performance import calculate_performance_metrics

        metrics = calculate_performance_metrics(ledger, days=7)

        if metrics.total_trades == 0:
            return True, "no trades in last 7 days"

        issues = []

        if metrics.win_rate < 0.40:
            issues.append(f"win rate {metrics.win_rate:.1%} < 40%")

        if metrics.profit_factor < 1.0:
            issues.append(f"profit factor {metrics.profit_factor:.2f} < 1.0")

        if metrics.max_consecutive_losses >= 5:
            issues.append(f"{metrics.max_consecutive_losses} consecutive losses")

        if issues:
            return False, "; ".join(issues)

        return True, f"{metrics.total_trades} trades, {metrics.win_rate:.1%} win rate"
    except Exception as e:
        return False, f"error: {e}"


def check_alert_conditions(
    ledger: PortfolioLedger,
    thresholds: AlertThresholds | None = None,
) -> list[dict]:
    """Check for alert conditions.

    Returns list of active alerts.
    """
    thresholds = thresholds or AlertThresholds()
    alerts = []

    from trading_bot.monitoring.performance import calculate_performance_metrics

    # Check recent performance
    metrics = calculate_performance_metrics(ledger, days=7)

    if metrics.total_trades > 0:
        # Win rate alert
        if metrics.win_rate < thresholds.min_win_rate:
            alerts.append({
                "level": "warning",
                "type": "win_rate_low",
                "message": f"Win rate {metrics.win_rate:.1%} below threshold {thresholds.min_win_rate:.1%}",
                "value": metrics.win_rate,
                "threshold": thresholds.min_win_rate,
            })

        # Profit factor alert
        if metrics.profit_factor < thresholds.min_profit_factor:
            alerts.append({
                "level": "critical",
                "type": "profit_factor_low",
                "message": f"Profit factor {metrics.profit_factor:.2f} below threshold {thresholds.min_profit_factor:.2f}",
                "value": metrics.profit_factor,
                "threshold": thresholds.min_profit_factor,
            })

        # Consecutive losses alert
        if metrics.max_consecutive_losses >= thresholds.max_consecutive_losses:
            alerts.append({
                "level": "critical",
                "type": "consecutive_losses",
                "message": f"{metrics.max_consecutive_losses} consecutive losses",
                "value": metrics.max_consecutive_losses,
                "threshold": thresholds.max_consecutive_losses,
            })

    return alerts


def format_health_report(health: HealthCheck) -> str:
    """Format health check as readable report."""
    lines = [
        "Health Check Report",
        f"Status: {'HEALTHY' if health.is_healthy() else 'UNHEALTHY'}",
        "",
        "Component Checks:",
    ]

    for component, (status, message) in health.checks.items():
        status_str = "✓" if status else "✗"
        lines.append(f"  {status_str} {component}: {message}")

    return "\n".join(lines)
