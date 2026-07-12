from __future__ import annotations

from dataclasses import dataclass

from trading_bot.strategy.counter_thesis import (
    CounterThesisContext,
    evaluate_counter_thesis,
)
from trading_bot.strategy.market_regime import MarketRegime, RegimeMetrics
from trading_bot.strategy.supermodel import build_stacked_signal


@dataclass(frozen=True)
class _Signal:
    confidence: float = 0.9
    strategy_tag: str = "v3-trend_following"


def _clean_context(regime: MarketRegime = MarketRegime.WEAK_UPTREND) -> CounterThesisContext:
    return CounterThesisContext(
        symbol="AAPL",
        strategy_tag="v3-trend_following",
        closes=[100.0, 100.5, 101.0, 101.2, 101.5],
        rsi_series=[55.0, 56.0, 57.0, 56.5, 58.0],
        latest_rsi=58.0,
        volumes=[1000.0, 1100.0, 1200.0, 1050.0, 1300.0],
        latest_volume=1300.0,
        avg_volume=1100.0,
        bb_percent_b=80.0,
        price_vs_ema20=1.0,
        price_vs_sma50=1.5,
        momentum_3=0.01,
        volatility_percentile=0.3,
        regime=regime,
        regime_metrics=RegimeMetrics(),
    )


def test_supermodel_without_local_signal_is_neutral_no_signal() -> None:
    result = build_stacked_signal("AAPL", None, {})

    assert result.decision == "no_signal"
    assert result.score == 0.0
    assert result.layers[0].name == "setup"
    assert result.layers[0].verdict == "neutral"


def test_supermodel_counter_thesis_veto_blocks_active_signal() -> None:
    result = build_stacked_signal(
        "AAPL",
        _Signal(),
        {"counter_thesis_block": True},
    )

    assert result.decision == "block"
    assert any(layer.name == "counter" and layer.verdict == "block" for layer in result.layers)


def test_clean_counter_thesis_context_keeps_full_confidence() -> None:
    result = evaluate_counter_thesis(_clean_context(), None)

    assert result.findings == []
    assert result.confidence_multiplier == 1.0
    assert result.block_trade is False


def test_strong_downtrend_counter_thesis_blocks_trade() -> None:
    result = evaluate_counter_thesis(
        _clean_context(regime=MarketRegime.STRONG_DOWNTREND),
        None,
    )

    assert result.block_trade is True
    assert any(
        finding.check_name == "regime_misalignment"
        and finding.severity == "severe"
        for finding in result.findings
    )
