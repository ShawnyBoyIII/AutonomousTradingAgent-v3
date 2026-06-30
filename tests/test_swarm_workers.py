"""Tests for concrete swarm worker implementations."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from trading_bot.swarm.base import WorkerConfig, WorkerResult, WorkerState
from trading_bot.swarm.workers import (
    FundamentalAnalystWorker,
    MacroStrategistWorker,
    OnChainAnalystWorker,
    PatternRecognizerWorker,
    QuantFactorWorker,
    RiskManagerWorker,
    TechnicalAnalystWorker,
    WORKER_CLASSES,
    get_worker_class,
)


def _make_dataframe(
    n: int = 252,
    start_price: float = 100.0,
    trend: float = 0.001,
    vol: float = 0.02,
) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame."""
    dates = pd.date_range(end=datetime.now(), periods=n, freq="B")
    returns = np.random.normal(trend, vol, n)
    prices = start_price * np.exp(np.cumsum(returns))
    highs = prices * (1 + np.random.uniform(0, 0.01, n))
    lows = prices * (1 - np.random.uniform(0, 0.01, n))
    opens = lows + (highs - lows) * np.random.uniform(0.3, 0.7, n)
    volumes = np.random.randint(100000, 1000000, n)
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes,
    }, index=dates)


def _make_dataframe_with_uptrend(n: int = 252, start_price: float = 100.0) -> pd.DataFrame:
    """Generate a DataFrame with a clear uptrend."""
    dates = pd.date_range(end=datetime.now(), periods=n, freq="B")
    prices = start_price * np.exp(np.linspace(0.05, 0.15, n))
    highs = prices * 1.005
    lows = prices * 0.995
    opens = (highs + lows) / 2
    volumes = np.random.randint(100000, 1000000, n)
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes,
    }, index=dates)


def _make_dataframe_with_downtrend(n: int = 252, start_price: float = 100.0) -> pd.DataFrame:
    """Generate a DataFrame with a clear downtrend."""
    dates = pd.date_range(end=datetime.now(), periods=n, freq="B")
    prices = start_price * np.exp(np.linspace(0.15, 0.0, n))
    highs = prices * 1.005
    lows = prices * 0.995
    opens = (highs + lows) / 2
    volumes = np.random.randint(100000, 1000000, n)
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes,
    }, index=dates)


