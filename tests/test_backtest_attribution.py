"""Tests for backtest attribution analysis (487 lines)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from trading_bot.backtest.attribution import (
    _beta_regression,
    _exit_reason_attribution,
    _holding_period_analysis,
    _interpret_beta_alpha,
    _interpret_monte_carlo,
    _monte_carlo_simulation,
    _regime_analysis,
    _signal_quality_attribution,
    _swarm_sentiment_attribution,
    _trade_level_attribution,
    _winner_loser_analysis,
    run_attribution,
)


def _base_result(**overrides) -> dict:
    result = {
        "rows": [
            {"ticker": "AAPL", "trades": 10, "wins": 7, "losses": 3, "net_pnl": 5000.0},
            {"ticker": "GOOGL", "trades": 5, "wins": 2, "losses": 3, "net_pnl": -2000.0},
        ],
        "trades": 15,
        "wins": 9,
        "losses": 6,
        "net_pnl": 3000.0,
        "avg_win": 600.0,
        "avg_loss": -400.0,
        "gross_profit": 5400.0,
        "gross_loss": -2400.0,
    }
    result.update(overrides)
    return result


class TestTradeLevelAttribution:
    def test_basic_attribution(self):
        result = _trade_level_attribution(_base_result())
        assert result["total_trades"] == 15
        assert result["total_pnl"] == 3000.0
        assert result["total_wins"] == 9
        assert result["total_losses"] == 6
        assert result["win_rate"] == 60.0
        assert result["top_contributor"] == "AAPL"
        assert result["worst_contributor"] == "GOOGL"

    def test_ticker_contributions_sorted(self):
        result = _trade_level_attribution(_base_result())
        contributions = result["ticker_contributions"]
        assert len(contributions) == 2
        assert contributions[0]["ticker"] == "AAPL"
        assert contributions[1]["ticker"] == "GOOGL"

    def test_contribution_percentages(self):
        result = _trade_level_attribution(_base_result())
        aapl = [t for t in result["ticker_contributions"] if t["ticker"] == "AAPL"][0]
        googl = [t for t in result["ticker_contributions"] if t["ticker"] == "GOOGL"][0]
        assert aapl["contribution_pct"] == 166.7  # 5000/3000*100
        assert googl["contribution_pct"] == -66.7  # -2000/3000*100

    def test_zero_pnl_no_division_by_zero(self):
        result = _trade_level_attribution({
            "rows": [{"ticker": "AAPL", "trades": 5, "wins": 3, "losses": 2, "net_pnl": 0.0}],
            "trades": 5,
            "wins": 3,
            "losses": 2,
            "net_pnl": 0.0,
        })
        assert result["win_rate"] == 60.0
        # When net_pnl is 0, contribution_pct is 0.0 but ticker is still included
        assert result["top_contributor"] == "AAPL"
        assert result["worst_contributor"] == "AAPL"

    def test_zero_trades_no_division_by_zero(self):
        result = _trade_level_attribution({
            "rows": [],
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "net_pnl": 0.0,
        })
        assert result["total_trades"] == 0
        assert result["win_rate"] == 0.0
        assert result["top_contributor"] is None


class TestWinnerLoserAnalysis:
    def test_basic_analysis(self):
        result = _winner_loser_analysis(_base_result())
        assert result["avg_win"] == 600.0
        assert result["avg_loss"] == -400.0
        assert result["win_loss_ratio"] == 1.5
        assert result["profit_factor"] == 2.25
        assert result["expectancy"] == 200.0

    def test_zero_loss_infinite_ratio(self):
        result = _winner_loser_analysis({
            "avg_win": 500.0,
            "avg_loss": 0.0,
            "gross_profit": 5000.0,
            "gross_loss": 0.0,
            "wins": 10,
            "losses": 0,
            "trades": 10,
            "net_pnl": 5000.0,
        })
        # win_loss_ratio is None (not inf) to remain JSON-serializable
        assert result["win_loss_ratio"] is None
        assert result["profit_factor"] == 5000.0

    def test_zero_profit_factor(self):
        result = _winner_loser_analysis({
            "avg_win": 0.0,
            "avg_loss": -100.0,
            "gross_profit": 0.0,
            "gross_loss": -1000.0,
            "wins": 0,
            "losses": 10,
            "trades": 10,
            "net_pnl": -1000.0,
        })
        assert result["profit_factor"] == 0.0

    def test_expectancy(self):
        result = _winner_loser_analysis({
            "avg_win": 100.0,
            "avg_loss": -50.0,
            "gross_profit": 1000.0,
            "gross_loss": -500.0,
            "wins": 10,
            "losses": 10,
            "trades": 20,
            "net_pnl": 500.0,
        })
        assert result["expectancy"] == 25.0


class TestHoldingPeriodAnalysis:
    def test_no_windows(self):
        result = _holding_period_analysis({"rows": [], "trades": 5})
        assert "note" in result
        assert result["windows_available"] is False

    def test_with_windows(self):
        windows = [
            {"trades": 10, "net_pnl": 1000.0},
            {"trades": 5, "net_pnl": 500.0},
            {"trades": 8, "net_pnl": 800.0},
        ]
        result = _holding_period_analysis({"rows": [], "windows": windows})
        assert result["num_windows"] == 3
        assert result["avg_trades_per_window"] == 7.7
        assert result["pnl_per_trade_mean"] == 100.0

    def test_window_with_zero_trades(self):
        windows = [
            {"trades": 10, "net_pnl": 1000.0},
            {"trades": 0, "net_pnl": 0.0},
        ]
        result = _holding_period_analysis({"rows": [], "windows": windows})
        assert result["num_windows"] == 2
        assert result["pnl_per_trade_mean"] == 100.0


class TestExitReasonAttribution:
    def test_exit_reasons_estimated(self):
        result = _exit_reason_attribution(_base_result())
        assert "ticker_exit_stats" in result
        assert "AAPL" in result["ticker_exit_stats"]
        assert "GOOGL" in result["ticker_exit_stats"]

    def test_aapl_exit_stats(self):
        result = _exit_reason_attribution(_base_result())
        aapl = result["ticker_exit_stats"]["AAPL"]
        assert aapl["estimated_profit_targets"] == 4  # int(7 * 0.7)
        assert aapl["estimated_stop_losses"] == 2  # int(3 * 0.8)
        assert aapl["estimated_time_exits"] == 1  # max(0, 3 - 2)

    def test_googl_exit_stats(self):
        result = _exit_reason_attribution(_base_result())
        googl = result["ticker_exit_stats"]["GOOGL"]
        assert googl["estimated_profit_targets"] == 1  # 2 * 0.7
        assert googl["estimated_stop_losses"] == 2  # 3 * 0.8


class TestSignalQualityAttribution:
    def test_quality_tiers(self):
        result = _signal_quality_attribution(_base_result())
        assert "ticker_quality" in result
        assert "tier_summary" in result
        # AAPL has 70% win rate -> high tier
        assert "high" in result["tier_summary"]

    def test_quality_rating_high(self):
        result = _signal_quality_attribution({
            "rows": [{"ticker": "AAPL", "trades": 10, "wins": 8, "losses": 2}],
        })
        aapl_quality = [t for t in result["ticker_quality"] if t["ticker"] == "AAPL"][0]
        assert aapl_quality["quality_rating"] == "high"
        assert aapl_quality["win_rate"] == 80.0

    def test_quality_rating_medium(self):
        result = _signal_quality_attribution({
            "rows": [{"ticker": "AAPL", "trades": 10, "wins": 5, "losses": 5}],
        })
        aapl_quality = [t for t in result["ticker_quality"] if t["ticker"] == "AAPL"][0]
        assert aapl_quality["quality_rating"] == "medium"

    def test_quality_rating_low(self):
        result = _signal_quality_attribution({
            "rows": [{"ticker": "AAPL", "trades": 10, "wins": 3, "losses": 7}],
        })
        aapl_quality = [t for t in result["ticker_quality"] if t["ticker"] == "AAPL"][0]
        assert aapl_quality["quality_rating"] == "low"

    def test_zero_trades(self):
        result = _signal_quality_attribution({
            "rows": [{"ticker": "AAPL", "trades": 0, "wins": 0, "losses": 0}],
        })
        aapl_quality = [t for t in result["ticker_quality"] if t["ticker"] == "AAPL"][0]
        assert aapl_quality["win_rate"] == 0.0


class TestSwarmSentimentAttribution:
    def test_returns_note_when_missing(self):
        result = _swarm_sentiment_attribution(_base_result())
        assert result["rows_with_sentiment"] == 0
        assert "note" in result

    def test_groups_rows_by_sentiment_bucket(self):
        result = _swarm_sentiment_attribution(
            {
                "rows": [
                    {"ticker": "AAPL", "trades": 10, "wins": 7, "net_pnl": 500.0, "swarm_sentiment_score": 0.5},
                    {"ticker": "MSFT", "trades": 8, "wins": 3, "net_pnl": -200.0, "swarm_sentiment_score": -0.4},
                    {"ticker": "SPY", "trades": 6, "wins": 3, "net_pnl": 50.0, "swarm_sentiment_score": 0.0},
                ]
            }
        )

        assert result["rows_with_sentiment"] == 3
        assert result["bucket_summary"]["bullish"]["tickers"] == 1
        assert result["bucket_summary"]["bearish"]["tickers"] == 1
        assert result["bucket_summary"]["neutral"]["tickers"] == 1


class TestBetaRegression:
    def test_basic_beta_degraded_without_strategy_returns(self):
        """Beta regression degrades gracefully without strategy return series."""
        benchmark = pd.DataFrame({
            "close": [100 + i for i in range(60)],
        })
        result = _beta_regression({
            "net_pnl": 3000.0,
            "trades": 15,
            "gross_profit": 5400.0,
            "gross_loss": -2400.0,
        }, benchmark)
        # Without strategy_returns, returns degraded path with note
        assert "note" in result
        assert "strategy_returns" in result["note"]
        assert "sharpe_proxy" in result

    def test_beta_with_strategy_returns(self):
        """Beta regression works when strategy_returns is provided."""
        benchmark = pd.DataFrame({
            "close": [100 + i for i in range(60)],
        }, index=pd.RangeIndex(60))
        benchmark_returns = benchmark["close"].pct_change().dropna()
        # Fabricate strategy returns aligned to benchmark
        strategy_returns = pd.Series(
            benchmark_returns.values * 1.2 + 0.0001,
            index=benchmark_returns.index,
        )
        result = _beta_regression({
            "net_pnl": 3000.0,
            "trades": 15,
            "strategy_returns": strategy_returns,
        }, benchmark)
        assert "beta" in result
        assert "alpha" in result
        assert "sharpe_ratio" in result
        assert "interpretation" in result

    def test_insufficient_benchmark_data(self):
        benchmark = pd.DataFrame({"close": [100, 101, 102]})
        result = _beta_regression({
            "net_pnl": 3000.0,
            "trades": 15,
        }, benchmark)
        assert "note" in result
        assert "Insufficient" in result["note"]

    def test_no_trades(self):
        benchmark = pd.DataFrame({
            "close": list(range(100, 150)),
        })
        result = _beta_regression({
            "net_pnl": 3000.0,
            "trades": 0,
        }, benchmark)
        assert "note" in result
        assert "No trades" in result["note"]

    def test_beta_regression_exception(self, caplog):
        benchmark = pd.DataFrame({"close": []})
        with caplog.at_level("WARNING"):
            result = _beta_regression({
                "net_pnl": 3000.0,
                "trades": 15,
            }, benchmark)
        assert "note" in result
        # Empty benchmark returns "Insufficient benchmark data" before exception
        assert "Insufficient" in result["note"] or "failed" in result["note"]


class TestRegimeAnalysis:
    def test_basic_regime(self):
        closes = list(range(100, 200))
        benchmark = pd.DataFrame({"close": closes, "volume": [1000] * len(closes)})
        result = _regime_analysis({
            "net_pnl": 3000.0,
            "trades": 15,
        }, benchmark)
        assert "total_bars_analyzed" in result
        assert "regime_distribution" in result
        assert "regime_pnl_estimates" in result

    def test_insufficient_data(self):
        benchmark = pd.DataFrame({"close": [100, 101, 102]})
        result = _regime_analysis({
            "net_pnl": 3000.0,
            "trades": 15,
        }, benchmark)
        assert "note" in result
        assert "Insufficient" in result["note"]

    def test_regime_distribution(self):
        # Create a trending market (bullish)
        closes = [100 + i * 2 for i in range(100)]
        benchmark = pd.DataFrame({"close": closes, "volume": [1000] * 100})
        result = _regime_analysis({
            "net_pnl": 3000.0,
            "trades": 15,
        }, benchmark)
        # Should have bullish regime due to uptrend
        assert result["regime_distribution"]["bullish"] > 0

    def test_regime_analysis_exception(self, caplog):
        with caplog.at_level("WARNING"):
            result = _regime_analysis({
                "net_pnl": 3000.0,
                "trades": 15,
            }, pd.DataFrame())
        assert "note" in result


class TestMonteCarloSimulation:
    def test_basic_monte_carlo(self):
        result = _monte_carlo_simulation({
            "trades": 50,
            "net_pnl": 5000.0,
            "wins": 30,
            "losses": 20,
            "avg_win": 200.0,
            "avg_loss": -100.0,
        }, num_simulations=100)
        assert "num_simulations" in result
        assert "original_pnl" in result
        assert "probability_of_profit" in result
        assert "confidence_intervals" in result
        assert "max_drawdown_simulation" in result

    def test_no_trades(self):
        result = _monte_carlo_simulation({
            "trades": 0,
            "net_pnl": 0.0,
        })
        assert "note" in result
        assert "No trades" in result["note"]

    def test_confidence_intervals_present(self):
        result = _monte_carlo_simulation({
            "trades": 50,
            "net_pnl": 5000.0,
            "wins": 30,
            "losses": 20,
            "avg_win": 200.0,
            "avg_loss": -100.0,
        }, num_simulations=1000, confidence_levels=[0.90, 0.95, 0.99])
        assert "90%_ci" in result["confidence_intervals"]
        assert "95%_ci" in result["confidence_intervals"]
        assert "99%_ci" in result["confidence_intervals"]

    def test_probability_of_profit(self):
        result = _monte_carlo_simulation({
            "trades": 100,
            "net_pnl": 10000.0,
            "wins": 70,
            "losses": 30,
            "avg_win": 200.0,
            "avg_loss": -50.0,
        }, num_simulations=1000)
        # With strong positive expectancy, prob of profit should be high
        assert result["probability_of_profit"] > 0.8

    def test_monte_carlo_exception(self, caplog):
        with caplog.at_level("WARNING"):
            result = _monte_carlo_simulation({})
        assert "note" in result


class TestInterpretFunctions:
    def test_interpret_beta_alpha_high_vol_positive_alpha(self):
        result = _interpret_beta_alpha(1.5, 0.02)
        assert "highly volatile vs market" in result
        assert "positive alpha" in result

    def test_interpret_beta_alpha_low_vol_negative_alpha(self):
        result = _interpret_beta_alpha(0.5, -0.02)
        assert "less volatile vs market" in result
        assert "negative alpha" in result

    def test_interpret_beta_alpha_market_correlated_neutral(self):
        result = _interpret_beta_alpha(1.0, 0.005)
        assert "market-correlated volatility" in result
        assert "neutral alpha" in result

    def test_interpret_monte_carlo_high_prob_low_drawdown(self):
        result = _interpret_monte_carlo(0.98, 0.05)
        assert "very high probability of profitability" in result
        assert "manageable drawdown risk" in result

    def test_interpret_monte_carlo_low_prob_high_drawdown(self):
        result = _interpret_monte_carlo(0.50, 0.25)
        assert "low probability of profitability" in result
        assert "significant drawdown risk" in result

    def test_interpret_monte_carlo_moderate(self):
        result = _interpret_monte_carlo(0.70, 0.15)
        assert "moderate probability of profitability" in result
        assert "moderate drawdown risk" in result


class TestRunAttribution:
    def test_full_attribution(self):
        benchmark = pd.DataFrame({
            "close": list(range(100, 200)),
        })
        result = run_attribution(
            _base_result(),
            benchmark_data=benchmark,
            risk_free_rate=0.03,
        )
        assert "trade_level_attribution" in result
        assert "winner_loser_analysis" in result
        assert "holding_period_analysis" in result
        assert "exit_reason_attribution" in result
        assert "signal_quality_attribution" in result
        assert "swarm_sentiment_attribution" in result
        assert "beta_regression" in result
        assert "regime_analysis" in result
        assert "monte_carlo" in result

    def test_attribution_without_benchmark(self):
        result = run_attribution(_base_result())
        assert "trade_level_attribution" in result
        assert "beta_regression" not in result
        assert "regime_analysis" not in result
        assert "monte_carlo" in result

    def test_attribution_with_empty_benchmark(self):
        result = run_attribution(
            _base_result(),
            benchmark_data=pd.DataFrame(),
        )
        assert "beta_regression" not in result
        assert "regime_analysis" not in result

    def test_attribution_uses_strategy_returns_for_capm(self):
        """When the runner supplies strategy_returns, beta regression
        performs full CAPM analysis instead of degrading gracefully."""
        benchmark = pd.DataFrame({"close": list(range(100, 200))})
        bench_returns = benchmark["close"].pct_change().dropna()
        strategy_returns = pd.Series(
            bench_returns.values * 1.2 + 0.0001,
            index=bench_returns.index,
        )
        result = run_attribution(
            _base_result(strategy_returns=strategy_returns),
            benchmark_data=benchmark,
            risk_free_rate=0.03,
        )
        beta_reg = result["beta_regression"]
        assert "beta" in beta_reg
        assert "alpha" in beta_reg
        assert "sharpe_ratio" in beta_reg
        assert "interpretation" in beta_reg
        # Not the degraded path
        assert beta_reg.get("note", "").find("strategy_returns") == -1 or "note" not in beta_reg

    def test_attribution_strategy_returns_degrades_when_insufficient(self):
        """Fewer than 20 aligned strategy returns still degrades gracefully."""
        benchmark = pd.DataFrame({"close": list(range(100, 200))})
        result = run_attribution(
            _base_result(strategy_returns=[0.001, 0.002, 0.001]),
            benchmark_data=benchmark,
        )
        beta_reg = result["beta_regression"]
        assert "note" in beta_reg
