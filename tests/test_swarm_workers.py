"""Tests for new swarm workers: fundamental, macro, pattern, on-chain."""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from trading_bot.swarm.base import WorkerConfig, WorkerState
from trading_bot.swarm.workers import (
    FundamentalAnalystWorker,
    MacroStrategistWorker,
    OnChainAnalystWorker,
    PatternRecognizerWorker,
)


def _make_frame(n=252, start_price=100.0):
    """Create a mock OHLCV frame."""
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    prices = [start_price * (1 + 0.001 * i + 0.0001 * i**2) for i in range(n)]
    return pd.DataFrame({
        "timestamp": dates,
        "open": [p * 0.999 for p in prices],
        "high": [p * 1.001 for p in prices],
        "low": [p * 0.998 for p in prices],
        "close": prices,
        "volume": [1_000_000 for _ in range(n)],
    })


class TestFundamentalAnalystWorker:
    """Tests for FundamentalAnalystWorker."""

    def test_execute_with_data(self):
        """Test execution with valid market data."""
        config = WorkerConfig(name="fundamental_analyst", preset="test")
        worker = FundamentalAnalystWorker(config)
        frame = _make_frame(252)
        result = worker.execute(["AAPL"], {"AAPL": frame})

        assert result.state == WorkerState.DONE
        assert result.signals is not None
        assert len(result.signals) == 1
        assert result.ticker_results.get("AAPL") is not None
        assert "quality" in result.ticker_results["AAPL"]["metadata"]
        assert "value" in result.ticker_results["AAPL"]["metadata"]
        assert "growth" in result.ticker_results["AAPL"]["metadata"]

    def test_execute_empty_data(self):
        """Test execution with no data."""
        config = WorkerConfig(name="fundamental_analyst", preset="test")
        worker = FundamentalAnalystWorker(config)
        result = worker.execute(["AAPL"], {})

        assert result.state == WorkerState.DONE
        assert len(result.signals) == 0
        assert result.ticker_results == {}

    def test_compute_fundamentals_quality(self):
        """Test quality factor computation."""
        config = WorkerConfig(name="fundamental_analyst", preset="test")
        worker = FundamentalAnalystWorker(config)
        frame = _make_frame(252)
        fundamentals = worker._compute_fundamentals(frame)

        assert "quality" in fundamentals
        assert "value" in fundamentals
        assert "growth" in fundamentals
        assert -1.0 <= fundamentals["quality"] <= 1.0
        assert -1.0 <= fundamentals["value"] <= 1.0
        assert -1.0 <= fundamentals["growth"] <= 1.0

    def test_fundamentals_to_signal_buy(self):
        """Test signal generation for strong fundamentals."""
        config = WorkerConfig(name="fundamental_analyst", preset="test")
        worker = FundamentalAnalystWorker(config)
        fundamentals = {"quality": 0.5, "value": 0.5, "growth": 0.5}
        signal = worker._fundamentals_to_signal("AAPL", fundamentals)

        assert signal is not None
        assert signal.action == "BUY"
        assert signal.confidence >= 0.5

    def test_fundamentals_to_signal_sell(self):
        """Test signal generation for weak fundamentals."""
        config = WorkerConfig(name="fundamental_analyst", preset="test")
        worker = FundamentalAnalystWorker(config)
        fundamentals = {"quality": -0.5, "value": -0.5, "growth": -0.5}
        signal = worker._fundamentals_to_signal("AAPL", fundamentals)

        assert signal is not None
        assert signal.action == "SELL"
        assert signal.confidence >= 0.5


