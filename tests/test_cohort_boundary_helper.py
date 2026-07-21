"""Cohort boundary resolution: equity_evaluation_since with graduation_since fallback.

Bug: circuit_breaker.drawdown and dashboard.snapshot used
``settings.paper.equity_evaluation_since`` directly, with no fallback to
``graduation_since``. When the equity boundary is unset, the cohort drawdown
included legacy peak equity from before the burn-in reset and could falsely
trip the circuit breaker.

Fix: centralize resolution in `trading_bot.safety.circuit_breaker.cohort_boundary`
and use it from both call sites. The CLI already used the fallback inline.
"""

from __future__ import annotations

from datetime import datetime


def _settings(*, equity_boundary=None, graduation_boundary=None):
    """Build a minimal Settings-like object exposing paper.{equity,graduation}_since."""
    from types import SimpleNamespace

    paper = SimpleNamespace(
        equity_evaluation_since=equity_boundary,
        graduation_since=graduation_boundary,
    )
    app = SimpleNamespace(timezone="America/New_York")
    return SimpleNamespace(paper=paper, app=app)


def test_cohort_boundary_prefers_equity_evaluation_since_when_both_set():
    from trading_bot.safety.circuit_breaker import cohort_boundary

    equity = datetime(2026, 7, 15)
    graduation = datetime(2026, 7, 11)
    settings = _settings(equity_boundary=equity, graduation_boundary=graduation)
    assert cohort_boundary(settings) == equity


def test_cohort_boundary_falls_back_to_graduation_when_equity_missing():
    from trading_bot.safety.circuit_breaker import cohort_boundary

    graduation = datetime(2026, 7, 11)
    settings = _settings(equity_boundary=None, graduation_boundary=graduation)
    assert cohort_boundary(settings) == graduation


def test_cohort_boundary_returns_none_when_neither_set():
    from trading_bot.safety.circuit_breaker import cohort_boundary

    settings = _settings(equity_boundary=None, graduation_boundary=None)
    assert cohort_boundary(settings) is None


def test_cohort_boundary_handles_missing_paper_section():
    """Defensive: a malformed Settings without paper.* should not crash."""
    from trading_bot.safety.circuit_breaker import cohort_boundary
    from types import SimpleNamespace

    settings = SimpleNamespace(app=SimpleNamespace(timezone="UTC"))
    assert cohort_boundary(settings) is None


def test_circuit_breaker_drawdown_uses_cohort_boundary(monkeypatch, tmp_path):
    """check_circuit_breakers must pass the resolved boundary to drawdown,
    not raw equity_evaluation_since. Without this test, a missing
    equity_evaluation_since would compute drawdown over legacy equity."""
    from datetime import datetime, timezone

    from trading_bot.config.settings import Settings
    from trading_bot.portfolio.ledger import PortfolioLedger
    from trading_bot.monitoring import drawdown as drawdown_module
    from trading_bot.safety import circuit_breaker

    settings = Settings(
        app={"state_db_path": str(tmp_path / "state.db"), "log_dir": str(tmp_path)}
    )
    settings.session.eod_enabled = False
    graduation_dt = datetime(2026, 7, 11, tzinfo=timezone.utc)
    # Set only graduation_since; equity_evaluation_since intentionally None
    settings.paper.equity_evaluation_since = None
    settings.paper.graduation_since = graduation_dt
    settings.risk.enable_drawdown_circuit_breaker = True

    captured: dict = {}

    def fake_drawdown(ledger, limit=None, since=None, naive_timezone=None):
        captured["since"] = since
        from types import SimpleNamespace
        return SimpleNamespace(
            current_drawdown_pct=0.0,
            max_drawdown_pct=0.0,
            sample_size=0,
            sufficient_evidence=False,
        )

    monkeypatch.setattr(
        circuit_breaker, "compute_drawdown_from_ledger", fake_drawdown
    )

    ledger = PortfolioLedger(tmp_path / "state.db")
    allowed, _ = circuit_breaker.check_circuit_breakers(ledger, settings)
    assert allowed is True
    assert captured["since"] == graduation_dt