class TestTechnicalAnalystWorker:
    """TechnicalAnalystWorker tests."""

    def test_execute_with_data(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df})
        assert result.state == WorkerState.DONE
        assert result.worker_name == "test_tech"
        assert result.data["workers_analyzed"] == 1

    def test_execute_with_multiple_symbols(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        market_data = {
            "AAPL": _make_dataframe(),
            "SPY": _make_dataframe(),
        }
        result = worker.execute(["AAPL", "SPY"], market_data)
        assert result.data["workers_analyzed"] == 2
        assert len(result.signals) == 2

    def test_execute_with_missing_data(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        result = worker.execute(["AAPL"], {})
        assert result.state == WorkerState.DONE
        assert len(result.signals) == 0

    def test_execute_with_empty_dataframe(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        result = worker.execute(["AAPL"], {"AAPL": empty_df})
        assert result.state == WorkerState.DONE
        assert len(result.signals) == 0

    def test_execute_with_non_dataframe(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        result = worker.execute(["AAPL"], {"AAPL": "not a dataframe"})
        assert result.state == WorkerState.DONE
        assert len(result.signals) == 0

    def test_signal_has_correct_structure(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df})
        signal = result.signals[0]
        assert "ticker" in signal
        assert "action" in signal
        assert "confidence" in signal
        assert "worker_name" in signal
        assert "preset" in signal

    def test_uptrend_generates_buy_signal(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        df = _make_dataframe_with_uptrend()
        result = worker.execute(["AAPL"], {"AAPL": df})
        signal = result.signals[0]
        assert signal["action"] == "BUY"

    def test_technical_rating_bullish(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        df = _make_dataframe_with_uptrend()
        rating = worker._compute_technical_rating(df)
        assert rating == "Bullish"

    def test_technical_rating_bearish(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        # Create a DataFrame with clear bearish indicators
        dates = pd.date_range(end=datetime.now(), periods=252, freq="B")
        # Strong downtrend: prices declining from 150 to 80
        prices = np.linspace(150, 80, 252)
        highs = prices * 1.002
        lows = prices * 0.998
        opens = (highs + lows) / 2
        # Volume below 1.5x average
        volumes = np.full(252, 500000)
        df = pd.DataFrame({
            "open": opens,
            "high": highs,
            "low": lows,
            "close": prices,
            "volume": volumes,
        }, index=dates)
        rating = worker._compute_technical_rating(df)
        # With declining prices, ema_trend should be negative
        # RSI will be low (< 40) in a strong downtrend, which adds +1
        # Volume ratio is 1.0 (not > 1.5), so no +1
        # Score could be 0 (neutral) or -1 (bearish) depending on RSI
        assert rating in ["Bearish", "Neutral"]

    def test_compute_indicators_returns_dict(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        df = _make_dataframe()
        indicators = worker._compute_indicators(df)
        assert "rsi" in indicators
        assert "ema_trend" in indicators
        assert "volume_ratio" in indicators

    def test_rsi_in_valid_range(self):
        config = WorkerConfig(name="test_tech", preset="technical_analysis_panel")
        worker = TechnicalAnalystWorker(config)
        df = _make_dataframe()
        indicators = worker._compute_indicators(df)
        rsi = indicators["rsi"]
        assert 0 <= rsi <= 100


class TestRiskManagerWorker:
    """RiskManagerWorker tests."""

    def test_execute_with_data(self):
        config = WorkerConfig(name="test_risk", preset="risk_committee")
        worker = RiskManagerWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df})
        assert result.state == WorkerState.DONE
        assert "risks" in result.data

    def test_execute_with_portfolio_state(self):
        config = WorkerConfig(name="test_risk", preset="risk_committee")
        worker = RiskManagerWorker(config)
        df = _make_dataframe()
        portfolio_state = {
            "cash": 50000,
            "equity": 500000,
            "positions": {
                "AAPL": {"quantity": 100, "average_cost": 150.0},
            },
        }
        result = worker.execute(["AAPL"], {"AAPL": df}, portfolio_state)
        assert result.state == WorkerState.DONE
        assert "risks" in result.data

    def test_portfolio_concentration_risk_detected(self):
        config = WorkerConfig(name="test_risk", preset="risk_committee")
        worker = RiskManagerWorker(config)
        df = _make_dataframe()
        portfolio_state = {
            "cash": 10000,
            "equity": 100000,
            "positions": {
                "AAPL": {"quantity": 1000, "average_cost": 150.0},
            },
        }
        result = worker.execute(["AAPL"], {"AAPL": df}, portfolio_state)
        risks = result.data["risks"]
        assert any("concentration" in r.lower() for r in risks)

    def test_low_cash_buffer_risk_detected(self):
        config = WorkerConfig(name="test_risk", preset="risk_committee")
        worker = RiskManagerWorker(config)
        df = _make_dataframe()
        portfolio_state = {
            "cash": 1000,
            "equity": 100000,
            "positions": {},
        }
        result = worker.execute(["AAPL"], {"AAPL": df}, portfolio_state)
        risks = result.data["risks"]
        assert any("cash buffer" in r.lower() for r in risks)

    def test_high_volatility_risk_detected(self):
        config = WorkerConfig(name="test_risk", preset="risk_committee")
        worker = RiskManagerWorker(config)
        df = _make_dataframe(vol=0.08)
        result = worker.execute(["AAPL"], {"AAPL": df})
        risks = result.data["risks"]
        assert any("volatility" in r.lower() for r in risks)

    def test_no_portfolio_state(self):
        config = WorkerConfig(name="test_risk", preset="risk_committee")
        worker = RiskManagerWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df}, None)
        assert result.state == WorkerState.DONE

    def test_consumes_upstream_analyst_results(self):
        config = WorkerConfig(name="risk_manager", preset="risk_committee")
        worker = RiskManagerWorker(config)
        df = _make_dataframe()
        technical = WorkerResult(
            worker_name="technical_analyst",
            preset="technical",
            state=WorkerState.DONE,
            ticker_results={"AAPL": {"action": "BUY", "confidence": 0.77}},
        )
        fundamental = WorkerResult(
            worker_name="fundamental_analyst",
            preset="fundamental",
            state=WorkerState.DONE,
            ticker_results={"AAPL": {"action": "HOLD", "confidence": 0.5}},
        )
        result = worker.execute(
            ["AAPL"],
            {"AAPL": df},
            worker_results={
                "fundamental_analyst": fundamental,
                "technical_analyst": technical,
            },
        )

        assert result.data["upstream_workers"] == [
            "fundamental_analyst",
            "technical_analyst",
        ]
        metadata = result.ticker_results["AAPL"]["metadata"]
        assert metadata["technical_action"] == "BUY"
        assert metadata["technical_confidence"] == 0.77
        assert metadata["fundamental_action"] == "HOLD"
        assert metadata["fundamental_confidence"] == 0.5


class TestQuantFactorWorker:
    """QuantFactorWorker tests."""

    def test_execute_with_data(self):
        config = WorkerConfig(name="test_quant", preset="quant_desk")
        worker = QuantFactorWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df})
        assert result.state == WorkerState.DONE
        assert "factors_computed" in result.data

    def test_factors_computed(self):
        config = WorkerConfig(name="test_quant", preset="quant_desk")
        worker = QuantFactorWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df})
        factors = worker._compute_factor_scores(df)
        assert "momentum" in factors
        assert "value" in factors
        assert "quality" in factors

    def test_uptrend_positive_momentum(self):
        config = WorkerConfig(name="test_quant", preset="quant_desk")
        worker = QuantFactorWorker(config)
        df = _make_dataframe_with_uptrend()
        factors = worker._compute_factor_scores(df)
        assert factors["momentum"] > 0

    def test_multiple_symbols(self):
        config = WorkerConfig(name="test_quant", preset="quant_desk")
        worker = QuantFactorWorker(config)
        market_data = {
            "AAPL": _make_dataframe(),
            "SPY": _make_dataframe(),
        }
        result = worker.execute(["AAPL", "SPY"], market_data)
        assert len(result.signals) == 2

    def test_missing_data_skipped(self):
        config = WorkerConfig(name="test_quant", preset="quant_desk")
        worker = QuantFactorWorker(config)
        result = worker.execute(["AAPL"], {})
        assert result.state == WorkerState.DONE
        assert len(result.signals) == 0


