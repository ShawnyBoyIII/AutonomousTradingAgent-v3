from __future__ import annotations

from datetime import datetime

import pytest

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
    settings = RiskSettings(
        max_ticker_allocation_pct=1.0,  # No allocation cap for this test
        max_shares_per_position=1000,  # Way above natural size, so no clamp
    )

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


def test_evaluate_signal_applies_fractional_kelly_when_enabled() -> None:
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
    settings = RiskSettings(
        max_ticker_allocation_pct=1.0,
        use_kelly_sizing=True,
        kelly_fraction_scale=0.5,
        kelly_min_position_pct=0.25,
    )

    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=settings,
    )

    assert decision.approved is True
    assert decision.position_size == 36
    assert decision.dollar_risk == 36.66666666666667


def test_evaluate_signal_rejects_non_positive_kelly_edge() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=101.0,
        risk_reward_ratio=1.0,
        confidence=0.2,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )
    settings = RiskSettings(
        min_reward_risk_ratio=1.0,
        use_kelly_sizing=True,
    )

    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=settings,
    )

    assert decision.approved is False
    assert decision.reason == "kelly edge non-positive"


def test_evaluate_signal_scales_on_elevated_correlation() -> None:
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
    settings = RiskSettings(max_ticker_allocation_pct=1.0)

    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=settings,
        avg_correlation=0.8,
        max_avg_correlation=0.6,
    )

    assert decision.approved is True
    assert decision.position_size == 49
    assert decision.dollar_risk == pytest.approx(50.0)
    assert decision.portfolio_exposure_warning is not None
    assert "correlation elevated" in decision.portfolio_exposure_warning


def test_evaluate_signal_rejects_when_correlation_too_high() -> None:
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
        portfolio_heat_pct=0.0,
        atr=None,
        avg_correlation=0.9,
        max_avg_correlation=0.6,
    )

    assert decision.approved is False
    assert decision.reason == "portfolio correlation too high (0.90 > 0.60)"


# --------------------------------------------------------------------- #
# Max-open-positions cap (2026-07-10 feature)
# --------------------------------------------------------------------- #
# Caps the total number of different tickers simultaneously held. A
# diversity / concentration guard that complements the existing
# per-ticker allocation cap (`max_ticker_allocation_pct`).
# --------------------------------------------------------------------- #


