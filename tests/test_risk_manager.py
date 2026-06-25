from __future__ import annotations

from datetime import datetime

from trading_bot.config.settings import RiskSettings
from trading_bot.models.signal import TradeSignal
from trading_bot.risk.risk_manager import evaluate_signal


def test_evaluate_signal_rejects_low_reward_risk() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.5,
        profit_target=100.6,
        risk_reward_ratio=1.2,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )

    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
    )

    assert decision.approved is False


def test_evaluate_signal_rejects_duplicate_open_ticker() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=103.0,
        risk_reward_ratio=3.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )

    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers={"AAPL"},
        portfolio_heat_pct=0.0,
        atr=None,
    )

    assert decision.approved is False
    assert decision.reason == "duplicate open ticker"


def test_evaluate_signal_approves_valid_signal() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=103.0,
        risk_reward_ratio=3.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )
    settings = RiskSettings(max_ticker_allocation_pct=1.0)  # No cap for this test

    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=settings,
    )

    assert decision.approved is True
    assert decision.reason == "approved"
    assert decision.position_size == 100
    assert decision.dollar_risk == 100.0


def test_evaluate_signal_rejects_unsupported_action() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="SELL",
        entry_price=100.0,
        stop_loss=101.0,
        profit_target=97.0,
        risk_reward_ratio=3.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )

    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
    )

    assert decision.approved is False
    assert decision.reason == "unsupported signal action"


def test_evaluate_signal_rejects_non_positive_equity() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=103.0,
        risk_reward_ratio=3.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )

    decision = evaluate_signal(
        signal=signal,
        account_equity=0,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
    )

    assert decision.approved is False
    assert decision.reason == "invalid account equity"


def test_evaluate_signal_uses_configured_risk_thresholds() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.5,
        profit_target=100.6,
        risk_reward_ratio=1.2,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )
    settings = RiskSettings(min_reward_risk_ratio=1.0)

    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=settings,
    )

    assert decision.approved is True


def test_evaluate_signal_rejects_when_portfolio_heat_exceeded() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=103.0,
        risk_reward_ratio=3.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )

    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.05,  # 5% heat
        atr=None,
    )

    assert decision.approved is False
    assert "portfolio heat limit exceeded" in decision.reason


def test_evaluate_signal_uses_atr_when_provided() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,  # $1 risk per share with fixed stop
        profit_target=103.0,
        risk_reward_ratio=3.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )
    settings = RiskSettings(use_atr_sizing=True, atr_multiplier=2.0)

    # ATR = $5, multiplier = 2.0 -> effective stop distance = $10
    # Risk amount = $10000 * 1% = $100
    # Position size = $100 / $10 = 10 shares
    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=5.0,
        risk_settings=settings,
    )

    assert decision.approved is True
    assert decision.position_size == 10  # ATR-based sizing
