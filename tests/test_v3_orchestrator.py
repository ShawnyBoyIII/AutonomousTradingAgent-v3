"""Tests for V3 strategy path wired into the orchestrator.

Validates that enabling ``strategy.use_v3_signals`` routes signal generation
through StrategySelector (regime detection + confluence scoring) instead of
the legacy intraday_signal_engine, while still producing TradeSignals that
the rest of the pipeline (risk manager, paper broker) can consume.
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from typer.testing import CliRunner

from trading_bot.cli.app import app
from trading_bot.models.portfolio import PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger


def _v3_daily_frame() -> pd.DataFrame:
    """60 bars of strong uptrend with narrowing ranges (BB squeeze).

    Produces HIGH_VOLATILITY regime (ADX > 50) due to strong directional movement.
    Tests use risk_tolerance: high to allow trading in this regime.
    """
    # Strong consistent uptrend - produces high ADX
    closes = [100.0 + i * 0.5 for i in range(60)]
    # Ranges narrow over time for BB squeeze
    ranges = [3.5 * (1 - i / 80) for i in range(60)]  # 3.5 -> 0.88
    
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-04-01", periods=60, freq="D"),
            "open": [c - 0.2 for c in closes],
            "high": [c + r / 2 for c, r in zip(closes, ranges)],
            "low": [c - r / 2 for c, r in zip(closes, ranges)],
            "close": closes,
            "volume": [1_000_000 for _ in range(60)],
        }
    )


def _v3_intraday_frame() -> pd.DataFrame:
    """5 bars with a breakout bar (close above prior range high) + volume surge.

    Last close (~130) aligns with the daily trend's latest close (~129.5).
    """
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
            "open": [129.0, 129.4, 129.2, 129.6, 130.1],
            "high": [129.5, 129.8, 129.6, 130.0, 131.0],
            "low": [128.8, 129.0, 129.0, 129.3, 129.9],
            "close": [129.2, 129.6, 129.4, 129.8, 130.6],
            "volume": [1000, 1100, 950, 1050, 6000],
        }
    )


def test_v3_signal_path_produces_approved_candidate(monkeypatch, tmp_path: Path) -> None:
    """When use_v3_signals=true, scan produces an APPROVED candidate."""
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = _v3_daily_frame()
    intraday = _v3_intraday_frame()

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
    )

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "strategy:\n"
        "  use_v3_signals: true\n"
        "  risk_tolerance: high\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=20_000.0, equity=20_000.0)
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "scan", "--symbols", "AAPL"])

    assert result.exit_code == 0
    snapshot = json.loads((tmp_path / "state" / "scan_results.json").read_text(encoding="utf-8"))
    approved = [c for c in snapshot["candidates"] if c["status"] == "APPROVED"]
    assert len(approved) == 1
    assert approved[0]["ticker"] == "AAPL"
    assert approved[0]["confidence"] > 0.0


def test_v3_signal_path_includes_confluence_details_in_why(monkeypatch, tmp_path: Path) -> None:
    """scan --why surfaces V3 confluence component scores in details."""
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = _v3_daily_frame()
    intraday = _v3_intraday_frame()

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
    )

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "strategy:\n"
        "  use_v3_signals: true\n"
        "  risk_tolerance: high\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=20_000.0, equity=20_000.0)
    )

    result = CliRunner().invoke(
        app, ["--config-path", str(config_file), "scan", "--symbols", "AAPL", "--why"]
    )

    assert result.exit_code == 0
    assert "v3_total_score" in result.stdout or "v3_confidence" in result.stdout


def test_v3_disabled_falls_back_to_legacy_path(monkeypatch, tmp_path: Path) -> None:
    """Without use_v3_signals, the legacy V2.5 path runs (conf=0.90)."""
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + i for i in range(60)],
            "high": [101.0 + i for i in range(60)],
            "low": [99.0 + i for i in range(60)],
            "close": [100.0 + i for i in range(60)],
            "volume": [1_000_000 for _ in range(60)],
        }
    )
    intraday = pd.DataFrame(
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

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
    )

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=20_000.0, equity=20_000.0)
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "scan", "--symbols", "AAPL"])

    assert result.exit_code == 0
    # Legacy path emits confidence 0.90 (volume surge threshold) and the
    # strategy_tag "intraday-signal-engine".
    assert "conf=0.90" in result.stdout
    assert "intraday-signal-engine" not in result.stdout  # strategy_tag isn't printed
    assert "v3_total_score" not in result.stdout


def test_adapt_v3_selection_enforces_buy_geometry() -> None:
    """The adapter clamps None entry/stop/target into valid BUY geometry."""
    from trading_bot.strategy.strategy_selector import selection_to_signal
    from trading_bot.strategy.signal_confluence import SignalScore
    from trading_bot.strategy.market_regime import MarketRegime

    class FakeSelection:
        should_trade = True
        strategy_type = "trend_following"
        setup_name = "breakout"
        signal_score = SignalScore(total_score=8.0, confidence="high")
        regime = MarketRegime.STRONG_UPTREND
        reason = "test"
        entry_price = None  # forces fallback to intraday close
        stop_loss = None    # forces fallback to entry * 0.99
        profit_target = None
        position_size_multiplier = 0.9

    intraday = pd.DataFrame(
        {
            "timestamp": [datetime(2026, 6, 13, 10, 20, 0)],
            "close": [100.0],
            "high": [101.0],
            "low": [99.0],
            "open": [99.5],
            "volume": [1000],
        }
    )

    signal = selection_to_signal("AAPL", FakeSelection(), intraday)

    assert signal is not None
    assert signal.action == "BUY"
    assert signal.stop_loss < signal.entry_price < signal.profit_target
    assert signal.entry_price == 100.0
    assert signal.stop_loss == 99.0
    assert signal.risk_reward_ratio > 0
    assert signal.strategy_tag == "v3-trend_following"