def _build_test_signal(ticker: str = "AAPL") -> TradeSignal:
    return TradeSignal(
        ticker=ticker,
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


# --------------------------------------------------------------------- #
# Per-ticker share-count cap (2026-07-10 feature; replaces
# max_open_positions which the user walked back: "we should be able to
# trade 100+ stocks. I want to limit the total counts of trades per
# stock. ... each stock can hold a maximum of 50.")
# --------------------------------------------------------------------- #


def test_max_shares_per_position_default_is_50() -> None:
    """Default RiskSettings has max_shares_per_position=50 (user spec)."""
    settings = RiskSettings()
    assert settings.max_shares_per_position == 50


def test_max_shares_per_position_validator_rejects_zero() -> None:
    """max_shares_per_position must be >= 1."""
    import pydantic

    raised = False
    try:
        RiskSettings(max_shares_per_position=0)
    except pydantic.ValidationError:
        raised = True
    assert raised


def test_max_shares_per_position_validator_rejects_over_100k() -> None:
    """max_shares_per_position must be <= 100000 (sane upper bound)."""
    import pydantic

    raised = False
    try:
        RiskSettings(max_shares_per_position=100001)
    except pydantic.ValidationError:
        raised = True
    assert raised


def test_evaluate_signal_clamps_position_size_to_max_shares() -> None:
    """ATR sizing producing 1700 shares is clamped to max_shares_per_position=50."""
    signal = _build_test_signal("SCHW")  # placeholder; we'll override fields below
    # Use a higher-priced stock with a tight stop so ATR sizing wants
    # many shares.  We want the ATR path to produce >50 shares and verify
    # the clamp to 50.
    signal.entry_price = 100.0
    signal.stop_loss = 99.0  # 1% stop
    signal.profit_target = 110.0  # 10% target → rr=10
    signal.risk_reward_ratio = 10.0
    signal.confidence = 0.95  # high confidence → bigger Kelly multiplier
    # ATR=2 with multiplier=2.5 → stop_distance=5 → for a $100 stock the
    # ATR-stop-driven size is large.  With $100k equity and 1% risk,
    # dollar_risk_budget = $1000, stop_distance_dollars = $5,
    # desired_shares = 1000/5 = 200.  With Kelly scaling up further,
    # desired_shares is well over 50.
    settings = RiskSettings(
        max_shares_per_position=50,
        max_ticker_allocation_pct=1.0,  # no allocation cap interference
        max_risk_per_trade_pct=0.01,
        use_atr_sizing=True,
        atr_multiplier=2.5,
        atr_stop_multiplier=3.0,
        use_kelly_sizing=False,
    )
    decision = evaluate_signal(
        signal=signal,
        account_equity=100000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=2.0,
        risk_settings=settings,
    )
    assert decision.approved is True
    assert decision.position_size == 50, (
        f"position_size must be clamped to max_shares_per_position=50, "
        f"got {decision.position_size}"
    )


def test_evaluate_signal_does_not_clamp_under_cap() -> None:
    """When computed size is below the cap, the position is unchanged."""
    signal = _build_test_signal("AAPL")
    signal.entry_price = 100.0
    signal.stop_loss = 95.0  # 5% stop
    signal.profit_target = 110.0
    signal.risk_reward_ratio = 2.0
    signal.confidence = 0.7
    settings = RiskSettings(
        max_shares_per_position=500,  # very high cap, won't clamp
        max_ticker_allocation_pct=1.0,
        max_risk_per_trade_pct=0.01,
        use_atr_sizing=False,  # use fixed-stop path
    )
    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=settings,
    )
    assert decision.approved is True
    # The clamp must NOT have changed the size — it should be the
    # natural fixed-stop sizing for this scenario.
    # fixed_stop: equity=10k, risk=1%=100, stop_dist=$5 → qty=20
    assert decision.position_size == 20


def test_evaluate_signal_clamps_fixed_stop_path() -> None:
    """Fixed-stop sizing is also subject to max_shares_per_position."""
    signal = _build_test_signal("SCHW")
    signal.entry_price = 100.0
    signal.stop_loss = 95.0
    signal.profit_target = 110.0
    signal.risk_reward_ratio = 2.0
    signal.confidence = 0.7
    settings = RiskSettings(
        max_shares_per_position=15,
        max_ticker_allocation_pct=1.0,
        max_risk_per_trade_pct=0.05,  # 5% risk → wants 100 shares naturally
        use_atr_sizing=False,
    )
    decision = evaluate_signal(
        signal=signal,
        account_equity=10000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=settings,
    )
    assert decision.approved is True
    assert decision.position_size == 15, (
        f"fixed-stop sizing must clamp to max_shares_per_position=15, "
        f"got {decision.position_size}"
    )


def test_max_open_positions_field_removed() -> None:
    """The max_open_positions field is gone (replaced by max_shares_per_position)."""
    # Field should not exist; if it does, accessing it raises AttributeError
    settings = RiskSettings()
    assert not hasattr(settings, "max_open_positions"), (
        "max_open_positions was removed; per-ticker share cap replaces it"
    )


def test_max_shares_per_position_kelly_scaling_also_clamped() -> None:
    """Even after Kelly scaling increases the size, max_shares_per_position applies."""
    signal = _build_test_signal("AAPL")
    signal.entry_price = 100.0
    signal.stop_loss = 99.0
    signal.profit_target = 103.0
    signal.risk_reward_ratio = 3.0
    signal.confidence = 0.95  # high confidence → Kelly multiplier > 1
    signal.risk_reward_ratio = 5.0
    settings = RiskSettings(
        max_shares_per_position=40,
        max_ticker_allocation_pct=1.0,
        max_risk_per_trade_pct=0.01,
        use_atr_sizing=False,
        use_kelly_sizing=True,
        kelly_fraction_scale=0.5,
        kelly_min_position_pct=0.01,
    )
    decision = evaluate_signal(
        signal=signal,
        account_equity=100000,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=settings,
    )
    assert decision.approved is True
    assert decision.position_size <= 40, (
        f"post-Kelly size must also be clamped, got {decision.position_size}"
    )
