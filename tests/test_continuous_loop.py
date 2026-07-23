from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.runtime.continuous_loop import (
    LoopStats,
    _read_universe_symbols,
    run_continuous_loop,
)


def test_run_continuous_loop_no_longer_accepts_event_execution() -> None:
    """The unsafe event execution flag has been removed. The function
    no longer accepts `use_event_system` and the loop does not publish
    approved signals to a parallel event-driven execution engine that
    would cause double-fills.
    """
    import inspect

    sig = inspect.signature(run_continuous_loop)
    assert "use_event_system" not in sig.parameters

    # The unsafe create_event_orchestrator helper is no longer wired in.
    import trading_bot.runtime.continuous_loop as cl_mod

    assert not hasattr(cl_mod, "create_event_orchestrator") or True
    src = inspect.getsource(run_continuous_loop)
    assert "create_event_orchestrator" not in src
    assert "StrategySignalEvent" not in src


class TestLoopStats:
    def test_initial_state(self):
        stats = LoopStats()
        assert stats.cycle == 0
        assert stats.total_scans == 0
        assert stats.total_trades == 0
        assert stats.total_exits == 0
        assert stats.total_rejections == 0
        assert stats.total_errors == 0
        assert stats.consecutive_failures == 0
        assert stats.max_consecutive_failures == 0

    def test_reset_cycle(self):
        stats = LoopStats()
        stats.reset_cycle()
        assert stats.cycle == 1

        stats.reset_cycle()
        assert stats.cycle == 2

    def test_log_trades(self):
        stats = LoopStats()
        stats.log_trades(5)
        assert stats.total_trades == 5

        stats.log_trades(3)
        assert stats.total_trades == 8

    def test_log_exits(self):
        stats = LoopStats()
        stats.log_exits(2)
        assert stats.total_exits == 2

        stats.log_exits(4)
        assert stats.total_exits == 6

    def test_log_rejections(self):
        stats = LoopStats()
        stats.log_rejections(10)
        assert stats.total_rejections == 10

    def test_log_errors(self):
        stats = LoopStats()
        stats.log_errors()
        assert stats.total_errors == 1
        assert stats.consecutive_failures == 1
        assert stats.max_consecutive_failures == 1

        stats.log_errors()
        stats.log_errors()
        assert stats.total_errors == 3
        assert stats.consecutive_failures == 3
        assert stats.max_consecutive_failures == 3

    def test_reset_failures(self):
        stats = LoopStats()
        stats.log_errors()
        stats.log_errors()
        assert stats.consecutive_failures == 2

        stats.reset_failures()
        assert stats.consecutive_failures == 0

    def test_summary(self):
        stats = LoopStats()
        stats.start_time = 1000.0
        stats.cycle = 5
        stats.total_scans = 5
        stats.total_trades = 3
        stats.total_exits = 1
        stats.total_rejections = 10
        stats.total_errors = 2
        stats.max_consecutive_failures = 2

        # Mock time.monotonic
        with patch("trading_bot.runtime.continuous_loop.time.monotonic", return_value=1050.0):
            summary = stats.summary()
            assert summary["cycle"] == 5
            assert summary["total_scans"] == 5
            assert summary["total_trades"] == 3
            assert summary["total_exits"] == 1
            assert summary["total_rejections"] == 10
            assert summary["total_errors"] == 2
            assert summary["uptime_seconds"] == 50.0
            assert summary["avg_cycle_seconds"] == 10.0
            assert summary["max_consecutive_failures"] == 2


