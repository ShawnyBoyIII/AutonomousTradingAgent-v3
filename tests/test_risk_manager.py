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

    decision = evaluate_signal(signal=signal, account_equity=10000, open_tickers=set())

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

    decision = evaluate_signal(signal=signal, account_equity=10000, open_tickers={"AAPL"})

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

    decision = evaluate_signal(signal=signal, account_equity=10000, open_tickers=set())

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

    decision = evaluate_signal(signal=signal, account_equity=10000, open_tickers=set())

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

    decision = evaluate_signal(signal=signal, account_equity=0, open_tickers=set())

    assert decision.approved is False
    assert decision.reason == "invalid account equity"


def test_evaluate_signal_uses_configured_risk_thresholds() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=101.5,
        risk_reward_ratio=1.5,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )

    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        risk_settings=RiskSettings(
            max_risk_per_trade_pct=0.02,
            max_daily_risk_pct=0.03,
            max_ticker_allocation_pct=0.20,
            min_reward_risk_ratio=1.5,
        ),
    )

    assert decision.approved is True
    assert decision.position_size == 200
    assert decision.dollar_risk == 200.0