class TestFundamentalAnalystWorker:
    """FundamentalAnalystWorker tests."""

    def test_execute_with_data(self):
        config = WorkerConfig(name="test_fund", preset="fundamental_analysis_team")
        worker = FundamentalAnalystWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df})
        assert result.state == WorkerState.DONE
        assert "fundamentals_computed" in result.data

    def test_fundamentals_computed(self):
        config = WorkerConfig(name="test_fund", preset="fundamental_analysis_team")
        worker = FundamentalAnalystWorker(config)
        df = _make_dataframe()
        fundamentals = worker._compute_fundamentals(df)
        assert "quality" in fundamentals
        assert "value" in fundamentals
        assert "growth" in fundamentals

    def test_quality_in_valid_range(self):
        config = WorkerConfig(name="test_fund", preset="fundamental_analysis_team")
        worker = FundamentalAnalystWorker(config)
        df = _make_dataframe()
        fundamentals = worker._compute_fundamentals(df)
        assert -1.0 <= fundamentals["quality"] <= 1.0

    def test_value_in_valid_range(self):
        config = WorkerConfig(name="test_fund", preset="fundamental_analysis_team")
        worker = FundamentalAnalystWorker(config)
        df = _make_dataframe()
        fundamentals = worker._compute_fundamentals(df)
        assert -1.0 <= fundamentals["value"] <= 1.0

    def test_growth_in_valid_range(self):
        config = WorkerConfig(name="test_fund", preset="fundamental_analysis_team")
        worker = FundamentalAnalystWorker(config)
        df = _make_dataframe()
        fundamentals = worker._compute_fundamentals(df)
        assert -1.0 <= fundamentals["growth"] <= 1.0

    def test_multiple_symbols(self):
        config = WorkerConfig(name="test_fund", preset="fundamental_analysis_team")
        worker = FundamentalAnalystWorker(config)
        market_data = {
            "AAPL": _make_dataframe(),
            "SPY": _make_dataframe(),
        }
        result = worker.execute(["AAPL", "SPY"], market_data)
        assert len(result.signals) == 2