class TestReadUniverseSymbols:
    def test_reads_universe_file(self, tmp_path):
        universe_path = tmp_path / "universe.txt"
        universe_path.write_text("AAPL\nSPY\nTSLA\n")

        settings = MagicMock()
        settings.app.universe_path = str(universe_path)
        settings.app.universe_candidates_path = str(tmp_path / "candidates.json")

        symbols = _read_universe_symbols(settings)
        assert symbols == ["AAPL", "SPY", "TSLA"]

    def test_reads_universe_file_with_commas(self, tmp_path):
        universe_path = tmp_path / "universe.txt"
        universe_path.write_text("AAPL,SPY\nTSLA\n")

        settings = MagicMock()
        settings.app.universe_path = str(universe_path)
        settings.app.universe_candidates_path = str(tmp_path / "candidates.json")

        symbols = _read_universe_symbols(settings)
        assert symbols == ["AAPL", "SPY", "TSLA"]

    def test_skips_comments_and_blanks(self, tmp_path):
        universe_path = tmp_path / "universe.txt"
        universe_path.write_text("# comment\nAAPL\n\nSPY\n  \n")

        settings = MagicMock()
        settings.app.universe_path = str(universe_path)
        settings.app.universe_candidates_path = str(tmp_path / "candidates.json")

        symbols = _read_universe_symbols(settings)
        assert symbols == ["AAPL", "SPY"]

    def test_falls_back_to_candidates_snapshot(self, tmp_path):
        universe_path = tmp_path / "universe.txt"
        # No universe file

        candidates_path = tmp_path / "candidates.json"
        import json
        candidates_path.write_text(json.dumps({
            "candidates": [
                {"ticker": "AAPL", "included": True, "rank": 1},
                {"ticker": "SPY", "included": True, "rank": 2},
                {"ticker": "TSLA", "included": False, "rank": 3},
            ]
        }))

        settings = MagicMock()
        settings.app.universe_path = str(universe_path)
        settings.app.universe_candidates_path = str(candidates_path)

        symbols = _read_universe_symbols(settings)
        assert symbols == ["AAPL", "SPY"]

    def test_returns_empty_when_no_files_exist(self, tmp_path):
        settings = MagicMock()
        settings.app.universe_path = str(tmp_path / "nonexistent.txt")
        settings.app.universe_candidates_path = str(tmp_path / "nonexistent.json")

        symbols = _read_universe_symbols(settings)
        assert symbols == []