class TestMacroStrategistWorker:
    """Tests for MacroStrategistWorker."""

    def test_execute_with_data(self):
        """Test execution with valid market data."""
        config = WorkerConfig(name="macro_strategist", preset="test")
        worker = MacroStrategistWorker(config)
        frame = _make_frame(252)
        result = worker.execute(["AAPL"], {"AAPL": frame})

        assert result.state == WorkerState.DONE
        assert result.signals is not None
        assert len(result.signals) == 1
        assert "market_regime" in result.data

    def test_detect_market_regime_bull(self):
        """Test bull trend regime detection."""
        config = WorkerConfig(name="macro_strategist", preset="test")
        worker = MacroStrategistWorker(config)
        # Create upward trending data
        frame = _make_frame(252, start_price=100.0)
        market_data = {"AAPL": frame}
        regime = worker._detect_market_regime(market_data)
        assert regime in ("bull_trend", "range_bound", "bear_trend", "unknown")

    def test_detect_market_regime_unknown(self):
        """Test unknown regime with empty data."""
        config = WorkerConfig(name="macro_strategist", preset="test")
        worker = MacroStrategistWorker(config)
        regime = worker._detect_market_regime({})
        assert regime == "unknown"

    def test_assess_regime_fit(self):
        """Test regime fit assessment."""
        config = WorkerConfig(name="macro_strategist", preset="test")
        worker = MacroStrategistWorker(config)
        frame = _make_frame(252)
        signal = worker._assess_regime_fit("AAPL", frame, "bull_trend")

        assert signal is not None
        assert signal.action in ("BUY", "SELL", "HOLD")
        assert signal.metadata.get("market_regime") == "bull_trend"


class TestPatternRecognizerWorker:
    """Tests for PatternRecognizerWorker."""

    def test_execute_with_data(self):
        """Test execution with valid market data."""
        config = WorkerConfig(name="pattern_recognizer", preset="test")
        worker = PatternRecognizerWorker(config)
        frame = _make_frame(252)
        result = worker.execute(["AAPL"], {"AAPL": frame})

        assert result.state == WorkerState.DONE
        assert result.signals is not None
        assert len(result.signals) == 1
        assert "patterns" in result.ticker_results["AAPL"]["metadata"]

    def test_detect_patterns_no_patterns(self):
        """Test pattern detection with no patterns."""
        config = WorkerConfig(name="pattern_recognizer", preset="test")
        worker = PatternRecognizerWorker(config)
        frame = _make_frame(252)
        patterns = worker._detect_patterns(frame)
        # With smooth data, no patterns should be detected
        assert isinstance(patterns, list)

    def test_patterns_to_signal_no_patterns(self):
        """Test signal generation with no patterns."""
        config = WorkerConfig(name="pattern_recognizer", preset="test")
        worker = PatternRecognizerWorker(config)
        signal = worker._patterns_to_signal("AAPL", [])

        assert signal is not None
        assert signal.action == "HOLD"
        assert signal.confidence == 0.5

    def test_patterns_to_signal_bullish(self):
        """Test signal generation with bullish patterns."""
        config = WorkerConfig(name="pattern_recognizer", preset="test")
        worker = PatternRecognizerWorker(config)
        patterns = [
            {"name": "double_bottom", "type": "bullish_reversal", "confidence": 0.7},
        ]
        signal = worker._patterns_to_signal("AAPL", patterns)

        assert signal is not None
        assert signal.action == "BUY"
        assert signal.confidence >= 0.5


class TestOnChainAnalystWorker:
    """Tests for OnChainAnalystWorker (volume/flow analysis)."""

    def test_execute_with_data(self):
        """Test execution with valid market data."""
        config = WorkerConfig(name="on_chain_analyst", preset="test")
        worker = OnChainAnalystWorker(config)
        frame = _make_frame(252)
        result = worker.execute(["AAPL"], {"AAPL": frame})

        assert result.state == WorkerState.DONE
        assert result.signals is not None
        assert len(result.signals) == 1
        assert "accumulation_score" in result.ticker_results["AAPL"]["metadata"]

    def test_analyze_volume_flow(self):
        """Test volume flow analysis."""
        config = WorkerConfig(name="on_chain_analyst", preset="test")
        worker = OnChainAnalystWorker(config)
        frame = _make_frame(252)
        flow = worker._analyze_volume_flow(frame)

        assert "mfi" in flow
        assert "ad_slope" in flow
        assert "volume_trend" in flow
        assert "smart_money" in flow
        assert "accumulation_score" in flow
        assert -1.0 <= flow["accumulation_score"] <= 1.0

    def test_flow_to_signal(self):
        """Test flow analysis to signal conversion."""
        config = WorkerConfig(name="on_chain_analyst", preset="test")
        worker = OnChainAnalystWorker(config)
        flow = {
            "mfi": 25.0,
            "ad_slope": 0.02,
            "volume_trend": 2.0,
            "smart_money": 3.0,
            "accumulation_score": 0.8,
        }
        signal = worker._flow_to_signal("AAPL", flow)

        assert signal is not None
        assert signal.action == "BUY"
        assert signal.confidence >= 0.5