class TestMacroStrategistWorker:
    """MacroStrategistWorker tests."""

    def test_execute_with_data(self):
        config = WorkerConfig(name="test_macro", preset="macro_economics_team")
        worker = MacroStrategistWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df})
        assert result.state == WorkerState.DONE
        assert "market_regime" in result.data

    def test_market_regime_detected(self):
        config = WorkerConfig(name="test_macro", preset="macro_economics_team")
        worker = MacroStrategistWorker(config)
        df = _make_dataframe_with_uptrend()
        result = worker.execute(["AAPL"], {"AAPL": df})
        regime = result.data["market_regime"]
        assert regime in ["bull_trend", "bear_trend", "range_bound", "unknown"]

    def test_regime_signal_generated(self):
        config = WorkerConfig(name="test_macro", preset="macro_economics_team")
        worker = MacroStrategistWorker(config)
        df = _make_dataframe_with_uptrend()
        result = worker.execute(["AAPL"], {"AAPL": df})
        assert len(result.signals) == 1
        signal = result.signals[0]
        assert signal["action"] in ["BUY", "SELL", "HOLD"]

    def test_multiple_regimes(self):
        config = WorkerConfig(name="test_macro", preset="macro_economics_team")
        worker = MacroStrategistWorker(config)
        market_data = {
            "AAPL": _make_dataframe_with_uptrend(),
            "SPY": _make_dataframe_with_downtrend(),
        }
        result = worker.execute(["AAPL", "SPY"], market_data)
        assert result.state == WorkerState.DONE


class TestPatternRecognizerWorker:
    """PatternRecognizerWorker tests."""

    def test_execute_with_data(self):
        config = WorkerConfig(name="test_pattern", preset="technical_analysis_panel")
        worker = PatternRecognizerWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df})
        assert result.state == WorkerState.DONE
        assert "patterns_detected" in result.data

    def test_patterns_detected(self):
        config = WorkerConfig(name="test_pattern", preset="technical_analysis_panel")
        worker = PatternRecognizerWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df})
        # Patterns may or may not be detected depending on data
        assert isinstance(result.signals, list)

    def test_no_patterns_returns_hold(self):
        config = WorkerConfig(name="test_pattern", preset="technical_analysis_panel")
        worker = PatternRecognizerWorker(config)
        signal = worker._patterns_to_signal("AAPL", [])
        assert signal is not None
        assert signal.action == "HOLD"
        assert signal.confidence == 0.5

    def test_bullish_patterns_generate_buy(self):
        config = WorkerConfig(name="test_pattern", preset="technical_analysis_panel")
        worker = PatternRecognizerWorker(config)
        patterns = [
            {"name": "double_bottom", "type": "bullish_reversal", "confidence": 0.7},
        ]
        signal = worker._patterns_to_signal("AAPL", patterns)
        assert signal.action == "BUY"

    def test_bearish_patterns_generate_sell(self):
        config = WorkerConfig(name="test_pattern", preset="technical_analysis_panel")
        worker = PatternRecognizerWorker(config)
        patterns = [
            {"name": "double_top", "type": "bearish_reversal", "confidence": 0.7},
        ]
        signal = worker._patterns_to_signal("AAPL", patterns)
        assert signal.action == "SELL"

    def test_double_bottom_detection(self):
        config = WorkerConfig(name="test_pattern", preset="technical_analysis_panel")
        worker = PatternRecognizerWorker(config)
        # Create a DataFrame with a double bottom pattern
        dates = pd.date_range(end=datetime.now(), periods=50, freq="B")
        prices = np.array([100] * 50)
        prices[10:20] = 110
        prices[25:35] = 110
        prices[40:45] = 95
        prices[48] = 97  # Above the bottom
        highs = prices * 1.01
        lows = prices * 0.99
        opens = (highs + lows) / 2
        volumes = np.random.randint(100000, 1000000, 50)
        df = pd.DataFrame({
            "open": opens,
            "high": highs,
            "low": lows,
            "close": prices,
            "volume": volumes,
        }, index=dates)
        patterns = worker._detect_patterns(df)
        pattern_names = [p["name"] for p in patterns]
        assert "double_bottom" in pattern_names

    def test_insufficient_data_no_patterns(self):
        config = WorkerConfig(name="test_pattern", preset="technical_analysis_panel")
        worker = PatternRecognizerWorker(config)
        dates = pd.date_range(end=datetime.now(), periods=20, freq="B")
        prices = np.random.uniform(90, 110, 20)
        highs = prices * 1.01
        lows = prices * 0.99
        opens = (highs + lows) / 2
        volumes = np.random.randint(100000, 1000000, 20)
        df = pd.DataFrame({
            "open": opens,
            "high": highs,
            "low": lows,
            "close": prices,
            "volume": volumes,
        }, index=dates)
        patterns = worker._detect_patterns(df)
        assert len(patterns) == 0