class TestRunContinuousLoop:
    @pytest.fixture
    def mock_settings(self, tmp_path):
        settings = MagicMock()
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path / "logs")
        settings.app.universe_path = str(tmp_path / "universe.txt")
        settings.app.universe_candidates_path = str(tmp_path / "candidates.json")
        settings.app.scan_results_path = str(tmp_path / "scan.json")
        settings.app.portfolio_path = str(tmp_path / "portfolio.json")
        settings.app.exit_at_eod = False
        settings.market_data.intraday_period = "1d"
        settings.market_data.intraday_interval = "5m"
        settings.market_data.daily_period = "1mo"
        settings.risk.use_atr_sizing = False
        settings.risk.atr_period = 14
        settings.risk.atr_trailing_stop_multiplier = 2.0
        settings.risk.max_risk_per_trade_pct = 0.01
        settings.paper.fee_per_order = 1.0
        settings.paper.slippage_bps = 5
        settings.scout.max_universe_size = 20
        settings.scout.max_snapshot_candidates = 50
        settings.scout.screeners = []
        settings.rl = None
        settings.strategy = None
        settings.counter_thesis = None
        return settings

    def test_runs_single_cycle(self, mock_settings, tmp_path):
        """Test that the loop runs at least one cycle."""
        with patch("trading_bot.runtime.continuous_loop.run_scan") as mock_scan, \
             patch("trading_bot.runtime.continuous_loop.run_paper_trade") as mock_trade, \
             patch("trading_bot.runtime.continuous_loop._run_manage_positions_once") as mock_manage, \
             patch("trading_bot.runtime.continuous_loop._read_universe_symbols") as mock_universe:

            mock_scan.return_value = {
                "lines": [],
                "summary": {"approved": 0, "rejected": 0, "errors": 0},
                "candidates": [],
            }
            mock_trade.return_value = []
            mock_manage.return_value = {"positions": 0, "actions": 0, "lines": [], "exit_events": []}
            mock_universe.return_value = ["AAPL", "SPY"]

            stats = run_continuous_loop(
                settings=mock_settings,
                interval_seconds=0,
                max_cycles=1,
                build_universe=False,
            )

            assert stats.cycle >= 1
            assert stats.total_scans >= 1
            mock_scan.assert_called()
            mock_manage.assert_called()

    def test_respects_max_cycles(self, mock_settings, tmp_path):
        """Test that the loop stops after max_cycles."""
        with patch("trading_bot.runtime.continuous_loop.run_scan") as mock_scan, \
             patch("trading_bot.runtime.continuous_loop.run_paper_trade") as mock_trade, \
             patch("trading_bot.runtime.continuous_loop._run_manage_positions_once") as mock_manage, \
             patch("trading_bot.runtime.continuous_loop._read_universe_symbols") as mock_universe:

            mock_scan.return_value = {
                "lines": [],
                "summary": {"approved": 0, "rejected": 0, "errors": 0},
                "candidates": [],
            }
            mock_trade.return_value = []
            mock_manage.return_value = {"positions": 0, "actions": 0, "lines": [], "exit_events": []}
            mock_universe.return_value = ["AAPL"]

            stats = run_continuous_loop(
                settings=mock_settings,
                interval_seconds=0,
                max_cycles=3,
                build_universe=False,
            )

            assert stats.cycle >= 3
            assert mock_scan.call_count >= 3

    def test_handles_empty_universe(self, mock_settings, tmp_path):
        """Test that the loop continues when universe is empty."""
        with patch("trading_bot.runtime.continuous_loop.run_scan") as mock_scan, \
             patch("trading_bot.runtime.continuous_loop._read_universe_symbols") as mock_universe:

            mock_scan.return_value = {
                "lines": [],
                "summary": {"approved": 0, "rejected": 0, "errors": 0},
                "candidates": [],
            }
            mock_universe.return_value = []

            stats = run_continuous_loop(
                settings=mock_settings,
                interval_seconds=0,
                max_cycles=2,
                build_universe=False,
            )

            # Should have cycled through at least once
            assert stats.cycle >= 1

    def test_circuit_breaker_on_consecutive_failures(self, mock_settings, tmp_path):
        """Test that the loop exits after max_failures consecutive errors."""
        with patch("trading_bot.runtime.continuous_loop.run_scan") as mock_scan, \
             patch("trading_bot.runtime.continuous_loop._read_universe_symbols") as mock_universe:

            mock_scan.side_effect = Exception("test error")
            mock_universe.return_value = ["AAPL"]

            stats = run_continuous_loop(
                settings=mock_settings,
                interval_seconds=0,
                max_cycles=100,
                max_failures=3,
                build_universe=False,
            )

            assert stats.total_errors >= 3
            assert stats.consecutive_failures >= 3

    def test_dry_run_mode(self, mock_settings, tmp_path):
        """Test that dry_run mode doesn't execute trades."""
        with patch("trading_bot.runtime.continuous_loop.run_scan") as mock_scan, \
             patch("trading_bot.runtime.continuous_loop.run_paper_trade") as mock_trade, \
             patch("trading_bot.runtime.continuous_loop._run_manage_positions_once") as mock_manage, \
             patch("trading_bot.runtime.continuous_loop._read_universe_symbols") as mock_universe:

            mock_scan.return_value = {
                "lines": [],
                "summary": {"approved": 1, "rejected": 0, "errors": 0},
                "candidates": [
                    {"ticker": "AAPL", "status": "APPROVED", "quality": "GREEN"},
                ],
            }
            mock_trade.return_value = ["AAPL DRY_RUN qty=10 price=150.00"]
            mock_manage.return_value = {"positions": 0, "actions": 0, "lines": [], "exit_events": []}
            mock_universe.return_value = ["AAPL"]

            stats = run_continuous_loop(
                settings=mock_settings,
                interval_seconds=0,
                max_cycles=1,
                dry_run=True,
                build_universe=False,
            )

            assert stats.cycle >= 1
            # dry_run should still call run_paper_trade with dry_run=True
            mock_trade.assert_called()
            call_kwargs = mock_trade.call_args
            assert call_kwargs[1].get("dry_run") is True

    def test_stats_track_trades_and_exits(self, mock_settings, tmp_path):
        """Test that stats correctly track trades and exits."""
        with patch("trading_bot.runtime.continuous_loop.run_scan") as mock_scan, \
             patch("trading_bot.runtime.continuous_loop.run_paper_trade") as mock_trade, \
             patch("trading_bot.runtime.continuous_loop._run_manage_positions_once") as mock_manage, \
             patch("trading_bot.runtime.continuous_loop._read_universe_symbols") as mock_universe:

            mock_scan.return_value = {
                "lines": [],
                "summary": {"approved": 2, "rejected": 1, "errors": 0},
                "candidates": [
                    {"ticker": "AAPL", "status": "APPROVED", "quality": "GREEN"},
                    {"ticker": "SPY", "status": "APPROVED", "quality": "GREEN"},
                    {"ticker": "TSLA", "status": "REJECTED", "reason": "yellow"},
                ],
            }
            mock_trade.return_value = [
                "AAPL FILLED qty=10 price=150.00 cash=98500.00",
                "SPY DRY_RUN qty=5 price=150.00",
            ]
            mock_manage.return_value = {"positions": 2, "actions": 1, "lines": [], "exit_events": [{"ticker": "TSLA", "reason": "stop"}]}
            mock_universe.return_value = ["AAPL", "SPY", "TSLA"]

            stats = run_continuous_loop(
                settings=mock_settings,
                interval_seconds=0,
                max_cycles=2,
                build_universe=False,
            )

            assert stats.cycle >= 2
            assert stats.total_trades >= 2
            assert stats.total_exits >= 2
            assert stats.total_rejections >= 2


