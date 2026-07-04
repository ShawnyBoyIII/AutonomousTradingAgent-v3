"""Tests for the counter-thesis engine.

Pure checks are exercised against directly-constructed contexts (no network),
the fetcher is tested via monkeypatched ``fetch_bars``, and the orchestrator
integration confirms the wiring vetoes/scales trades end-to-end.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from trading_bot.config.settings import CounterThesisSettings, RiskSettings
from trading_bot.models.signal import TradeSignal
from trading_bot.risk.risk_manager import evaluate_signal
from trading_bot.strategy.counter_thesis import (
    CounterThesisContext,
    SEVERITY_WEIGHTS,
    build_counter_thesis_context,
    evaluate_counter_thesis,
    fetch_counter_thesis_context,
)
from trading_bot.strategy.market_regime import MarketRegime, RegimeMetrics


def _clean_context(**overrides) -> CounterThesisContext:
    base = CounterThesisContext(
        symbol="AAPL",
        strategy_tag="v3-trend_following",
        closes=[100.0, 100.5, 101.0, 101.2, 101.5],
        rsi_series=[55.0, 56.0, 57.0, 56.5, 58.0],
        latest_rsi=58.0,
        volumes=[1000.0, 1100.0, 1200.0, 1050.0, 1300.0],
        latest_volume=2500.0,
        avg_volume=1100.0,
        bb_percent_b=80.0,
        price_vs_ema20=1.0,
        price_vs_sma50=1.5,
        momentum_3=0.01,
        volatility_percentile=0.3,
        regime=MarketRegime.WEAK_UPTREND,
        regime_metrics=RegimeMetrics(),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _signal(strategy_tag: str = "v3-trend_following") -> TradeSignal:
    return TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=103.0,
        risk_reward_ratio=3.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag=strategy_tag,
        timestamp=datetime(2026, 6, 13, 10, 0, 0),
    )


# --------------------------------------------------------------------------- #
# Aggregation / blocking behavior
# --------------------------------------------------------------------------- #


def test_clean_context_produces_no_findings() -> None:
    result = evaluate_counter_thesis(_clean_context(), _signal(), CounterThesisSettings())

    assert result.findings == []
    assert result.overall_severity == "none"
    assert result.confidence_multiplier == 1.0
    assert result.block_trade is False


def test_none_context_does_not_block() -> None:
    result = evaluate_counter_thesis(None, _signal(), CounterThesisSettings())

    assert result.findings == []
    assert result.block_trade is False
    assert result.confidence_multiplier == 1.0


def test_disabled_check_is_skipped() -> None:
    settings = CounterThesisSettings(check_overbought=False)
    result = evaluate_counter_thesis(
        _clean_context(latest_rsi=85.0), _signal(), settings
    )

    assert all(f.check_name != "overbought" for f in result.findings)


def test_severe_finding_blocks_via_block_on_severity() -> None:
    ctx = _clean_context(regime=MarketRegime.STRONG_DOWNTREND)
    result = evaluate_counter_thesis(ctx, _signal(), CounterThesisSettings())

    assert any(f.check_name == "regime_misalignment" and f.severity == "severe" for f in result.findings)
    assert result.overall_severity == "severe"
    assert result.block_trade is True


def test_high_finding_blocks_when_block_on_severity_is_high() -> None:
    ctx = _clean_context(
        latest_rsi=80.0,
        strategy_tag="v3-trend_following",
    )
    result = evaluate_counter_thesis(ctx, _signal(), CounterThesisSettings())

    overbought = [f for f in result.findings if f.check_name == "overbought"]
    assert len(overbought) == 1
    assert overbought[0].severity == "high"
    assert result.block_trade is True


def test_aggregate_blocks_when_threshold_exceeded() -> None:
    settings = CounterThesisSettings(block_on_severity="severe", aggregate_block_threshold=0.6)
    ctx = _clean_context(
        bb_percent_b=110.0,
        momentum_3=-0.02,
        latest_volume=500.0,
        avg_volume=1000.0,
    )
    result = evaluate_counter_thesis(ctx, _signal(), settings)

    medium = [f for f in result.findings if f.severity == "medium"]
    assert len(medium) >= 2
    penalty = sum(SEVERITY_WEIGHTS[f.severity] for f in result.findings)
    assert penalty >= 0.6
    assert result.block_trade is True


def test_confidence_multiplier_sums_weights() -> None:
    ctx = _clean_context(bb_percent_b=110.0)
    result = evaluate_counter_thesis(ctx, _signal(), CounterThesisSettings())

    assert len(result.findings) == 1
    assert result.findings[0].severity == "medium"
    assert result.confidence_multiplier == 1.0 - SEVERITY_WEIGHTS["medium"]


def test_to_dict_is_json_serializable() -> None:
    ctx = _clean_context(latest_rsi=80.0, regime=MarketRegime.STRONG_DOWNTREND)
    result = evaluate_counter_thesis(ctx, _signal(), CounterThesisSettings())

    serialized = json.dumps(result.to_dict())
    payload = json.loads(serialized)

    assert payload["overall_severity"] == "severe"
    assert payload["block_trade"] is True
    assert isinstance(payload["findings"], list)


# --------------------------------------------------------------------------- #
# Individual pure checks
# --------------------------------------------------------------------------- #


def test_overbought_is_higher_severity_for_trend_thesis() -> None:
    trend_ctx = _clean_context(latest_rsi=80.0, strategy_tag="v3-trend_following")
    reversion_ctx = _clean_context(latest_rsi=80.0, strategy_tag="mean_reversion")

    trend_result = evaluate_counter_thesis(trend_ctx, _signal("v3-trend_following"))
    reversion_result = evaluate_counter_thesis(reversion_ctx, _signal("mean_reversion"))

    trend_ob = next(f for f in trend_result.findings if f.check_name == "overbought")
    reversion_ob = next(f for f in reversion_result.findings if f.check_name == "overbought")
    assert trend_ob.severity == "high"
    assert reversion_ob.severity == "medium"


def test_volume_non_confirmation_finding() -> None:
    ctx = _clean_context(latest_volume=500.0, avg_volume=1000.0)
    result = evaluate_counter_thesis(ctx, _signal())

    finding = next(f for f in result.findings if f.check_name == "volume_non_confirmation")
    assert finding.severity == "medium"


def test_rsi_divergence_finding() -> None:
    ctx = _clean_context(
        closes=[100.0, 100.5, 101.0, 101.5, 102.0],
        rsi_series=[70.0, 65.0, 60.0, 56.0, 52.0],
    )
    result = evaluate_counter_thesis(ctx, _signal())

    finding = next(f for f in result.findings if f.check_name == "rsi_divergence")
    assert finding.severity == "high"


def test_resistance_proximity_finding() -> None:
    ctx = _clean_context(bb_percent_b=105.0)
    result = evaluate_counter_thesis(ctx, _signal())

    finding = next(f for f in result.findings if f.check_name == "resistance_proximity")
    assert finding.severity == "medium"


def test_regime_misalignment_levels() -> None:
    for regime, expected in [
        (MarketRegime.STRONG_DOWNTREND, "severe"),
        (MarketRegime.HIGH_VOLATILITY, "medium"),
        (MarketRegime.WEAK_DOWNTREND, "medium"),
    ]:
        ctx = _clean_context(regime=regime)
        result = evaluate_counter_thesis(ctx, _signal())
        finding = next(f for f in result.findings if f.check_name == "regime_misalignment")
        assert finding.severity == expected


def test_waning_momentum_finding() -> None:
    ctx = _clean_context(momentum_3=-0.02)
    result = evaluate_counter_thesis(ctx, _signal())

    finding = next(f for f in result.findings if f.check_name == "waning_momentum")
    assert finding.severity == "medium"


def test_volatility_spike_finding() -> None:
    ctx = _clean_context(volatility_percentile=0.9)
    result = evaluate_counter_thesis(ctx, _signal())

    finding = next(f for f in result.findings if f.check_name == "volatility_spike")
    assert finding.severity == "medium"


def test_extension_medium_and_high_thresholds() -> None:
    medium_ctx = _clean_context(price_vs_ema20=7.0)
    high_ctx = _clean_context(price_vs_ema20=12.0)

    medium_result = evaluate_counter_thesis(medium_ctx, _signal())
    high_result = evaluate_counter_thesis(high_ctx, _signal())

    medium_finding = next(f for f in medium_result.findings if f.check_name == "extension")
    high_finding = next(f for f in high_result.findings if f.check_name == "extension")
    assert medium_finding.severity == "medium"
    assert high_finding.severity == "high"


# --------------------------------------------------------------------------- #
# Pure builder over frames
# --------------------------------------------------------------------------- #


def _daily_frame() -> pd.DataFrame:
    closes = [100.0 + i for i in range(60)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-04-01", periods=60, freq="D"),
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1_000_000 for _ in range(60)],
        }
    )


def _intraday_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-13 09:30:00",
                    "2026-06-13 09:35:00",
                    "2026-06-13 09:40:00",
                    "2026-06-13 09:45:00",
                    "2026-06-13 09:50:00",
                    "2026-06-13 09:55:00",
                    "2026-06-13 10:00:00",
                    "2026-06-13 10:05:00",
                    "2026-06-13 10:10:00",
                    "2026-06-13 10:15:00",
                    "2026-06-13 10:20:00",
                    "2026-06-13 10:25:00",
                    "2026-06-13 10:30:00",
                    "2026-06-13 10:35:00",
                    "2026-06-13 10:40:00",
                    "2026-06-13 10:45:00",
                ]
            ),
            "open": [158.0] * 16,
            "high": [160.0] * 16,
            "low": [157.0] * 16,
            "close": [158.0 + 0.5 * i for i in range(16)],
            "volume": [2000 + i * 100 for i in range(16)],
        }
    )


def test_build_counter_thesis_context_populates_indicators() -> None:
    ctx = build_counter_thesis_context(
        symbol="AAPL",
        signal=_signal(),
        daily_frame=_daily_frame(),
        intraday_frame=_intraday_frame(),
    )

    assert ctx is not None
    assert ctx.symbol == "AAPL"
    assert ctx.latest_rsi is not None
    assert ctx.avg_volume is not None
    assert ctx.bb_percent_b is not None
    assert ctx.price_vs_ema20 is not None
    assert ctx.regime is not None


def test_build_context_returns_none_when_columns_missing() -> None:
    bad_daily = pd.DataFrame({"close": [1.0, 2.0]})
    assert build_counter_thesis_context("X", _signal(), bad_daily, _intraday_frame()) is None


# --------------------------------------------------------------------------- #
# Fetcher (network I/O isolated via monkeypatch)
# --------------------------------------------------------------------------- #


def test_fetch_context_returns_none_when_fetch_raises(monkeypatch) -> None:
    import trading_bot.data.market_data as market_data

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(market_data, "fetch_bars", boom)

    from trading_bot.config.settings import MarketDataSettings

    ctx = fetch_counter_thesis_context("AAPL", _signal(), MarketDataSettings())
    assert ctx is None


def test_fetch_context_builds_from_patched_frames(monkeypatch) -> None:
    import trading_bot.data.market_data as market_data

    daily = _daily_frame()
    intraday = _intraday_frame()

    def fake_fetch_bars(symbol, period, interval, **kwargs):
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    from trading_bot.config.settings import MarketDataSettings

    ctx = fetch_counter_thesis_context("AAPL", _signal(), MarketDataSettings())
    assert ctx is not None
    assert ctx.latest_rsi is not None


# --------------------------------------------------------------------------- #
# Risk manager integration
# --------------------------------------------------------------------------- #


def test_blocked_counter_thesis_vetoes_signal() -> None:
    from trading_bot.strategy.counter_thesis import CounterThesisFinding, CounterThesisResult

    blocked = CounterThesisResult(
        findings=[CounterThesisFinding("regime_misalignment", "severe", "downtrend", 1.0)],
        overall_severity="severe",
        confidence_multiplier=0.0,
        block_trade=True,
        reasons=["regime_misalignment:severe"],
    )

    decision = evaluate_signal(
        signal=_signal(),
        account_equity=10_000.0,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=RiskSettings(max_ticker_allocation_pct=1.0),
        counter_thesis=blocked,
    )

    assert decision.approved is False
    assert "counter-thesis blocked" in decision.reason
    assert decision.position_size == 0


def test_confidence_multiplier_scales_position_size() -> None:
    from trading_bot.strategy.counter_thesis import CounterThesisResult

    scaled = CounterThesisResult(
        findings=[],
        overall_severity="none",
        confidence_multiplier=0.5,
        block_trade=False,
    )

    decision = evaluate_signal(
        signal=_signal(),
        account_equity=10_000.0,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=RiskSettings(max_ticker_allocation_pct=1.0),
        counter_thesis=scaled,
    )

    assert decision.approved is True
    assert decision.position_size == 50  # 100 * 0.5
    assert decision.dollar_risk == 50.0


def test_none_counter_thesis_keeps_full_position_size() -> None:
    decision = evaluate_signal(
        signal=_signal(),
        account_equity=10_000.0,
        open_tickers=set(),
        portfolio_heat_pct=0.0,
        atr=None,
        risk_settings=RiskSettings(max_ticker_allocation_pct=1.0),
        counter_thesis=None,
    )

    assert decision.approved is True
    assert decision.position_size == 100


# --------------------------------------------------------------------------- #
# Orchestrator integration (scan + paper-trade)
# --------------------------------------------------------------------------- #


def _scan_daily_frame() -> pd.DataFrame:
    closes = [100.0 + i for i in range(60)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + i for i in range(60)],
            "high": [101.0 + i for i in range(60)],
            "low": [99.0 + i for i in range(60)],
            "close": closes,
            "volume": [1_000_000 for _ in range(60)],
        }
    )


def _scan_intraday_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-13 10:00:00",
                    "2026-06-13 10:05:00",
                    "2026-06-13 10:10:00",
                    "2026-06-13 10:15:00",
                    "2026-06-13 10:20:00",
                ]
            ),
            "open": [99.9, 100.1, 100.0, 100.2, 100.5],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4],
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "volume": [1000, 1100, 950, 1050, 2500],
        }
    )


def _write_counter_thesis_config(tmp_path: Path, db_path: Path) -> Path:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "counter_thesis:\n"
        "  enabled: true\n",
        encoding="utf-8",
    )
    return config_file


def test_scan_with_counter_thesis_enabled_rejects_blocked_trade(
    monkeypatch, tmp_path: Path
) -> None:
    """A linear-uptrend daily frame trips regime/extension/volatility checks;

    with counter_thesis enabled the risk manager vetoes the otherwise-GREEN
    candidate, proving the orchestrator wiring end-to-end."""
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.cli.app import app
    from trading_bot.models.portfolio import PortfolioState
    from trading_bot.portfolio.ledger import PortfolioLedger
    from typer.testing import CliRunner

    daily = _scan_daily_frame()
    intraday = _scan_intraday_frame()

    def fake_fetch_bars(symbol, period, interval, **kwargs):
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
    )

    db_path = tmp_path / "state.db"
    config_file = _write_counter_thesis_config(tmp_path, db_path)
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=20_000.0, equity=20_000.0)
    )

    result = CliRunner().invoke(
        app, ["--config-path", str(config_file), "scan", "--symbols", "AAPL", "--why"]
    )

    assert result.exit_code == 0
    assert "AAPL REJECTED" in result.stdout
    assert "counter_thesis_block=true" in result.stdout
    assert "counter_thesis_findings=" in result.stdout
    # Supermodel veto fires first (aggregates counter-thesis evidence)
    assert ("supermodel block" in result.stdout or "counter-thesis blocked" in result.stdout)

    snapshot = json.loads((tmp_path / "state" / "scan_results.json").read_text(encoding="utf-8"))
    rejected = [c for c in snapshot["candidates"] if c["status"] == "REJECTED"]
    assert len(rejected) == 1
    assert rejected[0]["details"]["counter_thesis_block"] is True


def test_scan_with_counter_thesis_disabled_does_not_block(
    monkeypatch, tmp_path: Path
) -> None:
    """With counter_thesis disabled (default), the same frame stays APPROVED."""
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.cli.app import app
    from trading_bot.models.portfolio import PortfolioState
    from trading_bot.portfolio.ledger import PortfolioLedger
    from typer.testing import CliRunner

    daily = _scan_daily_frame()
    intraday = _scan_intraday_frame()

    def fake_fetch_bars(symbol, period, interval, **kwargs):
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
    )

    db_path = tmp_path / "state.db"
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n" f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=20_000.0, equity=20_000.0)
    )

    result = CliRunner().invoke(
        app, ["--config-path", str(config_file), "scan", "--symbols", "AAPL", "--why"]
    )

    assert result.exit_code == 0
    assert "AAPL APPROVED" in result.stdout
    assert "counter_thesis_block=true" not in result.stdout


def test_counter_thesis_cli_command_runs_analysis(
    monkeypatch, tmp_path: Path
) -> None:
    """The counter-thesis CLI command surfaces findings for a symbol."""
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.cli.app import app
    from trading_bot.models.portfolio import PortfolioState
    from trading_bot.portfolio.ledger import PortfolioLedger
    from typer.testing import CliRunner

    daily = _scan_daily_frame()
    intraday = _scan_intraday_frame()

    def fake_fetch_bars(symbol, period, interval, **kwargs):
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
    )

    db_path = tmp_path / "state.db"
    config_file = _write_counter_thesis_config(tmp_path, db_path)
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=20_000.0, equity=20_000.0)
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "counter-thesis", "--symbols", "AAPL", "--why"],
    )

    assert result.exit_code == 0
    assert "AAPL counter_thesis" in result.stdout
    assert "block=true" in result.stdout


# --------------------------------------------------------------------------- #
# Backtest runner integration
# --------------------------------------------------------------------------- #


def _bt_daily_frame() -> pd.DataFrame:
    """60-bar uptrend with full OHLCV + EMA/SMA (steep enough to trip extension).

    close at end = 159, ema_20 ≈ 149 → price_vs_ema20 ≈ 6.7% → medium extension.
    Also trips resistance_proximity (bb_percent_b ≥ 100) → aggregate penalty ≥ 0.7 > 0.6.
    """
    from trading_bot.data.indicators import add_ema, add_sma

    closes = [100.0 + i for i in range(60)]
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1_000_000 for _ in range(60)],
        }
    )
    df = add_ema(df, 20, "ema_20")
    df = add_sma(df, 50, "sma_50")
    return df


def _bt_intraday_frame() -> pd.DataFrame:
    """7-bar intraday with a breakout + volume surge (generates a signal)."""
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-13 10:00:00",
                    "2026-06-13 10:05:00",
                    "2026-06-13 10:10:00",
                    "2026-06-13 10:15:00",
                    "2026-06-13 10:20:00",
                    "2026-06-13 10:25:00",
                    "2026-06-13 10:30:00",
                ]
            ),
            "open": [99.9, 100.1, 100.0, 100.2, 100.5, 101.0, 102.5],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1, 101.1, 103.5],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4, 99.7, 102.0],
            "close": [100.0, 100.2, 100.1, 100.3, 101.0, 100.8, 103.0],
            "volume": [1000, 1100, 950, 1050, 2500, 1500, 1800],
        }
    )
    df["volume_avg_5"] = df["volume"].rolling(5).mean()
    return df


def test_backtest_with_counter_thesis_enabled_blocks_trades() -> None:
    """Historical replay vetoes trades identically to scan when enabled."""
    from trading_bot.backtest.runner import _run_symbol_backtest
    from trading_bot.config.settings import Settings

    daily = _bt_daily_frame()
    intraday = _bt_intraday_frame()

    result_disabled = _run_symbol_backtest("AAPL", daily, intraday, Settings())
    assert result_disabled["trades"] >= 1  # Confirms baseline: trades are taken

    settings = Settings()
    settings.counter_thesis = CounterThesisSettings(enabled=True)
    result_enabled = _run_symbol_backtest("AAPL", daily, intraday, settings)

    assert result_enabled["trades"] == 0  # All trades vetoed by counter-thesis


def test_backtest_with_counter_thesis_disabled_does_not_block() -> None:
    """With counter-thesis disabled (default), backtest behaves identically."""
    from trading_bot.backtest.runner import _run_symbol_backtest
    from trading_bot.config.settings import Settings

    daily = _bt_daily_frame()
    intraday = _bt_intraday_frame()

    result = _run_symbol_backtest("AAPL", daily, intraday, Settings())

    assert result["trades"] >= 1
    assert result["wins"] + result["losses"] == result["trades"]


# --------------------------------------------------------------------------- #
# Manage-positions exit-side integration
# --------------------------------------------------------------------------- #


def _downtrend_daily_frame() -> pd.DataFrame:
    """60-bar downtrend → STRONG_DOWNTREND regime → severe counter-thesis block.

    close at end: 82, sma_50 at end ≈ 92 → price_vs_sma50 ≈ -11%.
    momentum (10-bar ROC): (82-102)/102 ≈ -19.6% → normalized -0.98.
    ADX > 25 (strong directional movement).
    """
    closes = [200.0 - i * 2.0 for i in range(60)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-04-01", periods=60, freq="D"),
            "open": [c + 1.0 for c in closes],
            "high": [c + 2.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1_000_000 for _ in range(60)],
        }
    )


def _manage_intraday_frame() -> pd.DataFrame:
    """6 bars around $100; last_price=99.7 (between stop=90 and target=120)."""
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-21 10:00:00",
                    "2026-06-21 10:05:00",
                    "2026-06-21 10:10:00",
                    "2026-06-21 10:15:00",
                    "2026-06-21 10:20:00",
                    "2026-06-21 10:25:00",
                ]
            ),
            "open": [100.0, 99.8, 99.5, 99.7, 100.1, 99.9],
            "high": [100.2, 100.0, 99.7, 99.9, 100.3, 100.1],
            "low": [99.7, 99.5, 99.2, 99.5, 99.8, 99.6],
            "close": [99.8, 99.6, 99.3, 99.6, 100.0, 99.7],
            "volume": [1000, 1100, 950, 1050, 1200, 1000],
        }
    )


def _write_manage_config(tmp_path: Path, db_path: Path, enabled: bool) -> Path:
    config_file = tmp_path / "config.yaml"
    ct_block = "  enabled: true\n" if enabled else "  enabled: false\n"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "session:\n"
        "  eod_enabled: false\n"
        "market_data:\n"
        "  max_data_age_minutes: 120\n"
        "counter_thesis:\n"
        f"{ct_block}",
        encoding="utf-8",
    )
    return config_file


def _open_position_state(db_path) -> None:
    """Save a portfolio state with one open AAPL position."""
    from trading_bot.models.portfolio import Position, PortfolioState
    from trading_bot.portfolio.ledger import PortfolioLedger

    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=90.0,
                    profit_target=120.0,
                    strategy_tag="test",
                )
            },
        )
    )


def test_manage_positions_exits_on_counter_thesis_block(
    monkeypatch, tmp_path: Path
) -> None:
    """When the thesis is broken, manage-positions exits early."""
    import trading_bot.data.market_data as market_data
    from trading_bot.cli.app import app
    from trading_bot.runtime import session
    from typer.testing import CliRunner

    daily = _downtrend_daily_frame()
    intraday = _manage_intraday_frame()

    def fake_fetch_bars(symbol, period, interval, **kwargs):
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        session,
        "now_in_zone",
        lambda tz: datetime(2026, 6, 21, 11, 0, 0, tzinfo=session.ZoneInfo(tz)),
    )

    db_path = tmp_path / "state.db"
    config_file = _write_manage_config(tmp_path, db_path, enabled=True)
    _open_position_state(db_path)

    result = CliRunner().invoke(
        app, ["--config-path", str(config_file), "manage-positions"]
    )

    assert result.exit_code == 0
    assert "FILLED reason=counter-thesis" in result.stdout
    assert "actions=1" in result.stdout

    log_path = tmp_path / "logs" / "decision-log.jsonl"
    log_text = log_path.read_text(encoding="utf-8")
    assert '"reason": "counter-thesis"' in log_text
    assert '"counter_thesis"' in log_text


def test_manage_positions_keeps_position_when_counter_thesis_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    """With counter-thesis disabled, the position is kept (no early exit)."""
    import trading_bot.data.market_data as market_data
    from trading_bot.cli.app import app
    from trading_bot.runtime import session
    from typer.testing import CliRunner

    daily = _downtrend_daily_frame()
    intraday = _manage_intraday_frame()

    def fake_fetch_bars(symbol, period, interval, **kwargs):
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        session,
        "now_in_zone",
        lambda tz: datetime(2026, 6, 21, 11, 0, 0, tzinfo=session.ZoneInfo(tz)),
    )

    db_path = tmp_path / "state.db"
    config_file = _write_manage_config(tmp_path, db_path, enabled=False)
    _open_position_state(db_path)

    result = CliRunner().invoke(
        app, ["--config-path", str(config_file), "manage-positions"]
    )

    assert result.exit_code == 0
    assert "counter-thesis" not in result.stdout


def test_manage_positions_exit_priority_after_target_before_trail(
    monkeypatch, tmp_path: Path
) -> None:
    """Counter-thesis exit slots after profit-target but before trailing-stop.

    Position has stop=90, target=120 (neither hit at last_price=99.7). The
    counter-thesis block fires before the trailing-stop logic, so the
    position is exited as 'counter-thesis' rather than trailing.
    """
    import trading_bot.data.market_data as market_data
    from trading_bot.cli.app import app
    from trading_bot.runtime import session
    from typer.testing import CliRunner

    daily = _downtrend_daily_frame()
    intraday = _manage_intraday_frame()

    def fake_fetch_bars(symbol, period, interval, **kwargs):
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        session,
        "now_in_zone",
        lambda tz: datetime(2026, 6, 21, 11, 0, 0, tzinfo=session.ZoneInfo(tz)),
    )

    db_path = tmp_path / "state.db"
    config_file = _write_manage_config(tmp_path, db_path, enabled=True)
    _open_position_state(db_path)

    result = CliRunner().invoke(
        app, ["--config-path", str(config_file), "manage-positions"]
    )

    assert result.exit_code == 0
    assert "FILLED reason=counter-thesis" in result.stdout
    assert "FILLED reason=target" not in result.stdout
    assert "TRAIL" not in result.stdout
