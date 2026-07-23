"""Tests for the automated circuit breaker system (Phase 1 safety)."""

from __future__ import annotations

from pathlib import Path

import pytest

from datetime import datetime

from trading_bot.config.settings import AppSettings, MonitoringSettings, RiskSettings, Settings
from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.safety.circuit_breaker import check_circuit_breakers
from trading_bot.safety.kill_switch import is_trading_halted


def _settings() -> Settings:
    return Settings(
        app=AppSettings(
            state_db_path="state/test.db",
            log_dir="logs",
            dashboard_summary_path="state/dashboard.json",
            scan_results_path="state/scan.json",
            portfolio_summary_path="state/portfolio.json",
            backtest_summary_path="state/backtest.json",
        ),
        risk=RiskSettings(
            max_consecutive_losses=3,
            enable_drawdown_circuit_breaker=True,
        ),
        monitoring=MonitoringSettings(max_drawdown_pct=10.0),
    )


import itertools

_order_counter = itertools.count()

def _record_sell(ledger: PortfolioLedger, pnl: float) -> None:
    fill = FillResult(
        order_id=f"test-{next(_order_counter)}",
        ticker="TEST",
        quantity=1,
        fill_price=100.0,
        fees=1.0,
        filled_at=datetime.now(),
    )
    ledger.record_fill(fill, side="SELL", realized_pnl=pnl)


def _record_buy(ledger: PortfolioLedger) -> None:
    fill = FillResult(
        order_id=f"buy-{next(_order_counter)}",
        ticker="TEST",
        quantity=1,
        fill_price=100.0,
        fees=1.0,
        filled_at=datetime.now(),
    )
    ledger.record_fill(fill, side="BUY")