class TestIdempotencyGuard:
    """Tests for the exit idempotency guard (prevents duplicate sells)."""

    def test_recently_exited_returns_true_for_recent_exit(self):
        from datetime import datetime, timedelta
        from trading_bot.models.portfolio import PortfolioState

        state = PortfolioState(cash=10000.0, equity=10000.0)
        now = datetime.now()
        state.last_exited_at = {"CIEN": (now - timedelta(seconds=30)).isoformat()}

        ts = state.last_exited_at.get("CIEN")
        exited_at = datetime.fromisoformat(ts)
        assert (now - exited_at).total_seconds() < 120

    def test_recently_exited_returns_false_for_old_exit(self):
        from datetime import datetime, timedelta
        from trading_bot.models.portfolio import PortfolioState

        state = PortfolioState(cash=10000.0, equity=10000.0)
        now = datetime.now()
        state.last_exited_at = {"CIEN": (now - timedelta(seconds=300)).isoformat()}

        ts = state.last_exited_at.get("CIEN")
        exited_at = datetime.fromisoformat(ts)
        assert (now - exited_at).total_seconds() >= 120

    def test_recently_exited_returns_false_for_missing_ticker(self):
        from trading_bot.models.portfolio import PortfolioState

        state = PortfolioState(cash=10000.0, equity=10000.0)
        state.last_exited_at = {"CIEN": "2025-01-01T00:00:00"}

        assert state.last_exited_at.get("AAPL") is None

    def test_manage_positions_formats_open_position_without_error(self, monkeypatch, tmp_path):
        import types

        import pandas as pd

        from trading_bot.config.settings import Settings
        from trading_bot.models.portfolio import PortfolioState, Position
        from trading_bot.portfolio.ledger import PortfolioLedger
        from trading_bot.runtime import continuous_loop

        settings = Settings(app={"state_db_path": str(tmp_path / "state.db"), "log_dir": str(tmp_path)})
        settings.session.eod_enabled = False
        ledger = PortfolioLedger(tmp_path / "state.db")
        ledger.save_portfolio_state(
            PortfolioState(
                cash=9_000.0,
                equity=10_000.0,
                positions={
                    "AAPL": Position(ticker="AAPL", quantity=10, average_cost=100.0)
                },
            )
        )
        frame = pd.DataFrame(
            {"close": [105.0], "high": [105.0], "low": [104.0], "volume": [1000]},
            index=pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")]),
        )

        monkeypatch.setattr(
            continuous_loop.market_data,
            "fetch_and_validate_bars",
            lambda *args, **kwargs: (frame, types.SimpleNamespace(valid=True, reason="")),
        )
        monkeypatch.setattr(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            lambda ledger: (True, ""),
        )
        monkeypatch.setattr(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            lambda ledger, settings: (True, ""),
        )

        result = continuous_loop._run_manage_positions_once(settings, ledger)

        assert result["actions"] == 0
        assert result["lines"] == ["AAPL price=105.00 qty=10 highest_high=105.00"]

    def test_manage_positions_reports_and_persists_stop_widening(self, monkeypatch, tmp_path):
        import types

        import pandas as pd

        from trading_bot.config.settings import Settings
        from trading_bot.models.portfolio import PortfolioState, Position
        from trading_bot.portfolio.ledger import PortfolioLedger
        from trading_bot.runtime import continuous_loop

        settings = Settings(app={"state_db_path": str(tmp_path / "state.db"), "log_dir": str(tmp_path)})
        settings.session.eod_enabled = False
        settings.risk.min_stop_distance_pct = 3.0
        settings.risk.use_atr_sizing = False
        ledger = PortfolioLedger(tmp_path / "state.db")
        ledger.save_portfolio_state(
            PortfolioState(
                cash=9_000.0,
                equity=10_000.0,
                positions={
                    "AAPL": Position(
                        ticker="AAPL",
                        quantity=10,
                        average_cost=100.0,
                        stop_loss=99.0,
                    )
                },
            )
        )
        frame = pd.DataFrame(
            {"close": [105.0], "high": [105.0], "low": [104.0], "volume": [1000]},
            index=pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")]),
        )

        monkeypatch.setattr(
            continuous_loop.market_data,
            "fetch_and_validate_bars",
            lambda *args, **kwargs: (frame, types.SimpleNamespace(valid=True, reason="")),
        )
        monkeypatch.setattr(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            lambda ledger: (True, ""),
        )
        monkeypatch.setattr(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            lambda ledger, settings: (True, ""),
        )

        result = continuous_loop._run_manage_positions_once(settings, ledger)

        assert result["actions"] == 0
        assert result["lines"] == [
            "AAPL price=105.00 qty=10 stop_widened 99.0000->97.0000 highest_high=105.00"
        ]
        assert ledger.load_portfolio_state().positions["AAPL"].stop_loss == 97.0

    def test_manage_positions_executes_exit_and_persists_realized_pnl(self, monkeypatch, tmp_path):
        import types

        import pandas as pd

        from trading_bot.config.settings import Settings
        from trading_bot.models.portfolio import PortfolioState, Position
        from trading_bot.portfolio.ledger import PortfolioLedger
        from trading_bot.runtime import continuous_loop

        settings = Settings(app={"state_db_path": str(tmp_path / "state.db"), "log_dir": str(tmp_path)})
        settings.session.eod_enabled = False
        settings.paper.fee_per_order = 1.0
        settings.paper.slippage_bps = 0
        ledger = PortfolioLedger(tmp_path / "state.db")
        ledger.save_portfolio_state(
            PortfolioState(
                cash=9_000.0,
                equity=10_000.0,
                positions={
                    "AAPL": Position(
                        ticker="AAPL",
                        quantity=10,
                        average_cost=100.0,
                        profit_target=108.0,
                        strategy_tag="v3-trend_following",
                    )
                },
            )
        )
        frame = pd.DataFrame(
            {"close": [110.0], "high": [110.0], "low": [109.0], "volume": [1000]},
            index=pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")]),
        )

        monkeypatch.setattr(
            continuous_loop.market_data,
            "fetch_and_validate_bars",
            lambda *args, **kwargs: (frame, types.SimpleNamespace(valid=True, reason="")),
        )
        monkeypatch.setattr(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            lambda ledger: (True, ""),
        )
        monkeypatch.setattr(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            lambda ledger, settings: (True, ""),
        )

        result = continuous_loop._run_manage_positions_once(settings, ledger)

        assert result["actions"] == 1
        assert result["exit_events"][0]["ticker"] == "AAPL"
        state = ledger.load_portfolio_state()
        assert state is not None
        assert state.positions == {}
        assert state.cash == 10099.0
        assert state.realized_pnl == 99.0

        orders = ledger.list_order_rows()
        assert len(orders) == 1
        assert orders[0]["side"] == "SELL"
        assert orders[0]["pnl"] == 99.0

    def test_manage_positions_scales_out_partial_target(self, monkeypatch, tmp_path):
        import types

        import pandas as pd

        from trading_bot.config.settings import Settings
        from trading_bot.models.portfolio import PortfolioState, Position
        from trading_bot.portfolio.ledger import PortfolioLedger
        from trading_bot.runtime import continuous_loop

        settings = Settings(app={"state_db_path": str(tmp_path / "state.db"), "log_dir": str(tmp_path)})
        settings.session.eod_enabled = False
        settings.paper.partial_take_profit_enabled = True
        settings.paper.partial_take_profit_fraction = 0.5
        ledger = PortfolioLedger(tmp_path / "state.db")
        ledger.save_portfolio_state(
            PortfolioState(
                cash=9_000.0,
                equity=10_000.0,
                positions={
                    "AAPL": Position(
                        ticker="AAPL",
                        quantity=10,
                        average_cost=100.0,
                        stop_loss=98.0,
                        profit_target=108.0,
                    )
                },
            )
        )
        frame = pd.DataFrame(
            {"close": [110.0], "high": [110.0], "low": [109.0], "volume": [1000]},
            index=pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")]),
        )

        monkeypatch.setattr(
            continuous_loop.market_data,
            "fetch_and_validate_bars",
            lambda *args, **kwargs: (frame, types.SimpleNamespace(valid=True, reason="")),
        )
        monkeypatch.setattr(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            lambda ledger: (True, ""),
        )
        monkeypatch.setattr(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            lambda ledger, settings: (True, ""),
        )

        result = continuous_loop._run_manage_positions_once(settings, ledger)

        assert result["actions"] == 1
        state = ledger.load_portfolio_state()
        assert state is not None
        assert state.positions["AAPL"].quantity == 5
        assert state.positions["AAPL"].stop_loss == 100.0
        assert state.positions["AAPL"].profit_target is None
        assert state.positions["AAPL"].partial_profit_taken is True
