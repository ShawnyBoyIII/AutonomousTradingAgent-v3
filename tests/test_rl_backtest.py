from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from trading_bot.rl.backtest import RLBacktestConfig, RLBacktestRunner


class TestRLBacktestConfig:
    def test_default_config(self):
        config = RLBacktestConfig()
        assert config.model_path is None
        assert config.symbols == ["AAPL"]
        assert config.starting_cash == 10_000.0
        assert config.observer_window == 10

    def test_custom_config(self):
        config = RLBacktestConfig(
            model_path="/path/to/model",
            symbols=["SPY", "QQQ"],
            starting_cash=50_000.0,
            observer_window=20,
        )
        assert config.model_path == "/path/to/model"
        assert config.symbols == ["SPY", "QQQ"]
        assert config.starting_cash == 50_000.0
        assert config.observer_window == 20


class TestRLBacktestRunner:
    @pytest.fixture
    def sample_daily_frame(self):
        dates = pd.date_range("2024-01-01", periods=200, freq="1D")
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(200) * 0.5)
        return pd.DataFrame({
            "timestamp": dates,
            "open": close + np.random.randn(200) * 0.1,
            "high": close + abs(np.random.randn(200)) * 0.5,
            "low": close - abs(np.random.randn(200)) * 0.5,
            "close": close,
            "volume": np.random.randint(1000000, 10000000, 200),
        })

    @pytest.fixture
    def sample_intraday_frame(self):
        dates = pd.date_range("2024-01-01", periods=300, freq="1D")
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(300) * 0.3)
        return pd.DataFrame({
            "timestamp": dates,
            "open": close + np.random.randn(300) * 0.1,
            "high": close + abs(np.random.randn(300)) * 0.3,
            "low": close - abs(np.random.randn(300)) * 0.3,
            "close": close,
            "volume": np.random.randint(500000, 5000000, 300),
        })

    def test_init_with_default_config(self):
        runner = RLBacktestRunner()
        assert runner.config is not None
        assert runner._model is None
        assert runner._data_cache == {}

    def test_init_with_custom_config(self):
        config = RLBacktestConfig(
            symbols=["SPY", "AAPL"],
            starting_cash=25_000.0,
            observer_window=15,
        )
        runner = RLBacktestRunner(config=config)
        assert runner.config.symbols == ["SPY", "AAPL"]
        assert runner.config.starting_cash == 25_000.0
        assert runner.config.observer_window == 15

    def test_load_model_raises_without_path(self):
        runner = RLBacktestRunner()
        with pytest.raises(ValueError, match="No model path specified"):
            runner.load_model()

    def test_set_model(self):
        mock_model = MagicMock()
        runner = RLBacktestRunner()
        runner.set_model(mock_model)
        assert runner._model is mock_model

    def test_build_observation_shape(self, sample_intraday_frame):
        config = RLBacktestConfig(observer_window=10)
        runner = RLBacktestRunner(config=config)

        mock_portfolio = MagicMock()
        mock_portfolio.equity = 10000.0
        mock_portfolio.cash = 10000.0
        mock_portfolio.unrealized_pnl = 0.0
        mock_portfolio.realized_pnl = 0.0
        mock_portfolio.positions = {}

        observation = runner._build_observation("AAPL", sample_intraday_frame.iloc[:15], mock_portfolio)
        assert observation.shape == (10, 18)
        assert observation.dtype == np.float32

    def test_build_observation_with_positions(self, sample_intraday_frame):
        config = RLBacktestConfig(observer_window=10)
        runner = RLBacktestRunner(config=config)

        from trading_bot.models.portfolio import PortfolioState, Position
        portfolio = PortfolioState(
            cash=5000.0,
            equity=8000.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=10, average_cost=100.0)},
            unrealized_pnl=500.0,
            realized_pnl=200.0,
        )

        observation = runner._build_observation("AAPL", sample_intraday_frame.iloc[:15], portfolio)
        assert observation.shape == (10, 18)
        assert np.isfinite(observation).all()

    def test_run_backtest_short_frame(self, sample_intraday_frame):
        config = RLBacktestConfig(observer_window=10)
        runner = RLBacktestRunner(config=config)
        runner.set_model(MagicMock())

        short_frame = sample_intraday_frame.iloc[:10]
        result = runner.run_backtest("AAPL", short_frame, short_frame)
        assert result["trades"] == 0
        assert result["net_pnl"] == 0.0

    def test_run_backtest_without_model(self, sample_intraday_frame):
        config = RLBacktestConfig(observer_window=10)
        runner = RLBacktestRunner(config=config)

        with pytest.raises(RuntimeError, match="No model loaded"):
            runner._predict_action(np.zeros((10, 18), dtype=np.float32))

    def test_run_backtest_with_mock_model(self, sample_intraday_frame):
        config = RLBacktestConfig(
            observer_window=10,
            symbols=["AAPL"],
            max_shares=50,
        )
        runner = RLBacktestRunner(config=config)

        mock_model = MagicMock()
        mock_model.predict.return_value = (0, None)
        runner.set_model(mock_model)

        result = runner.run_backtest("AAPL", sample_intraday_frame, sample_intraday_frame)
        assert "trades" in result
        assert "wins" in result
        assert "losses" in result
        assert "net_pnl" in result
        assert "rl_actions" in result
        assert isinstance(result["rl_actions"], list)

    def test_action_to_trade_no_action(self):
        config = RLBacktestConfig(symbols=["AAPL"])
        runner = RLBacktestRunner(config=config)

        from trading_bot.execution.paper_broker import PaperBroker
        broker = PaperBroker(starting_cash=10000.0, fee_per_order=1.0, slippage_bps=0)
        prices = {"AAPL": 100.0}

        trade_type, trade_price, proportion = runner._action_to_trade(0, "AAPL", prices, broker)
        assert trade_type is None
        assert trade_price is None
        assert proportion == 1.0

    def test_action_to_trade_buy(self):
        config = RLBacktestConfig(symbols=["AAPL"])
        runner = RLBacktestRunner(config=config)

        from trading_bot.execution.paper_broker import PaperBroker
        broker = PaperBroker(starting_cash=10000.0, fee_per_order=1.0, slippage_bps=0)
        prices = {"AAPL": 100.0}

        action = 2  # BUY for AAPL (action=1 is HOLD for AAPL)
        trade_type, trade_price, proportion = runner._action_to_trade(action, "AAPL", prices, broker)
        assert trade_type == "BUY"
        assert trade_price == 100.0
        assert proportion == 1.0

    def test_action_to_trade_sell_no_position(self):
        config = RLBacktestConfig(symbols=["AAPL"])
        runner = RLBacktestRunner(config=config)

        from trading_bot.execution.paper_broker import PaperBroker
        broker = PaperBroker(starting_cash=10000.0, fee_per_order=1.0, slippage_bps=0)
        prices = {"AAPL": 100.0}

        action = 3  # SELL for AAPL (action=2 is BUY, action=3 is SELL)
        trade_type, trade_price, proportion = runner._action_to_trade(action, "AAPL", prices, broker)
        assert trade_type is None
        assert trade_price is None
        assert proportion == 1.0

    def test_action_to_trade_sell_with_position(self):
        config = RLBacktestConfig(symbols=["AAPL"])
        runner = RLBacktestRunner(config=config)

        from trading_bot.execution.paper_broker import PaperBroker
        broker = PaperBroker(starting_cash=10000.0, fee_per_order=1.0, slippage_bps=0)
        broker.positions["AAPL"] = 10
        prices = {"AAPL": 100.0}

        action = 3  # SELL for AAPL
        trade_type, trade_price, proportion = runner._action_to_trade(action, "AAPL", prices, broker)
        assert trade_type == "SELL"
        assert trade_price == 100.0
        assert proportion == 1.0

    def test_action_to_trade_invalid_symbol(self):
        config = RLBacktestConfig(symbols=["AAPL"])
        runner = RLBacktestRunner(config=config)

        from trading_bot.execution.paper_broker import PaperBroker
        broker = PaperBroker(starting_cash=10000.0, fee_per_order=1.0, slippage_bps=0)
        prices = {"SPY": 100.0}

        trade_type, trade_price, proportion = runner._action_to_trade(1, "SPY", prices, broker)
        assert trade_type is None
        assert trade_price is None
        assert proportion == 1.0

    def test_resolve_exit_stop_loss_hit(self):
        config = RLBacktestConfig()
        runner = RLBacktestRunner(config=config)

        dates = pd.date_range("2024-01-01", periods=100, freq="1D")
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(100) * 0.5)
        df = pd.DataFrame({
            "timestamp": dates,
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.random.randint(1000000, 10000000, 100),
        })

        entry_index = 50
        stop_loss = float(df.iloc[50]["close"]) - 5.0
        profit_target = float(df.iloc[50]["close"]) + 5.0

        exit_price, exit_index = runner._resolve_exit(df, entry_index, stop_loss, profit_target)
        assert exit_index > entry_index

    def test_resolve_exit_no_exit(self):
        config = RLBacktestConfig()
        runner = RLBacktestRunner(config=config)

        dates = pd.date_range("2024-01-01", periods=100, freq="1D")
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(100) * 0.5)
        df = pd.DataFrame({
            "timestamp": dates,
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.random.randint(1000000, 10000000, 100),
        })

        entry_index = len(df) - 5
        stop_loss = 1.0
        profit_target = 10000.0

        exit_price, exit_index = runner._resolve_exit(df, entry_index, stop_loss, profit_target)
        assert exit_index >= entry_index
        assert exit_index < len(df)

    def test_load_symbols(self, sample_daily_frame):
        config = RLBacktestConfig(symbols=["AAPL", "MSFT"])
        runner = RLBacktestRunner(config=config)

        runner._load_symbols(["AAPL", "MSFT"], sample_daily_frame, sample_daily_frame)
        assert "AAPL" in runner._data_cache
        assert "MSFT" in runner._data_cache
        assert runner._data_indices["AAPL"] == 0

    def test_run_backtest_returns_correct_keys(self, sample_intraday_frame):
        config = RLBacktestConfig(observer_window=10, symbols=["AAPL"])
        runner = RLBacktestRunner(config=config)

        mock_model = MagicMock()
        mock_model.predict.return_value = (0, None)
        runner.set_model(mock_model)

        result = runner.run_backtest("AAPL", sample_intraday_frame, sample_intraday_frame)
        assert "trades" in result
        assert "wins" in result
        assert "losses" in result
        assert "net_pnl" in result
        assert "win_rate" in result
        assert "rl_actions" in result


