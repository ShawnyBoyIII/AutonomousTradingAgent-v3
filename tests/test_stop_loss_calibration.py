from __future__ import annotations

import pandas as pd

from trading_bot.config.settings import RiskSettings, Settings
from trading_bot.strategy.intraday_signal_engine import (
    generate_signal_with_reason,
    generate_recent_signal_with_reason,
)
from trading_bot.strategy.strategy_selector import StrategySelector


def _bullish_daily_frame():
    n = 30
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1000.0] * n,
            "ema_20": [99.0] * n,
            "sma_50": [98.0] * n,
        },
        index=pd.date_range("2025-01-01", periods=n, freq="1d"),
    )


def _breakout_intraday_frame():
    n = 20
    close = [50.0] * 10 + [51.0, 51.5, 52.0, 52.5, 53.0, 54.0, 55.0, 56.0, 57.0, 60.0]
    return pd.DataFrame(
        {
            "open": [c * 0.999 for c in close],
            "high": [c * 1.015 for c in close],
            "low": [c * 0.995 for c in close],
            "close": close,
            "volume": [2000.0] * n,
            "volume_avg_5": [1000.0] * n,
            "atr_14": [0.5] * n,
        },
        index=pd.date_range("2025-01-02 10:00", periods=n, freq="5min"),
    )


class TestV25StopEnforcement:
    def test_min_stop_distance_enforced_when_low_stop_tight(self):
        daily = _bullish_daily_frame()
        intraday = _breakout_intraday_frame()
        # ATR = 0.5, atr_stop_multiplier = 3.0, so ATR floor = 58 - 0.5*3 = 56.5
        # 5-bar low ~ 57.0
        # But min_stop_distance_pct = 3%: stop must be <= 58 * 0.97 = 56.26
        signal, reason = generate_signal_with_reason(
            "AAPL", daily, intraday,
            atr_stop_multiplier=3.0,
            min_stop_distance_pct=3.0,
        )
        assert signal is not None
        assert reason == "approved"
        min_stop = signal.entry_price * 0.97
        assert signal.stop_loss <= min_stop, f"stop {signal.stop_loss} > min_stop {min_stop}"

    def test_min_stop_distance_enforced_with_large_atr(self):
        daily = _bullish_daily_frame()
        intraday = _breakout_intraday_frame()
        intraday["atr_14"] = 10.0
        signal, reason = generate_signal_with_reason(
            "AAPL", daily, intraday,
            atr_stop_multiplier=3.0,
            min_stop_distance_pct=3.0,
        )
        assert signal is not None
        assert signal.stop_loss <= signal.entry_price * 0.5

    def test_min_stop_0pct_disables_enforcement(self):
        daily = _bullish_daily_frame()
        intraday = _breakout_intraday_frame()
        signal, reason = generate_signal_with_reason(
            "AAPL", daily, intraday,
            atr_stop_multiplier=1.5,
            min_stop_distance_pct=0.0,
        )
        assert signal is not None
        min_stop_boundary = signal.entry_price * 0.99
        assert signal.stop_loss < min_stop_boundary

    def test_generate_recent_signal_passes_min_stop(self):
        daily = _bullish_daily_frame()
        intraday = _breakout_intraday_frame()
        signal, reason = generate_recent_signal_with_reason(
            "AAPL", daily, intraday,
            lookback_bars=6,
            atr_stop_multiplier=3.0,
            min_stop_distance_pct=3.0,
        )
        assert signal is not None
        assert signal.stop_loss <= signal.entry_price * 0.97


class TestV3StopEnforcement:
    def test_min_stop_enforced_in_strategy_selector(self):
        daily = _bullish_daily_frame()
        intraday = _breakout_intraday_frame()
        # Add V3 indicators
        intraday["rsi_14"] = [55.0] * len(intraday)
        intraday["bb_lower"] = [40.0] * len(intraday)
        intraday["bb_upper"] = [60.0] * len(intraday)
        intraday["vwap"] = [55.0] * len(intraday)
        daily["atr_14"] = [2.0] * len(daily)

        selector = StrategySelector(risk_tolerance="high")
        selector.min_confidence = "low"
        selector.atr_stop_multiplier = 3.0
        selector.min_stop_distance_pct = 3.0
        selection = selector.select_strategy("AAPL", daily, intraday)

        if selection.should_trade and selection.stop_loss is not None:
            assert selection.stop_loss <= selection.entry_price * 0.97

    def test_selector_default_min_stop_is_0(self):
        selector = StrategySelector()
        assert selector.min_stop_distance_pct == 0.0


class TestDefaultConfigValues:
    def test_atr_stop_multiplier_default_is_3(self):
        assert RiskSettings().atr_stop_multiplier == 3.0

    def test_min_stop_distance_pct_default_is_3(self):
        # AGENTS.md mandates a 5-minute-bar minimum stop distance. The default
        # must not silently disable the rule; config.yaml's 3.0 floor is the
        # universal floor (burn-in configs override to 5.0 for stricter runs).
        assert RiskSettings().min_stop_distance_pct == 3.0
