import pytest
from pathlib import Path

from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.safety.kill_switch import (
    check_kill_switch_before_trade,
    halt_trading,
    is_trading_halted,
    resume_trading,
)


def test_kill_switch_defaults_to_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=10000, equity=10000))

    state = is_trading_halted(ledger)

    assert state.enabled is False
    assert state.reason is None


def test_halt_trading_sets_kill_switch(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=10000, equity=10000))

    halt_trading(ledger, reason="emergency test", triggered_by="test")

    state = is_trading_halted(ledger)
    assert state.enabled is True
    assert state.reason == "emergency test"
    assert state.triggered_by == "test"
    assert state.triggered_at is not None


def test_resume_trading_clears_kill_switch(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=10000, equity=10000))

    # First halt
    halt_trading(ledger, reason="test", triggered_by="test")
    assert is_trading_halted(ledger).enabled is True

    # Then resume
    resume_trading(ledger, resumed_by="operator")

    state = is_trading_halted(ledger)
    assert state.enabled is False


def test_check_kill_switch_before_trade_allows_when_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=10000, equity=10000))

    allowed, reason = check_kill_switch_before_trade(ledger)

    assert allowed is True
    assert reason is None


def test_check_kill_switch_before_trade_blocks_when_enabled(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=10000, equity=10000))

    halt_trading(ledger, reason="circuit breaker", triggered_by="system")

    allowed, reason = check_kill_switch_before_trade(ledger)

    assert allowed is False
    assert reason is not None
    assert "circuit breaker" in reason


def test_kill_switch_blocks_scan(tmp_path: Path) -> None:
    """Test that kill switch blocks scan command."""
    from trading_bot.config.settings import Settings
    from trading_bot.runtime.orchestrator import run_scan

    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=10000, equity=10000))

    # Halt trading
    halt_trading(ledger, reason="test halt", triggered_by="test")

    # Try to scan
    settings = Settings()
    settings.app.state_db_path = str(db_path)

    result = run_scan(["AAPL"], settings)

    assert len(result["lines"]) == 1
    assert "KILL_SWITCH" in result["lines"][0]
    assert result["summary"]["approved"] == 0


def test_kill_switch_blocks_paper_trade(tmp_path: Path) -> None:
    """Test that kill switch blocks paper-trade command."""
    from trading_bot.config.settings import Settings
    from trading_bot.runtime.orchestrator import run_paper_trade

    db_path = tmp_path / "test.db"
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=10000, equity=10000))

    # Halt trading
    halt_trading(ledger, reason="test halt", triggered_by="test")

    # Try to paper trade
    settings = Settings()
    settings.app.state_db_path = str(db_path)

    result = run_paper_trade(["AAPL"], settings)

    assert len(result) == 1
    assert "KILL_SWITCH" in result[0]


def test_kill_switch_persists_across_ledger_instances(tmp_path: Path) -> None:
    """Test that kill switch state persists in database."""
    db_path = tmp_path / "test.db"

    # First ledger instance - halt trading
    ledger1 = PortfolioLedger(db_path)
    ledger1.save_portfolio_state(PortfolioState(cash=10000, equity=10000))
    halt_trading(ledger1, reason="persistent test", triggered_by="test")

    # Second ledger instance - should see halted state
    ledger2 = PortfolioLedger(db_path)
    state = is_trading_halted(ledger2)

    assert state.enabled is True
    assert state.reason == "persistent test"