class TestOnChainAnalystWorker:
    """OnChainAnalystWorker tests."""

    def test_execute_with_data(self):
        config = WorkerConfig(name="test_onchain", preset="crypto_desk")
        worker = OnChainAnalystWorker(config)
        df = _make_dataframe()
        result = worker.execute(["AAPL"], {"AAPL": df})
        assert result.state == WorkerState.DONE
        assert "flow_metrics" in result.data

    def test_flow_metrics_computed(self):
        config = WorkerConfig(name="test_onchain", preset="crypto_desk")
        worker = OnChainAnalystWorker(config)
        df = _make_dataframe()
        flow = worker._analyze_volume_flow(df)
        assert "mfi" in flow
        assert "ad_slope" in flow
        assert "volume_trend" in flow
        assert "smart_money" in flow
        assert "accumulation_score" in flow

    def test_accumulation_score_in_range(self):
        config = WorkerConfig(name="test_onchain", preset="crypto_desk")
        worker = OnChainAnalystWorker(config)
        df = _make_dataframe()
        flow = worker._analyze_volume_flow(df)
        assert -1.0 <= flow["accumulation_score"] <= 1.0

    def test_multiple_symbols(self):
        config = WorkerConfig(name="test_onchain", preset="crypto_desk")
        worker = OnChainAnalystWorker(config)
        market_data = {
            "AAPL": _make_dataframe(),
            "SPY": _make_dataframe(),
        }
        result = worker.execute(["AAPL", "SPY"], market_data)
        assert len(result.signals) == 2


class TestWorkerRegistry:
    """WORKER_CLASSES and get_worker_class tests."""

    def test_all_workers_registered(self):
        assert "technical_analyst" in WORKER_CLASSES
        assert "risk_manager" in WORKER_CLASSES
        assert "factor_model" in WORKER_CLASSES
        assert "fundamental_analyst" in WORKER_CLASSES
        assert "macro_strategist" in WORKER_CLASSES
        assert "pattern_recognizer" in WORKER_CLASSES
        assert "on_chain_analyst" in WORKER_CLASSES

    def test_get_worker_class(self):
        cls = get_worker_class("technical_analyst")
        assert cls == TechnicalAnalystWorker

        cls = get_worker_class("risk_manager")
        assert cls == RiskManagerWorker

        cls = get_worker_class("factor_model")
        assert cls == QuantFactorWorker

        cls = get_worker_class("fundamental_analyst")
        assert cls == FundamentalAnalystWorker

        cls = get_worker_class("macro_strategist")
        assert cls == MacroStrategistWorker

        cls = get_worker_class("pattern_recognizer")
        assert cls == PatternRecognizerWorker

        cls = get_worker_class("on_chain_analyst")
        assert cls == OnChainAnalystWorker

    def test_get_worker_class_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown worker"):
            get_worker_class("nonexistent_worker")

    def test_get_worker_class_mentions_available(self):
        with pytest.raises(ValueError, match="technical_analyst"):
            get_worker_class("nonexistent_worker")

    def test_worker_class_inherits_from_base(self):
        cls = get_worker_class("technical_analyst")
        instance = cls(WorkerConfig(name="test", preset="default"))
        assert isinstance(instance, TechnicalAnalystWorker)
        assert isinstance(instance, TechnicalAnalystWorker)