class TestLedgerConsecutiveLosses:
    """``PortfolioLedger.get_consecutive_losses`` counts recent SELL losses."""

    def test_empty_returns_zero(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        assert ledger.get_consecutive_losses() == 0

    def test_all_losses_counted(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        for pnl in [-10.0, -5.0, -2.0]:
            _record_sell(ledger, pnl)
        assert ledger.get_consecutive_losses() == 3

    def test_stops_at_first_win(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        for pnl in [-10.0, -5.0, 2.0, -3.0]:
            _record_sell(ledger, pnl)
        assert ledger.get_consecutive_losses() == 1  # last is -3.0

    def test_win_resets_count(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        for pnl in [-10.0, 5.0, -2.0, -3.0]:
            _record_sell(ledger, pnl)
        assert ledger.get_consecutive_losses() == 2  # last two are losses

    def test_ignores_buy_orders(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "test.db")
        _record_buy(ledger)
        _record_sell(ledger, -5.0)
        assert ledger.get_consecutive_losses() == 1

    def test_break_even_does_not_count_as_loss(self, tmp_path: Path) -> None:
        """Breakeven (pnl == 0) does NOT count as a loss — only actual losses do."""
        ledger = PortfolioLedger(tmp_path / "test.db")
        _record_sell(ledger, 0.0)
        _record_sell(ledger, -5.0)
        # breakeven resets the counter; only the -5.0 loss counts
        assert ledger.get_consecutive_losses() == 1

    def test_multiple_break_evens_then_loss(self, tmp_path: Path) -> None:
        """Multiple breakevens followed by a loss counts only the loss."""
        ledger = PortfolioLedger(tmp_path / "test.db")
        _record_sell(ledger, 0.0)
        _record_sell(ledger, 0.0)
        _record_sell(ledger, -10.0)
        assert ledger.get_consecutive_losses() == 1


class TestCircuitBreaker:
    """`check_circuit_breakers` auto-halts on thresholds."""

    def _make_ledger(self, tmp_path: Path) -> PortfolioLedger:
        ledger = PortfolioLedger(tmp_path / "test.db")
        ledger.ensure_portfolio_state()
        return ledger

    def _record_equity(self, ledger: PortfolioLedger, equity: float) -> None:
        state = PortfolioState(cash=equity, equity=equity)
        ledger.save_portfolio_state(state)
        ledger.record_equity_snapshot(state)

    def test_below_thresholds_allows_trading(self, tmp_path: Path) -> None:
        settings = _settings()
        ledger = self._make_ledger(tmp_path)

        allowed, reason = check_circuit_breakers(ledger, settings)

        assert allowed is True
        assert reason is None
        assert is_trading_halted(ledger).enabled is False

    def test_consecutive_losses_halt(self, tmp_path: Path) -> None:
        """3 consecutive losses triggers the breaker (threshold=3)."""
        settings = _settings()
        ledger = self._make_ledger(tmp_path)

        for pnl in [-10.0, -5.0, -2.0]:
            _record_sell(ledger, pnl)

        allowed, reason = check_circuit_breakers(ledger, settings)

        assert allowed is False
        assert "circuit breaker" in reason
        assert "3 consecutive losses" in reason
        assert is_trading_halted(ledger).enabled is True

    def test_two_losses_does_not_halt(self, tmp_path: Path) -> None:
        """2 consecutive losses is under the threshold."""
        settings = _settings()
        ledger = self._make_ledger(tmp_path)

        for pnl in [-10.0, -5.0]:
            _record_sell(ledger, pnl)

        allowed, reason = check_circuit_breakers(ledger, settings)

        assert allowed is True
        assert reason is None

    def test_drawdown_halt(self, tmp_path: Path) -> None:
        """Max drawdown >= 10% triggers the breaker."""
        settings = _settings()
        ledger = self._make_ledger(tmp_path)

        # Build equity history: 10,000 → 9,000 (10% drawdown)
        self._record_equity(ledger, 10_000.0)
        self._record_equity(ledger, 8_999.0)  # >10% drawdown

        allowed, reason = check_circuit_breakers(ledger, settings)

        assert allowed is False
        assert "circuit breaker" in reason
        assert "drawdown" in reason
        assert "10.0" in reason
        assert is_trading_halted(ledger).enabled is True

    def test_drawdown_disabled_does_not_halt(self, tmp_path: Path) -> None:
        """When enable_drawdown_circuit_breaker=False, only consecutive-loss check runs."""
        settings = _settings()
        settings.risk.enable_drawdown_circuit_breaker = False
        ledger = self._make_ledger(tmp_path)

        self._record_equity(ledger, 10_000.0)
        self._record_equity(ledger, 8_000.0)  # 20% drawdown

        allowed, reason = check_circuit_breakers(ledger, settings)

        assert allowed is True  # no consecutive losses, drawdown disabled

    def test_consecutive_losses_zero_means_disabled(self, tmp_path: Path) -> None:
        """max_consecutive_losses=0 means the check is skipped entirely."""
        settings = _settings()
        settings.risk.max_consecutive_losses = 0
        ledger = self._make_ledger(tmp_path)

        for pnl in [-10.0, -5.0, -2.0, -1.0]:
            _record_sell(ledger, pnl)

        allowed, reason = check_circuit_breakers(ledger, settings)

        assert allowed is True
        assert reason is None

    def test_kill_switch_persists_across_calls(self, tmp_path: Path) -> None:
        """Once triggered, the kill-switch stays active"""
        settings = _settings()
        ledger = self._make_ledger(tmp_path)

        for pnl in [-5.0, -5.0, -5.0]:
            _record_sell(ledger, pnl)

        check_circuit_breakers(ledger, settings)
        # Subsequent check should still report blocked (kill switch state persists)
        allowed, reason = check_circuit_breakers(ledger, settings)

        assert allowed is False
        assert "Trading halted" in reason


class TestCircuitBreakerInOrchestrator:
    """Integration: run_paper_trade respects the breaker."""

    def test_paper_trade_blocked_by_circuit_breaker(self, tmp_path: Path) -> None:
        from trading_bot.runtime.orchestrator import run_paper_trade

        settings = Settings(
            app=AppSettings(
                state_db_path=str(tmp_path / "test.db"),
                log_dir=str(tmp_path / "logs"),
                portfolio_summary_path=str(tmp_path / "portfolio.json"),
                scan_results_path=str(tmp_path / "scan.json"),
            ),
            risk=RiskSettings(max_consecutive_losses=1),
            monitoring=MonitoringSettings(max_drawdown_pct=99.0),
        )

        ledger = PortfolioLedger(Path(settings.app.state_db_path))
        ledger.ensure_portfolio_state()
        _record_sell(ledger, -5.0)  # 1 consecutive loss == threshold

        result = run_paper_trade(["SPY"], settings=settings)

        assert len(result) == 1
        assert "CIRCUIT_BREAKER" in result[0]

    def test_scan_blocked_by_circuit_breaker(self, tmp_path: Path) -> None:
        from trading_bot.runtime.orchestrator import run_scan

        settings = Settings(
            app=AppSettings(
                state_db_path=str(tmp_path / "test.db"),
                log_dir=str(tmp_path / "logs"),
                portfolio_summary_path=str(tmp_path / "portfolio.json"),
                scan_results_path=str(tmp_path / "scan.json"),
            ),
            risk=RiskSettings(max_consecutive_losses=1),
            monitoring=MonitoringSettings(max_drawdown_pct=99.0),
        )

        ledger = PortfolioLedger(Path(settings.app.state_db_path))
        ledger.ensure_portfolio_state()
        _record_sell(ledger, -5.0)

        result = run_scan(["SPY"], settings=settings)

        assert len(result["lines"]) == 1
        assert "CIRCUIT_BREAKER" in result["lines"][0]


class TestManagePositionsCircuitBreaker:
    """manage-positions CLI also respects the circuit breaker."""

    def test_manage_positions_blocked_by_consecutive_losses(self, monkeypatch, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from trading_bot.cli.app import app
        from trading_bot.data import market_data

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            f"""
app:
  state_db_path: {tmp_path}/test.db
  log_dir: {tmp_path}/logs
  timezone: UTC
  live_trading_enabled: false
  scan_results_path: {tmp_path}/scan.json
  portfolio_summary_path: {tmp_path}/portfolio.json
  dashboard_summary_path: {tmp_path}/dashboard.json
  backtest_summary_path: {tmp_path}/backtest.json

risk:
  max_consecutive_losses: 1
  enable_drawdown_circuit_breaker: false

market_data:
  daily_period: 60d
  intraday_period: 5d
  intraday_interval: 5m
  max_data_age_hours: 1
  max_data_age_minutes: 60
  validate_data: false

paper:
  fee_per_order: 1.0
  slippage_bps: 10
"""
        )

        # Record a sell with loss so circuit breaker trips
        ledger = PortfolioLedger(tmp_path / "test.db")
        ledger.ensure_portfolio_state()
        _record_sell(ledger, -5.0)

        # Monkey-patch fetch_bars to return empty frames (no real market data)
        monkeypatch.setattr(market_data, "fetch_bars", lambda *a, **k: __import__("pandas").DataFrame())

        result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

        assert "CIRCUIT_BREAKER" in result.output