class TestStrategyComparison:
    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock()
        settings.market_data.daily_period = "1y"
        settings.market_data.intraday_period = "6mo"
        settings.market_data.intraday_interval = "1d"
        settings.paper.fee_per_order = 1.0
        settings.paper.slippage_bps = 5
        settings.app.log_dir = "/tmp/test_logs"
        settings.app.backtest_summary_path = "/tmp/test_summary.json"
        settings.strategy.use_v3_signals = True
        settings.rl.enabled = True
        settings.rl.model_path = "/tmp/test_model"
        settings.rl.backtest_starting_cash = 100_000.0
        settings.rl.backtest_max_shares = 150
        settings.rl.backtest_stop_loss_pct = 0.03
        settings.rl.backtest_profit_target_pct = 0.08
        return settings

    def test_run_strategy_comparison_detects_strategies(self, mock_settings):
        from trading_bot.backtest.runner import run_strategy_comparison

        with patch("trading_bot.backtest.runner.run_backtest") as mock_bt, \
             patch("trading_bot.backtest.runner.run_rl_backtest") as mock_rl:

            mock_bt.return_value = {
                "trades": 10, "wins": 6, "losses": 4,
                "net_pnl": 500.0, "win_rate": 0.6, "rows": [],
            }
            mock_rl.return_value = {
                "trades": 8, "wins": 5, "losses": 3,
                "net_pnl": 400.0, "win_rate": 0.625, "rows": [],
            }

            comparison = run_strategy_comparison(
                ["AAPL", "MSFT"], mock_settings, start="2024-01-01", end="2024-12-31"
            )

            assert "results" in comparison
            assert "summary" in comparison
            assert "best_pnl_strategy" in comparison
            assert "best_winrate_strategy" in comparison
            assert "AAPL" in str(comparison) or "MSFT" in str(comparison)

    def test_run_strategy_comparison_toggles_v3_flag(self, mock_settings):
        from trading_bot.backtest.runner import run_strategy_comparison

        seen_flags = []

        def fake_backtest(symbols, settings, start=None, end=None):
            seen_flags.append(settings.strategy.use_v3_signals)
            return {
                "trades": 1, "wins": 1, "losses": 0,
                "net_pnl": 1.0, "win_rate": 1.0, "rows": [],
            }

        with patch("trading_bot.backtest.runner.run_backtest", side_effect=fake_backtest), \
             patch("trading_bot.backtest.runner.run_rl_backtest") as mock_rl:
            mock_rl.return_value = {
                "trades": 0, "wins": 0, "losses": 0,
                "net_pnl": 0.0, "win_rate": 0.0, "rows": [],
            }

            run_strategy_comparison(
                ["AAPL"], mock_settings, start="2024-01-01", end="2024-12-31", strategies=["v2.5", "v3", "rl"]
            )

        assert seen_flags == [False, True]

    def test_run_rl_backtest_uses_configured_sizing(self, mock_settings, tmp_path):
        from trading_bot.backtest.runner import run_rl_backtest

        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=30, freq="1D"),
                "open": [100.0] * 30,
                "high": [101.0] * 30,
                "low": [99.0] * 30,
                "close": [100.0] * 30,
                "volume": [1_000_000] * 30,
            }
        )
        model_path = tmp_path / "PPO_final.zip"
        model_path.write_text("fake", encoding="utf-8")
        meta_path = tmp_path / "PPO_final_meta.json"
        meta_path.write_text(json.dumps({"symbols": ["AAPL"], "action_scheme": "proportion"}), encoding="utf-8")
        mock_settings.rl.model_path = str(model_path)
        seen = {}

        class FakeRunner:
            FEATURE_COLS = RLBacktestRunner.FEATURE_COLS

            def __init__(self, config):
                seen["starting_cash"] = config.starting_cash
                seen["max_shares"] = config.max_shares
                seen["stop_loss_pct"] = config.stop_loss_pct
                seen["profit_target_pct"] = config.profit_target_pct
                seen["action_scheme"] = config.action_scheme
                seen["model_path"] = config.model_path
                self.config = config
                self._model = MagicMock()
                self._model.observation_space.shape = (
                    config.observer_window,
                    len(config.symbols) * len(self.FEATURE_COLS) + 5,
                )

            def load_model(self):
                return None

            def run_backtest(self, **_kwargs):
                return {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "net_pnl": 0.0,
                    "win_rate": 0.0,
                    "gross_profit": 0.0,
                    "gross_loss": 0.0,
                    "rl_actions": [],
                }

        with patch("trading_bot.rl.backtest.RLBacktestRunner", FakeRunner), \
             patch("trading_bot.backtest.runner._fetch_rl_frames", return_value=({"AAPL": frame}, {"AAPL": frame})), \
             patch("trading_bot.backtest.runner.append_decision_event"), \
             patch("trading_bot.backtest.runner._write_rl_summary"):
            run_rl_backtest(["AAPL"], mock_settings, start="2024-01-01", end="2024-12-31")

        assert seen["starting_cash"] == 100_000.0
        assert seen["max_shares"] == 150
        assert seen["stop_loss_pct"] == 0.03
        assert seen["profit_target_pct"] == 0.08
        assert seen["action_scheme"] == "proportion"
        assert seen["model_path"] == str(model_path)

    def test_run_rl_backtest_runs_multisymbol_model_once(self, mock_settings, tmp_path):
        from trading_bot.backtest.runner import run_rl_backtest
        from trading_bot.rl.features import CROSS_SYMBOL_FEATURES

        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=30, freq="1D"),
                "open": [100.0] * 30,
                "high": [101.0] * 30,
                "low": [99.0] * 30,
                "close": [100.0] * 30,
                "volume": [1_000_000] * 30,
            }
        )
        model_path = tmp_path / "PPO_final.zip"
        model_path.write_text("fake", encoding="utf-8")
        meta_path = tmp_path / "PPO_final_meta.json"
        meta_path.write_text(json.dumps({"symbols": ["AAPL", "MSFT"]}), encoding="utf-8")
        mock_settings.rl.model_path = str(model_path)
        calls = []

        class FakeRunner:
            FEATURE_COLS = RLBacktestRunner.FEATURE_COLS

            def __init__(self, config):
                self.config = config
                self._model = MagicMock()
                self._model.observation_space.shape = (
                    config.observer_window,
                    len(config.symbols) * (len(self.FEATURE_COLS) + len(CROSS_SYMBOL_FEATURES)) + 5,
                )

            def load_model(self):
                return None

            def run_backtest(self, **kwargs):
                calls.append(kwargs)
                return {
                    "trades": 2,
                    "wins": 1,
                    "losses": 1,
                    "net_pnl": 10.0,
                    "win_rate": 0.5,
                    "gross_profit": 20.0,
                    "gross_loss": -10.0,
                    "rl_actions": [],
                }

        with patch("trading_bot.rl.backtest.RLBacktestRunner", FakeRunner), \
             patch("trading_bot.backtest.runner._fetch_rl_frames", return_value=({"AAPL": frame, "MSFT": frame}, {"AAPL": frame, "MSFT": frame})), \
             patch("trading_bot.backtest.runner.append_decision_event"), \
             patch("trading_bot.backtest.runner._write_rl_summary"):
            result = run_rl_backtest(["AAPL", "MSFT"], mock_settings)

        assert len(calls) == 1
        assert calls[0]["trade_symbols"] == ["AAPL", "MSFT"]
        assert result["trades"] == 2
        assert result["net_pnl"] == 10.0

    def test_run_rl_backtest_infers_missing_max_symbols_from_model_shape(self, mock_settings, tmp_path):
        from trading_bot.backtest.runner import run_rl_backtest
        from trading_bot.rl.features import CROSS_SYMBOL_FEATURES

        symbols = ["XOM", "CVX", "UNH", "LLY", "CAT", "DE"]
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=30, freq="1D"),
                "open": [100.0] * 30,
                "high": [101.0] * 30,
                "low": [99.0] * 30,
                "close": [100.0] * 30,
                "volume": [1_000_000] * 30,
            }
        )
        model_path = tmp_path / "PPO_seed_789.zip"
        model_path.write_text("fake", encoding="utf-8")
        (tmp_path / "PPO_seed_789_meta.json").write_text(
            json.dumps({"symbols": symbols, "action_scheme": "proportion"}),
            encoding="utf-8",
        )
        mock_settings.rl.model_path = str(model_path)
        seen = {}

        class FakeRunner:
            FEATURE_COLS = RLBacktestRunner.FEATURE_COLS

            def __init__(self, config):
                self.config = config
                self._model = MagicMock()
                self._model.observation_space.shape = (
                    config.observer_window,
                    len(symbols) * (len(self.FEATURE_COLS) + len(CROSS_SYMBOL_FEATURES)) + 5,
                )

            def load_model(self):
                return None

            def run_backtest(self, **_kwargs):
                seen["max_symbols"] = self.config.max_symbols
                return {
                    "trades": 1,
                    "wins": 1,
                    "losses": 0,
                    "net_pnl": 1.0,
                    "win_rate": 1.0,
                    "gross_profit": 1.0,
                    "gross_loss": 0.0,
                    "rl_actions": [],
                }

        with patch("trading_bot.rl.backtest.RLBacktestRunner", FakeRunner), \
             patch("trading_bot.backtest.runner._fetch_rl_frames", return_value=({s: frame for s in symbols}, {s: frame for s in symbols})), \
             patch("trading_bot.backtest.runner.append_decision_event"), \
             patch("trading_bot.backtest.runner._write_rl_summary"):
            result = run_rl_backtest(symbols, mock_settings)

        assert seen["max_symbols"] == len(symbols)
        assert result["trades"] == 1
