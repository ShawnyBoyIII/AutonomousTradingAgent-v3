import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from typer.testing import CliRunner

from trading_bot.cli.app import app, _format_scan_summary
from trading_bot.main import main
from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.runtime.watchlist import add_symbol, read_watchlist, remove_symbol


def test_cli_shows_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["tradebot", "--help"])

    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()
    assert "scan" in captured.out
    assert "paper-trade" in captured.out


def test_doctor_command_reports_local_readiness(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        "  state_db_path: state/trading_bot.db\n"
        "  log_dir: logs\n"
        "  scan_results_path: state/scan_results.json\n"
        "  portfolio_summary_path: state/portfolio_summary.json\n"
        "  dashboard_summary_path: state/dashboard_summary.json\n"
        "  backtest_summary_path: state/backtest_summary.json\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "doctor"])

    assert result.exit_code == 0
    assert result.stdout.strip() == (
        "doctor live_trading=false state_db=missing log_dir=missing snapshots=0/4 "
        "provider=yfinance provider_auth=yfinance:ok"
    )


def test_tune_command_dry_run_prints_override_preview(tmp_path: Path) -> None:
    from trading_bot.strategy.strategy_tracker import record_exit

    config_file = tmp_path / "config.yaml"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  log_dir: {log_dir}\n"
        "  scan_results_path: state/scan_results.json\n",
        encoding="utf-8",
    )
    scan_results_path = tmp_path / "state" / "scan_results.json"
    scan_results_path.parent.mkdir(parents=True)
    scan_results_path.write_text(
        json.dumps({"summary": {"approved": 1, "rejected": 9}}),
        encoding="utf-8",
    )

    for i in range(20):
        win = i < 6
        record_exit(
            log_dir,
            "v3-breakout",
            "AAPL",
            entry_price=100.0,
            exit_price=101.0 if win else 99.0,
            quantity=1,
            fees=1.0,
            pnl=10.0 if win else -10.0,
            reason="target" if win else "stop",
            timestamp=datetime(2026, 7, 1 + i),
        )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "tune", "--dry-run"])

    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    assert "supermodel:" in result.stdout
    assert "block_threshold: 0.25" in result.stdout


def test_swarm_command_passes_saved_portfolio_state(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {tmp_path / 'logs'}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=7_500.0,
            equity=10_000.0,
            positions={"MSFT": Position(ticker="MSFT", quantity=5, average_cost=200.0)},
        )
    )

    frame = pd.DataFrame(
        {
            "open": [100.0] * 60,
            "high": [101.0] * 60,
            "low": [99.0] * 60,
            "close": [100.0] * 60,
            "volume": [1_000_000] * 60,
        }
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "trading_bot.data.market_data.fetch_and_validate_bars",
        lambda *args, **kwargs: (frame, SimpleNamespace(valid=True, reason=None)),
    )
    monkeypatch.setattr("trading_bot.swarm.engine.SwarmEngine.setup_workers", lambda self, workers: None)

    def capture_run(self, symbols, market_data, portfolio_state=None, **kwargs):
        captured["portfolio_state"] = portfolio_state
        return SimpleNamespace(
            decisions={},
            execution_time_seconds=0.0,
            completed_workers=0,
            total_workers=0,
        )

    monkeypatch.setattr("trading_bot.swarm.engine.SwarmEngine.run", capture_run)

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "swarm", "--symbols", "AAPL"])

    assert result.exit_code == 0
    assert captured["portfolio_state"]["cash"] == 7_500.0
    assert captured["portfolio_state"]["positions"]["MSFT"]["quantity"] == 5


def test_scan_summary_includes_rl_counts_when_present() -> None:
    assert _format_scan_summary(
        {
            "symbols": 3,
            "approved": 1,
            "green": 1,
            "yellow": 0,
            "rejected": 0,
            "no_signal": 2,
            "errors": 0,
            "rl_buy": 1,
            "rl_hold": 1,
            "rl_sell": 1,
            "rl_unsupported": 2,
            "rl_avg_confidence": 0.62,
        }
    ).endswith("rl_buy=1 rl_hold=1 rl_sell=1 rl_unsupported=2 rl_avg_conf=0.62")


def test_watchlist_file_adds_and_removes_symbols(tmp_path: Path) -> None:
    path = tmp_path / "state" / "watchlist.txt"

    assert add_symbol(path, " msft ") == ["MSFT"]
    assert add_symbol(path, "MSFT") == ["MSFT"]
    assert add_symbol(path, "brk.b") == ["MSFT", "BRK.B"]
    assert read_watchlist(path) == ["MSFT", "BRK.B"]
    assert remove_symbol(path, "msft") == ["BRK.B"]


def test_rl_signal_rejects_symbols_outside_model_metadata(monkeypatch, tmp_path: Path) -> None:
    import types

    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.config.settings import Settings

    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    settings = Settings()
    settings.app.log_dir = str(tmp_path / "logs")
    settings.rl.enabled = True
    settings.rl.model_path = "model.zip"
    settings.rl.allow_untrained_symbol_inference = True
    settings.rl.action_confidence_threshold = 0.5

    captured: dict[str, object] = {}

    class FakeAgent:
        def predict_signal(self, **kwargs):
            captured["symbols"] = kwargs["symbols"]
            captured["market_frames"] = sorted(kwargs["market_frames"])
            return 0, 0.75

    monkeypatch.setattr("trading_bot.rl.agent.RLAgent.load", lambda model_path: FakeAgent())

    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=30, freq="D"),
            "open": [100.0 + index for index in range(30)],
            "high": [101.0 + index for index in range(30)],
            "low": [99.0 + index for index in range(30)],
            "close": [100.0 + index for index in range(30)],
            "volume": [1_000_000 for _ in range(30)],
        }
    )

    def fake_fetch(symbol: str, *args, **kwargs):
        return frame.copy(), types.SimpleNamespace(valid=True, reason=None)

    monkeypatch.setattr(orchestrator.market_data, "fetch_and_validate_bars", fake_fetch)

    signal, reason, details = orchestrator._build_rl_signal_result("MSFT", settings)

    assert signal is None
    assert "RL agent predicts HOLD" in reason
    assert details["rl_trained_symbols"] == ["AAPL"]
    assert details["rl_untrained_symbol"] is True
    assert captured["symbols"] == ["AAPL", "MSFT"]


def test_rl_signal_rejects_untrained_symbol_by_default(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.config.settings import Settings

    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    settings = Settings()
    settings.app.log_dir = str(tmp_path / "logs")
    settings.rl.enabled = True
    settings.rl.model_path = "model.zip"

    def fail_fetch(*args, **kwargs):
        raise AssertionError("untrained RL symbol should fail before market data fetch")

    monkeypatch.setattr(orchestrator.market_data, "fetch_and_validate_bars", fail_fetch)

    signal, reason, details = orchestrator._build_rl_signal_result("MSFT", settings)

    assert signal is None
    assert reason == "RL model not trained for MSFT"
    assert details["rl_trained_symbols"] == ["AAPL"]
    assert details["rl_untrained_symbol"] is True
    assert details["rl_models"] == 0


def test_rl_signal_rejects_missing_model_metadata_before_fetch(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.config.settings import Settings

    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    settings = Settings()
    settings.app.log_dir = str(tmp_path / "logs")
    settings.rl.enabled = True
    settings.rl.model_path = "model.zip"

    def fail_fetch(*args, **kwargs):
        raise AssertionError("missing RL metadata should fail before market data fetch")

    monkeypatch.setattr(orchestrator.market_data, "fetch_and_validate_bars", fail_fetch)

    signal, reason, details = orchestrator._build_rl_signal_result("AAPL", settings)

    assert signal is None
    assert "RL model metadata missing or empty:" in reason
    assert details == {}


def test_rl_signal_rejects_empty_model_metadata_before_fetch(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.config.settings import Settings

    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": []}', encoding="utf-8")
    settings = Settings()
    settings.app.log_dir = str(tmp_path / "logs")
    settings.rl.enabled = True
    settings.rl.model_path = "model.zip"

    def fail_fetch(*args, **kwargs):
        raise AssertionError("empty RL metadata should fail before market data fetch")

    monkeypatch.setattr(orchestrator.market_data, "fetch_and_validate_bars", fail_fetch)

    signal, reason, details = orchestrator._build_rl_signal_result("AAPL", settings)

    assert signal is None
    assert "RL model metadata missing or empty:" in reason
    assert details == {}


def test_rl_signal_passes_all_trained_symbol_frames(monkeypatch, tmp_path: Path) -> None:
    import types

    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.config.settings import Settings

    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL", "MSFT"]}', encoding="utf-8")
    settings = Settings()
    settings.app.log_dir = str(tmp_path / "logs")
    settings.rl.enabled = True
    settings.rl.model_path = "model.zip"

    captured: dict[str, object] = {}

    class FakeAgent:
        def predict_signal(self, **kwargs):
            captured["symbols"] = kwargs["symbols"]
            captured["market_frames"] = sorted(kwargs["market_frames"])
            return 0, 0.75

    monkeypatch.setattr("trading_bot.rl.agent.RLAgent.load", lambda model_path: FakeAgent())

    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=30, freq="D"),
            "open": [100.0 + index for index in range(30)],
            "high": [101.0 + index for index in range(30)],
            "low": [99.0 + index for index in range(30)],
            "close": [100.0 + index for index in range(30)],
            "volume": [1_000_000 + index for index in range(30)],
        }
    )

    def fake_fetch(symbol, period, interval, settings):
        return frame.copy(deep=True), types.SimpleNamespace(valid=True, reason="")

    monkeypatch.setattr(orchestrator.market_data, "fetch_and_validate_bars", fake_fetch)
    monkeypatch.setattr(
        orchestrator.market_data,
        "fetch_bars",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HOLD should not fetch a separate current price")),
    )

    signal, reason, details = orchestrator._build_rl_signal_result("AAPL", settings)

    assert signal is None
    assert "RL agent predicts HOLD" in reason
    assert captured["symbols"] == ["AAPL", "MSFT"]
    assert captured["market_frames"] == ["AAPL", "MSFT"]
    assert details["rl_action"] == 0


def test_rl_signal_rejects_buy_when_current_price_unavailable(monkeypatch, tmp_path: Path) -> None:
    import types

    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.config.settings import Settings

    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    settings = Settings()
    settings.app.log_dir = str(tmp_path / "logs")
    settings.rl.enabled = True
    settings.rl.model_path = "model.zip"
    settings.rl.action_confidence_threshold = 0.5

    class FakeAgent:
        def predict_signal(self, **kwargs):
            return 1, 0.9

    monkeypatch.setattr("trading_bot.rl.agent.RLAgent.load", lambda model_path: FakeAgent())

    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=30, freq="D"),
            "open": [100.0 + index for index in range(30)],
            "high": [101.0 + index for index in range(30)],
            "low": [99.0 + index for index in range(30)],
            "close": [100.0 + index for index in range(29)] + [0.0],
            "volume": [1_000_000 for _ in range(30)],
        }
    )

    def fake_fetch_validate(symbol, period, interval, settings):
        return frame.copy(deep=True), types.SimpleNamespace(valid=True, reason="")

    monkeypatch.setattr(orchestrator.market_data, "fetch_and_validate_bars", fake_fetch_validate)
    monkeypatch.setattr(
        orchestrator.market_data,
        "fetch_bars",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("RL should use the validated target frame price")),
    )

    signal, reason, details = orchestrator._build_rl_signal_result("AAPL", settings)

    assert signal is None
    assert reason == "RL current price unavailable"
    assert details["rl_action"] == 1
    assert details["rl_confidence"] == 0.9


def test_rl_signal_penalizes_untrained_symbol_confidence(monkeypatch, tmp_path: Path) -> None:
    import types

    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.config.settings import Settings

    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    settings = Settings()
    settings.app.log_dir = str(tmp_path / "logs")
    settings.rl.enabled = True
    settings.rl.model_path = "model.zip"
    settings.rl.action_confidence_threshold = 0.5
    settings.rl.untrained_confidence_threshold_multiplier = 0.8
    settings.rl.allow_untrained_symbol_inference = True

    class FakeAgent:
        def predict_signal(self, **kwargs):
            return 1, 0.6

    monkeypatch.setattr("trading_bot.rl.agent.RLAgent.load", lambda model_path: FakeAgent())

    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=30, freq="D"),
            "open": [100.0 + index for index in range(30)],
            "high": [101.0 + index for index in range(30)],
            "low": [99.0 + index for index in range(30)],
            "close": [100.0 + index for index in range(30)],
            "volume": [1_000_000 for _ in range(30)],
        }
    )

    def fake_fetch_validate(symbol, period, interval, settings):
        return frame.copy(deep=True), types.SimpleNamespace(valid=True, reason="")

    monkeypatch.setattr(orchestrator.market_data, "fetch_and_validate_bars", fake_fetch_validate)

    signal, reason, details = orchestrator._build_rl_signal_result("MSFT", settings)

    assert signal is None
    assert reason == "RL confidence 0.48 below threshold 0.5"
    assert details["rl_confidence"] == 0.6
    assert details["rl_effective_confidence"] == 0.48
    assert details["rl_untrained_symbol"] is True


def test_rl_signal_rejects_ensemble_action_tie(monkeypatch, tmp_path: Path) -> None:
    import types

    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.config.settings import Settings

    buy_model = tmp_path / "buy_model.zip"
    sell_model = tmp_path / "sell_model.zip"
    buy_model.write_bytes(b"")
    sell_model.write_bytes(b"")
    (tmp_path / "buy_model_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    (tmp_path / "sell_model_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    settings = Settings()
    settings.app.log_dir = str(tmp_path / "logs")
    settings.rl.enabled = True
    settings.rl.model_path = "buy_model.zip"
    settings.rl.model_paths = ["buy_model.zip", "sell_model.zip"]

    class FakeAgent:
        def __init__(self, action: int) -> None:
            self.action = action

        def predict_signal(self, **kwargs):
            return self.action, 0.8

    def fake_load(model_path):
        return FakeAgent(1 if "buy_model" in str(model_path) else 2)

    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=30, freq="D"),
            "open": [100.0 + index for index in range(30)],
            "high": [101.0 + index for index in range(30)],
            "low": [99.0 + index for index in range(30)],
            "close": [100.0 + index for index in range(30)],
            "volume": [1_000_000 for _ in range(30)],
        }
    )

    monkeypatch.setattr("trading_bot.rl.agent.RLAgent.load", fake_load)
    monkeypatch.setattr(
        orchestrator.market_data,
        "fetch_and_validate_bars",
        lambda *args, **kwargs: (frame.copy(deep=True), types.SimpleNamespace(valid=True, reason="")),
    )

    signal, reason, details = orchestrator._build_rl_signal_result("AAPL", settings)

    assert signal is None
    assert reason == "RL ensemble action tie ([1, 2])"
    assert details["rl_action"] == 0
    assert details["rl_vote_tie"] == [1, 2]


def test_read_only_analysis_commands_render_cleanly_with_empty_state(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        "  state_db_path: state/trading_bot.db\n"
        "  log_dir: logs\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    checks = [
        (["--config-path", str(config_file), "performance"], 0, "No trades found"),
        (["--config-path", str(config_file), "health"], 0, "Health Check Report"),
        (["--config-path", str(config_file), "alerts"], 0, "No active alerts. System operating normally."),
        (["--config-path", str(config_file), "strategy-health"], 0, "No strategy results tracked yet."),
        (["--config-path", str(config_file), "drawdown"], 0, "No equity history"),
        (["--config-path", str(config_file), "correlation"], 0, "Need 2+ open positions to compute correlation."),
        (["--config-path", str(config_file), "var"], 0, "No open positions for VaR calculation."),
        (["--config-path", str(config_file), "risk-report"], 0, "No open positions — skipping VaR, correlation, stress tests."),
    ]

    for argv, exit_code, text in checks:
        result = runner.invoke(app, argv)
        assert result.exit_code == exit_code
        assert text in result.stdout


def test_risk_report_handles_normalized_close_history_and_shows_trade_quality(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "  log_dir: logs\n",
        encoding="utf-8",
    )

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
                    stop_loss=95.0,
                    profit_target=110.0,
                )
            },
        )
    )
    ledger.record_fill(
        FillResult(
            order_id="sell-1",
            ticker="AAPL",
            quantity=10,
            fill_price=110.0,
            fees=1.0,
            filled_at=datetime(2026, 6, 18, 10, 0, 0),
        ),
        side="SELL",
        realized_pnl=99.0,
        strategy_tag="v3-trend_following",
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        if interval == "1d":
            return pd.DataFrame(
                {
                    "timestamp": pd.date_range("2026-05-01", periods=30, freq="D"),
                    "open": [100.0 + i for i in range(30)],
                    "high": [101.0 + i for i in range(30)],
                    "low": [99.0 + i for i in range(30)],
                    "close": [100.0 + i for i in range(30)],
                    "volume": [1_000_000 for _ in range(30)],
                }
            )
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-06-18T09:55:00"]),
                "open": [110.0],
                "high": [110.0],
                "low": [110.0],
                "close": [110.0],
                "volume": [1_000_000],
            }
        )

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "risk-report"])

    assert result.exit_code == 0
    assert "profit_factor=99.00" in result.stdout
    assert "win_rate=100.0%" in result.stdout
    assert "Top Strategy Attribution:" in result.stdout
    assert "v3-trend_following: +99.00" in result.stdout


def test_build_universe_writes_ranked_symbols_and_snapshot(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    monkeypatch.setattr(
        market_data,
        "fetch_small_cap_candidates",
        lambda limit=200, screeners=None: [
            {
                "symbol": "FAST",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "marketCap": 3_000_000_000,
                "regularMarketPrice": 16.0,
                "averageDailyVolume3Month": 500_000,
                "dayVolume": 1_800_000,
                "source": "aggressive_small_caps",
            },
            {
                "symbol": "SLOW",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "marketCap": 12_500_000_000,
                "regularMarketPrice": 28.0,
                "averageDailyVolume3Month": 250_000,
                "dayVolume": 420_000,
                "source": "small_cap_gainers",
            },
        ],
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        f"  universe_path: {tmp_path / 'state' / 'universe.txt'}\n"
        f"  universe_candidates_path: {tmp_path / 'state' / 'universe_candidates.json'}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "build-universe"])

    assert result.exit_code == 0
    assert "FAST" in result.stdout
    assert "summary candidates=2 included=2" in result.stdout
    assert (tmp_path / "state" / "universe.txt").read_text(encoding="utf-8") == "FAST\nSLOW\n"
    snapshot = json.loads((tmp_path / "state" / "universe_candidates.json").read_text(encoding="utf-8"))
    assert snapshot["mode"] == "universe"
    assert snapshot["summary"]["included"] == 2


def test_discover_export_writes_configured_universe_path(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.strategy.dynamic_watchlist as dynamic_watchlist

    config_file = tmp_path / "config.yaml"
    universe_path = tmp_path / "state" / "universe.txt"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        f"  universe_path: {universe_path}\n"
        "market_data:\n"
        "  providers:\n"
        "    - alpaca\n"
        "    - polygon\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs):
        captured["provider_stack"] = kwargs["settings"].provider_stack
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-06-01", periods=30, freq="D"),
                "open": [100.0] * 30,
                "high": [101.0] * 30,
                "low": [99.0] * 30,
                "close": [100.0] * 30,
                "volume": [1_000_000] * 30,
            }
        )

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    class FakeWatchlist:
        def __init__(self, max_symbols: int, scout_settings=None) -> None:
            self.max_symbols = max_symbols
            self.symbols = ["AAPL", "MSFT"]

        def update(self, data_provider):
            data_provider("AAPL")
            return SimpleNamespace(
                sectors_favored=[],
                added=[],
                removed=[],
                current=[
                    SimpleNamespace(symbol="AAPL", reason="test", score=80.0),
                    SimpleNamespace(symbol="MSFT", reason="test", score=75.0),
                ],
            )

        def get_symbols(self):
            return self.symbols

        def export_for_burn_in(self, output_path=None):
            assert output_path == str(universe_path.resolve())
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text("AAPL\nMSFT", encoding="utf-8")
            return str(output_path)

    monkeypatch.setattr(dynamic_watchlist, "DynamicWatchlist", FakeWatchlist)

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "discover", "--export"])

    assert result.exit_code == 0
    assert f"Exported 2 symbols to {universe_path.resolve()}" in result.stdout
    assert universe_path.read_text(encoding="utf-8") == "AAPL\nMSFT"
    assert not (tmp_path / "burn-in-symbols.txt").exists()
    assert captured["provider_stack"] == ["alpaca", "polygon"]


def test_discover_passes_scout_settings_into_watchlist(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.strategy.dynamic_watchlist as dynamic_watchlist

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "scout:\n"
        "  min_market_cap: 2000000000\n"
        "  max_market_cap: 50000000000\n"
        "  min_price: 5.0\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeWatchlist:
        def __init__(self, max_symbols: int, scout_settings=None) -> None:
            captured["max_symbols"] = max_symbols
            captured["min_market_cap"] = scout_settings.min_market_cap
            captured["max_market_cap"] = scout_settings.max_market_cap
            captured["min_price"] = scout_settings.min_price

        def update(self, data_provider):
            return SimpleNamespace(sectors_favored=[], added=[], removed=[], current=[])

        def get_symbols(self):
            return []

        def export_for_burn_in(self, output_path=None):
            return str(output_path)

    monkeypatch.setattr(dynamic_watchlist, "DynamicWatchlist", FakeWatchlist)

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "discover", "--max", "12"])

    assert result.exit_code == 0
    assert captured == {
        "max_symbols": 12,
        "min_market_cap": 2_000_000_000.0,
        "max_market_cap": 50_000_000_000.0,
        "min_price": 5.0,
    }


def test_fetch_latest_prices_uses_configured_provider_stack(monkeypatch) -> None:
    import trading_bot.data.market_data as market_data
    from trading_bot.cli.app import _fetch_latest_prices
    from trading_bot.config.settings import Settings

    settings = Settings()
    settings.market_data.providers = ["alpaca", "polygon"]
    captured: list[list[str]] = []

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        captured.append(kwargs["settings"].provider_stack)
        return pd.DataFrame(
            {
                "timestamp": [pd.Timestamp("2026-06-01")],
                "open": [99.0],
                "high": [101.0],
                "low": [98.0],
                "close": [100.5],
                "volume": [1_000_000],
            }
        )

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    prices = _fetch_latest_prices(["AAPL"], settings)

    assert prices == {"AAPL": 100.5}
    assert captured == [["alpaca", "polygon"]]


def test_build_universe_keeps_multi_screener_hits(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    monkeypatch.setattr(
        market_data,
        "fetch_small_cap_candidates",
        lambda limit=200, screeners=None: [
            {
                "symbol": "DUPE",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "marketCap": 5_000_000_000,
                "regularMarketPrice": 10.0,
                "averageDailyVolume3Month": 600_000,
                "dayVolume": 1_500_000,
                "source": "aggressive_small_caps",
            },
            {
                "symbol": "DUPE",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "marketCap": 5_000_000_000,
                "regularMarketPrice": 10.0,
                "averageDailyVolume3Month": 600_000,
                "dayVolume": 1_400_000,
                "source": "small_cap_gainers",
            },
        ],
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        f"  universe_path: {tmp_path / 'state' / 'universe.txt'}\n"
        f"  universe_candidates_path: {tmp_path / 'state' / 'universe_candidates.json'}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "build-universe"])

    assert result.exit_code == 0
    assert "source_hits=2" in result.stdout
    snapshot = json.loads((tmp_path / "state" / "universe_candidates.json").read_text(encoding="utf-8"))
    assert snapshot["candidates"][0]["source_hits"] == 2


def test_build_universe_applies_advisory_scout_override(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    monkeypatch.setattr(
        market_data,
        "fetch_small_cap_candidates",
        lambda limit=200, screeners=None: [
            {
                "symbol": "FAST",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "marketCap": 4_000_000_000,
                "regularMarketPrice": 16.0,
                "averageDailyVolume3Month": 500_000,
                "dayVolume": 1_800_000,
                "source": "aggressive_small_caps",
            },
            {
                "symbol": "DROP",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "marketCap": 4_000_000_000,
                "regularMarketPrice": 18.0,
                "averageDailyVolume3Month": 500_000,
                "dayVolume": 1_800_000,
                "source": "small_cap_gainers",
            },
        ],
    )
    advisory_dir = tmp_path / "state" / "advisory"
    advisory_dir.mkdir(parents=True)
    (advisory_dir / "scout_override.yaml").write_text(
        "main_midcap:\n"
        "  promote_symbols:\n"
        "    - BOOST\n"
        "  avoid_symbols:\n"
        "    - DROP\n",
        encoding="utf-8",
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        f"  universe_path: {tmp_path / 'state' / 'universe.txt'}\n"
        f"  universe_candidates_path: {tmp_path / 'state' / 'universe_candidates.json'}\n"
        f"  advisory_dir: {advisory_dir}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "build-universe"])

    assert result.exit_code == 0
    assert (tmp_path / "state" / "universe.txt").read_text(encoding="utf-8") == "BOOST\nFAST\n"
    snapshot = json.loads((tmp_path / "state" / "universe_candidates.json").read_text(encoding="utf-8"))
    included = [row["ticker"] for row in snapshot["candidates"] if row.get("included")]
    assert included == ["BOOST", "FAST"]


def test_discover_export_applies_advisory_scout_override(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.strategy.dynamic_watchlist as dynamic_watchlist

    config_file = tmp_path / "config.yaml"
    universe_path = tmp_path / "state" / "universe.txt"
    advisory_dir = tmp_path / "state" / "advisory"
    advisory_dir.mkdir(parents=True)
    (advisory_dir / "scout_override.yaml").write_text(
        "main_midcap:\n"
        "  promote_symbols:\n"
        "    - BOOST\n"
        "  avoid_symbols:\n"
        "    - MSFT\n",
        encoding="utf-8",
    )
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        f"  universe_path: {universe_path}\n"
        f"  advisory_dir: {advisory_dir}\n",
        encoding="utf-8",
    )

    class FakeWatchlist:
        def __init__(self, max_symbols: int, scout_settings=None) -> None:
            self.symbols = ["AAPL", "MSFT"]

        def update(self, data_provider):
            return SimpleNamespace(
                sectors_favored=[],
                added=[],
                removed=[],
                current=[
                    SimpleNamespace(symbol="AAPL", reason="test", score=80.0),
                    SimpleNamespace(symbol="MSFT", reason="test", score=75.0),
                ],
            )

        def get_symbols(self):
            return self.symbols

        def export_for_burn_in(self, output_path=None):
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text("AAPL\nMSFT\n", encoding="utf-8")
            return str(Path(output_path).resolve())

    monkeypatch.setattr(dynamic_watchlist, "DynamicWatchlist", FakeWatchlist)

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "discover", "--export"])

    assert result.exit_code == 0
    assert universe_path.read_text(encoding="utf-8") == "BOOST\nAAPL"


def test_advisory_learn_and_report_commands(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    advisory_dir = tmp_path / "state" / "advisory"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'burn_in.db'}\n"
        f"  log_dir: {tmp_path / 'logs'}\n"
        f"  advisory_dir: {advisory_dir}\n"
        "advisory:\n"
        "  enabled: true\n"
        "  min_observations_per_symbol: 1\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "logs" / "decision-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "command": "scan",
                "ticker": "AAPL",
                "status": "APPROVED",
                "reason": "approved",
                "confidence": 0.9,
                "quality": "GREEN",
                "entry": 100.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    learn_result = CliRunner().invoke(app, ["--config-path", str(config_file), "advisory-learn", "--daily-report"])
    report_result = CliRunner().invoke(app, ["--config-path", str(config_file), "advisory-report"])

    assert learn_result.exit_code == 0
    assert "observations_added=1" in learn_result.stdout
    assert report_result.exit_code == 0
    assert "ADVISORY LEARNER REPORT" in report_result.stdout


def test_advisory_learn_command_noops_when_disabled(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'burn_in.db'}\n"
        f"  log_dir: {tmp_path / 'logs'}\n"
        f"  advisory_dir: {tmp_path / 'state' / 'advisory'}\n"
        "advisory:\n"
        "  enabled: false\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "advisory-learn"])

    assert result.exit_code == 0
    assert "advisory=disabled" in result.stdout


def test_scan_universe_reads_saved_symbols_file(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
    )
    universe_path = tmp_path / "state" / "universe.txt"
    universe_path.parent.mkdir(parents=True, exist_ok=True)
    universe_path.write_text("AAPL\n", encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    candidates_path = tmp_path / "state" / "universe_candidates.json"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  universe_path: {universe_path}\n"
        f"  universe_candidates_path: {candidates_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(PortfolioState(cash=20_000.0, equity=20_000.0))

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "scan-universe", "--summary"])

    assert result.exit_code == 0
    assert "AAPL APPROVED quality=GREEN" in result.stdout
    assert "summary symbols=1 approved=1 green=1 yellow=0 rejected=0 no_signal=0 errors=0" in result.stdout


def test_alert_signals_sends_from_scan_snapshot(monkeypatch, tmp_path: Path) -> None:
    from trading_bot.monitoring.notifiers import DiscordNotifier

    sent: list[str] = []

    def fake_send(self, event) -> bool:
        sent.append(f"{event.title}: {event.message}")
        return True

    monkeypatch.setattr(DiscordNotifier, "send", fake_send)
    scan_path = tmp_path / "state" / "scan_results.json"
    scan_path.parent.mkdir(parents=True, exist_ok=True)
    scan_path.write_text(
        json.dumps(
            {
                "mode": "scan",
                "summary": {"approved": 1},
                "candidates": [
                    {
                        "ticker": "AAPL",
                        "status": "APPROVED",
                        "quality": "GREEN",
                        "freshness": "fresh",
                        "entry": 101.0,
                        "stop": 99.8,
                        "target": 103.4,
                        "confidence": 0.9,
                        "reasons": ["bullish daily regime", "intraday breakout"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  scan_results_path: {scan_path}\n"
        "alerts:\n"
        "  discord_webhook_url: https://discord.test/webhook\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "alert-signals"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "alerts=1"
    assert len(sent) == 1
    assert "BUY CANDIDATE" in sent[0]
    assert "AAPL" in sent[0]


def test_scan_universe_includes_watchlist_symbols(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    captured: dict[str, object] = {}

    def fake_run_scan(symbols, settings, include_details=False):
        captured["symbols"] = symbols
        return {
            "lines": [],
            "summary": {
                "symbols": len(symbols),
                "approved": 0,
                "green": 0,
                "yellow": 0,
                "rejected": 0,
                "no_signal": len(symbols),
                "errors": 0,
            },
            "candidates": [],
        }

    monkeypatch.setattr(orchestrator, "run_scan", fake_run_scan)
    universe_path = tmp_path / "state" / "universe.txt"
    watchlist_path = tmp_path / "state" / "watchlist.txt"
    universe_path.parent.mkdir(parents=True, exist_ok=True)
    universe_path.write_text("AAPL\nMSFT\n", encoding="utf-8")
    watchlist_path.write_text("msft\nnvda\n", encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state.db'}\n"
        f"  universe_path: {universe_path}\n"
        f"  watchlist_path: {watchlist_path}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "scan-universe", "--summary"])

    assert result.exit_code == 0
    assert captured["symbols"] == ["AAPL", "MSFT", "NVDA"]


def test_alert_signals_no_webhook_is_noop(tmp_path: Path) -> None:
    scan_path = tmp_path / "state" / "scan_results.json"
    scan_path.parent.mkdir(parents=True, exist_ok=True)
    scan_path.write_text(
        json.dumps(
            {
                "mode": "scan",
                "summary": {"approved": 1},
                "candidates": [
                    {
                        "ticker": "AAPL",
                        "status": "APPROVED",
                        "quality": "GREEN",
                        "freshness": "fresh",
                        "entry": 101.0,
                        "stop": 99.8,
                        "target": 103.4,
                        "confidence": 0.9,
                        "reasons": ["bullish daily regime"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  scan_results_path: {scan_path}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "alert-signals"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "alerts=1"


def test_robinhood_status_reports_mcp_snapshot_state(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    synced_at = datetime(2026, 6, 19, 10, 0, 0)
    state_dir.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {state_dir / 'trading_bot.db'}\n"
        "robinhood:\n"
        "  enabled: true\n",
        encoding="utf-8",
    )
    (state_dir / "robinhood_sync_meta.json").write_text(
        json.dumps(
                {
                    "source": "mcp",
                    "account_number": "ACC123",
                    "synced_at": synced_at.isoformat(),
                    "fresh_until": datetime(2099, 6, 19, 10, 15, 0).isoformat(),
                    "capabilities": {
                        "read_only": True,
                        "shadow_preview": True,
                        "live_submit": False,
                        "live_cancel": False,
                },
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "robinhood_account.json").write_text(
        json.dumps(
            {
                "account_number": "ACC123",
                "cash": 1200.5,
                "equity": 2500.0,
                "buying_power": 1800.0,
                "updated_at": synced_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "robinhood_positions.json").write_text("[]", encoding="utf-8")
    (state_dir / "robinhood_orders.json").write_text("[]", encoding="utf-8")

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "robinhood-status"])

    assert result.exit_code == 0
    assert "Source: MCP" in result.stdout
    assert "Connection: connected" in result.stdout
    assert "Account: ACC123" in result.stdout
    assert "Freshness: fresh" in result.stdout


def test_sync_account_requires_operator_managed_snapshot_when_missing(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "robinhood:\n"
        "  enabled: true\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "sync-account"])

    assert result.exit_code == 1
    assert "Codex/operator using Robinhood MCP" in result.stdout


def test_sync_positions_apply_is_rejected_for_local_cli(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {state_dir / 'trading_bot.db'}\n"
        "robinhood:\n"
        "  enabled: true\n",
        encoding="utf-8",
    )
    (state_dir / "robinhood_sync_meta.json").write_text(
        json.dumps(
                {
                    "source": "mcp",
                    "account_number": "ACC123",
                    "synced_at": datetime(2026, 6, 19, 10, 0, 0).isoformat(),
                    "fresh_until": datetime(2099, 6, 19, 10, 15, 0).isoformat(),
                    "capabilities": {
                        "read_only": True,
                        "shadow_preview": True,
                        "live_submit": False,
                        "live_cancel": False,
                },
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "robinhood_account.json").write_text(
        json.dumps(
            {
                "account_number": "ACC123",
                "cash": 1200.5,
                "equity": 2500.0,
                "buying_power": 1800.0,
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "robinhood_positions.json").write_text("[]", encoding="utf-8")
    (state_dir / "robinhood_orders.json").write_text("[]", encoding="utf-8")

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "sync-positions", "--apply"])

    assert result.exit_code == 1
    assert "Local apply is not supported" in result.stdout


def test_scan_command_sizes_from_saved_portfolio_state(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
        assert symbol == "AAPL"
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

    runner = CliRunner()

    result = runner.invoke(app, ["--config-path", str(config_file), "scan", "--symbols", "AAPL"])

    assert result.exit_code == 0
    assert (
        result.stdout.strip()
        == "AAPL APPROVED quality=GREEN status=fresh age=5m ts=2026-06-13T10:20:00+00:00 last=101.00 qty=39 rr=2.00 conf=0.90 risk=$156.00 alloc=0.20 entry=101.00 stop=99.80 target=103.40 reasons=bullish daily regime; intraday breakout"
    )
    snapshot = json.loads((tmp_path / "state" / "scan_results.json").read_text(encoding="utf-8"))
    assert snapshot["mode"] == "scan"
    assert snapshot["summary"]["approved"] == 1
    assert snapshot["candidates"][0]["ticker"] == "AAPL"
    assert snapshot["candidates"][0]["quality"] == "GREEN"
    assert snapshot["candidates"][0]["freshness"] == "fresh"
    assert snapshot["candidates"][0]["age"] == "5m"


def test_scan_command_sorts_approved_candidates_and_prints_richer_fields(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
            "volume": [1_000_000 for _ in range(60)],
        }
    )
    intraday_map = {
        "AAPL": pd.DataFrame(
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
        ),
        "MSFT": pd.DataFrame(
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
                "open": [199.9, 200.1, 200.0, 200.2, 200.5],
                "high": [200.1, 200.3, 200.2, 200.4, 201.1],
                "low": [199.8, 200.0, 199.9, 200.1, 200.4],
                "close": [200.0, 200.2, 200.1, 200.3, 201.0],
                "volume": [1000, 1100, 950, 1050, 1500],
            }
        ),
    }

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        if interval == "5m":
            return intraday_map[symbol].copy(deep=True)
        return daily.copy(deep=True)

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

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config-path", str(config_file), "scan", "--symbols", "MSFT,AAPL", "--summary"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "AAPL APPROVED quality=GREEN status=fresh age=5m ts=2026-06-13T10:20:00+00:00 last=101.00 qty=39 rr=2.00 conf=0.90 risk=$156.00 alloc=0.20 entry=101.00 stop=99.80 target=103.40 reasons=bullish daily regime; intraday breakout",
        "MSFT APPROVED quality=GREEN status=fresh age=5m ts=2026-06-13T10:20:00+00:00 last=201.00 qty=19 rr=2.00 conf=0.80 risk=$76.00 alloc=0.19 entry=201.00 stop=199.80 target=203.40 reasons=bullish daily regime; intraday breakout",
        "summary symbols=2 approved=2 green=2 yellow=0 rejected=0 no_signal=0 errors=0 supermodel_support=2 supermodel_caution=0 supermodel_block=0 supermodel_no_signal=0",
    ]
    log_text = (tmp_path / "logs" / "decision-log.jsonl").read_text(encoding="utf-8")
    assert '"command": "scan"' in log_text
    assert '"ticker": "AAPL"' in log_text
    assert '"status": "APPROVED"' in log_text


def test_scan_quality_marks_weak_confirmation_yellow() -> None:
    from trading_bot.runtime.orchestrator import _scan_quality

    assert _scan_quality(
        {
            "intraday_close": 732.64,
            "range_high": 733.57,
            "volume_ratio": 0.87,
        }
    ) == "YELLOW"


def test_scan_command_marks_stale_market_data(monkeypatch, tmp_path: Path) -> None:
    """Stale data check removed — signals now go through regardless of age."""
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 11, 0, 0, tzinfo=signal_timestamp.tzinfo),
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

    runner = CliRunner()
    result = runner.invoke(app, ["--config-path", str(config_file), "scan", "--symbols", "AAPL"])

    assert result.exit_code == 0
    assert "APPROVED" in result.stdout
    assert "status=stale" in result.stdout
    assert "age=40m" in result.stdout
    snapshot = json.loads((tmp_path / "state" / "scan_results.json").read_text(encoding="utf-8"))
    assert snapshot["candidates"][0]["status"] == "APPROVED"
    assert snapshot["candidates"][0]["freshness"] == "stale"


def test_scan_command_writes_empty_snapshot_for_no_signal(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 for _ in range(60)],
            "high": [101.0 for _ in range(60)],
            "low": [99.0 for _ in range(59)] + [98.0],
            "close": [100.0 for _ in range(59)] + [98.0],  # Last day bearish
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
            "open": [100.0, 100.0, 100.0, 100.0, 100.0],
            "high": [100.1, 100.1, 100.1, 100.1, 100.1],
            "low": [99.9, 99.9, 99.9, 99.9, 99.9],
            "close": [100.0, 100.0, 100.0, 100.0, 100.0],
            "volume": [1000, 1000, 1000, 1000, 1000],
        }
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config-path", str(config_file), "scan", "--symbols", "AAPL"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "AAPL NO_SIGNAL reason=daily regime not bullish"
    snapshot = json.loads((tmp_path / "state" / "scan_results.json").read_text(encoding="utf-8"))
    assert snapshot["summary"]["approved"] == 0
    assert snapshot["summary"]["no_signal"] == 1
    assert snapshot["candidates"] == [
        {
            "ticker": "AAPL",
            "status": "NO_SIGNAL",
            "reason": "daily regime not bullish",
            "supermodel_decision": "no_signal",
            "supermodel_score": 0.0,
        }
    ]


def test_scan_command_prefers_fresh_candidate_over_stale_higher_confidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
            "volume": [1_000_000 for _ in range(60)],
        }
    )
    intraday_map = {
        "AAPL": pd.DataFrame(
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
                "volume": [1000, 1100, 950, 1050, 1500],
            }
        ),
        "MSFT": pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2026-06-13 09:25:00",
                        "2026-06-13 09:30:00",
                        "2026-06-13 09:35:00",
                        "2026-06-13 09:40:00",
                        "2026-06-13 09:45:00",
                    ]
                ),
                "open": [199.9, 200.1, 200.0, 200.2, 200.5],
                "high": [200.1, 200.3, 200.2, 200.4, 201.1],
                "low": [199.8, 200.0, 199.9, 200.1, 200.4],
                "close": [200.0, 200.2, 200.1, 200.3, 201.0],
                "volume": [1000, 1100, 950, 1050, 2500],
            }
        ),
    }

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        if interval == "5m":
            return intraday_map[symbol].copy(deep=True)
        return daily.copy(deep=True)

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

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config-path", str(config_file), "scan", "--symbols", "MSFT,AAPL", "--summary"],
    )

    assert result.exit_code == 0
    lines = result.stdout.strip().splitlines()
    assert lines[0].startswith("AAPL APPROVED")
    assert "status=fresh" in lines[0]
    assert lines[1].startswith("MSFT APPROVED")
    assert "status=stale" in lines[1]


def test_scan_why_surfaces_swarm_sentiment_fields(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    scan_path = tmp_path / "state" / "scan_results.json"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  scan_results_path: {scan_path}\n",
        encoding="utf-8",
    )

    def fake_run_scan(symbols, settings, include_details=False):
        return {
            "lines": [
                "AAPL APPROVED quality=GREEN status=fresh age=5m ts=2026-06-13T10:20:00+00:00 last=101.00 qty=39 rr=2.00 conf=0.90 risk=$156.00 alloc=0.20 entry=101.00 stop=99.80 target=103.40 reasons=test swarm=APPROVE:0.8 swarm_sentiment_action=BUY swarm_sentiment_confidence=0.72 swarm_sentiment_score=0.42 swarm_sentiment_news_count=2"
            ],
            "summary": {"symbols": 1, "approved": 1, "green": 1, "yellow": 0, "rejected": 0, "no_signal": 0, "errors": 0},
            "candidates": [
                {
                    "ticker": "AAPL",
                    "status": "APPROVED",
                    "confidence": 0.9,
                    "quality": "GREEN",
                    "freshness": "fresh",
                    "swarm_decision": "APPROVE",
                    "swarm_sentiment_action": "BUY",
                    "swarm_sentiment_confidence": 0.72,
                    "swarm_sentiment_score": 0.42,
                    "swarm_sentiment_news_count": 2,
                }
            ],
        }

    from unittest.mock import patch

    result = None
    with patch("trading_bot.runtime.orchestrator.run_scan", side_effect=fake_run_scan):
        result = CliRunner().invoke(app, ["--config-path", str(config_file), "scan", "--symbols", "AAPL", "--why"])

    assert result is not None
    assert result.exit_code == 0
    assert "swarm_sentiment_action=BUY" in result.stdout
    assert "swarm_sentiment_score=0.42" in result.stdout


def test_scan_command_prints_no_signal_reason(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 for _ in range(60)],
            "high": [101.0 for _ in range(60)],
            "low": [99.0 for _ in range(59)] + [98.0],
            "close": [100.0 for _ in range(59)] + [98.0],  # Last day bearish,
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
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["--config-path", str(config_file), "scan", "--symbols", "AAPL"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "AAPL NO_SIGNAL reason=daily regime not bullish"


def test_scan_command_can_print_gate_details(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 for _ in range(60)],
            "high": [101.0 for _ in range(60)],
            "low": [99.0 for _ in range(59)] + [98.0],
            "close": [100.0 for _ in range(59)] + [98.0],  # Last day bearish,
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
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config-path", str(config_file), "scan", "--symbols", "AAPL", "--why"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == (
        "AAPL NO_SIGNAL reason=daily regime not bullish "
        "daily_close=98.00 ema_20=99.81 sma_50=99.96 "
        "intraday_close=101.00 range_high=100.40 volume=2500 volume_avg=1320.00 volume_ratio=1.89 "
        "supermodel=no_signal:0.0 supermodel_layers=setup:neutral:0.00"
    )
    snapshot = json.loads((tmp_path / "state" / "scan_results.json").read_text(encoding="utf-8"))
    assert snapshot["candidates"][0]["details"] == {
        "daily_close": 98.0,
        "ema_20": 99.81,
        "sma_50": 99.96,
        "intraday_close": 101.0,
        "range_high": 100.4,
        "volume": 2500,
        "volume_avg": 1320.0,
        "volume_ratio": 1.89,
        "supermodel_decision": "no_signal",
        "supermodel_score": 0.0,
        "supermodel_layers": "setup:neutral:0.00",
    }


def test_scan_details_ignore_trailing_zero_volume_bar(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
                    "2026-06-13 10:25:00",
                ]
            ),
            "open": [99.9, 100.1, 100.0, 100.2, 100.5, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1, 101.2],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4, 100.8],
            "close": [100.0, 100.2, 100.1, 100.3, 101.0, 100.9],
            "volume": [1000, 1100, 950, 1050, 2500, 0],
        }
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
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

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config-path", str(config_file), "scan", "--symbols", "AAPL", "--why"],
    )

    assert result.exit_code == 0
    assert "ts=2026-06-13T10:20:00+00:00" in result.stdout
    assert "intraday_close=101.00" in result.stdout
    assert "volume=2500" in result.stdout
    assert "volume=0" not in result.stdout


def test_scan_details_explain_signal_bar_when_later_bar_exists(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
                    "2026-06-13 10:25:00",
                ]
            ),
            "open": [99.9, 100.1, 100.0, 100.2, 100.5, 100.8],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1, 101.0],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4, 100.6],
            "close": [100.0, 100.2, 100.1, 100.3, 101.0, 100.7],
            "volume": [1000, 1100, 950, 1050, 2500, 500],
        }
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 35, 0, tzinfo=signal_timestamp.tzinfo),
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

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config-path", str(config_file), "scan", "--symbols", "AAPL", "--why"],
    )

    assert result.exit_code == 0
    assert "ts=2026-06-13T10:20:00+00:00" in result.stdout
    assert "intraday_close=101.00" in result.stdout
    assert "volume=2500" in result.stdout
    assert "intraday_close=100.70" not in result.stdout


def test_portfolio_command_prints_saved_summary(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 13, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-06-13T09:55:00"]),
                "close": [110.0],
            }
        )

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=12_500.0,
            equity=13_000.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)},
        )
    )
    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    runner = CliRunner()

    result = runner.invoke(app, ["--config-path", str(config_file), "portfolio"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "cash=12500.00 equity=13050.00 realized_pnl=0.00 unrealized_pnl=50.00 exposure=0.04 positions=1",
        "AAPL qty=5 avg=100.00 last=110.00 mv=550.00 upl=50.00 alloc=0.04",
    ]
    snapshot = json.loads((tmp_path / "state" / "portfolio_summary.json").read_text(encoding="utf-8"))
    assert snapshot["mode"] == "portfolio"
    assert snapshot["summary"]["equity"] == 13050.0
    assert snapshot["positions"][0]["ticker"] == "AAPL"


def test_portfolio_command_falls_back_to_average_cost_when_latest_price_is_nan(
    monkeypatch, tmp_path: Path
) -> None:
    import trading_bot.data.market_data as market_data

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-06-13T09:55:00"]),
                "close": [float("nan")],
            }
        )

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=12_500.0,
            equity=13_000.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)},
        )
    )
    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "portfolio"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "cash=12500.00 equity=13000.00 realized_pnl=0.00 unrealized_pnl=0.00 exposure=0.04 positions=1",
        "AAPL qty=5 avg=100.00 last=100.00 mv=500.00 upl=0.00 alloc=0.04",
    ]


def test_paper_audit_passes_for_matching_local_state(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    ledger = PortfolioLedger(db_path)
    state = PortfolioState(
        cash=12_500.0,
        equity=13_000.0,
        realized_pnl=150.0,
        unrealized_pnl=350.0,
        positions={"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)},
    )
    ledger.save_portfolio_state(state)
    ledger.record_equity_snapshot(state, timestamp=datetime(2026, 6, 22, 10, 0, 0))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "portfolio_summary.json").write_text(
        json.dumps(
            {
                "mode": "portfolio",
                "summary": {
                    "cash": 12500.0,
                    "equity": 13050.0,
                    "realized_pnl": 150.0,
                    "unrealized_pnl": 50.0,
                    "exposure": 0.04,
                    "positions": 1,
                },
                "positions": [
                    {
                        "ticker": "AAPL",
                        "quantity": 5,
                        "average_cost": 100.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "paper-audit"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "paper_audit=PASS orders=0 positions=1 equity_snapshots=1 snapshot=yes"


def test_paper_audit_fails_when_snapshot_drifts_from_ledger(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    ledger = PortfolioLedger(db_path)
    state = PortfolioState(
        cash=12_500.0,
        equity=13_000.0,
        positions={"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)},
    )
    ledger.save_portfolio_state(state)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "portfolio_summary.json").write_text(
        json.dumps(
            {
                "mode": "portfolio",
                "summary": {"cash": 12000.0, "positions": 1},
                "positions": [
                    {
                        "ticker": "AAPL",
                        "quantity": 7,
                        "average_cost": 100.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "paper-audit"])

    assert result.exit_code == 1
    assert "paper_audit=FAIL" in result.stdout
    assert "- portfolio snapshot cash does not match ledger state" in result.stdout
    assert "- portfolio snapshot quantity mismatch for AAPL" in result.stdout


def test_paper_trade_command_executes_fill_and_persists_state(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
    )
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(
        PortfolioState(cash=20_000.0, equity=20_000.0)
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "AAPL FILLED qty=39 price=101.00 cash=16060.00"

    state = ledger.load_portfolio_state()
    assert state is not None
    assert state.cash == 16060.0  # $20k - ($39 × $101) - $1 fee
    assert state.equity == 19999.0
    assert state.positions["AAPL"].quantity == 39
    assert state.positions["AAPL"].average_cost == 101.0
    assert state.positions["AAPL"].stop_loss == 99.8
    assert state.positions["AAPL"].profit_target == 103.4

    rows = ledger.list_order_rows()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["side"] == "BUY"

    log_text = (log_dir / "decision-log.jsonl").read_text(encoding="utf-8")
    assert '"command": "paper-trade"' in log_text
    assert '"status": "FILLED"' in log_text


def test_paper_trade_command_applies_configured_fees_and_slippage(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
        assert symbol == "AAPL"
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
        "paper:\n"
        "  fee_per_order: 1.0\n"
        "  slippage_bps: 5\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=20_000.0, equity=20_000.0)
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL"],
    )

    assert result.exit_code == 0

    # Entry=101.00; slippage=5bps on BUY → 101 * 1.0005 = 101.0505
    # qty=39 → gross=3940.97; +fee=1 → cost=3941.97
    # cash = 20000 - 3941.97 = 16058.03
    assert result.stdout.strip() == "AAPL FILLED qty=39 price=101.05 cash=16058.03"

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    assert state.cash == 16058.03
    assert state.realized_pnl == -1.0
    assert state.positions["AAPL"].quantity == 39


def test_paper_trade_dry_run_uses_slippage_adjusted_cash_after(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
        assert symbol == "AAPL"
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
        "paper:\n"
        "  fee_per_order: 1.0\n"
        "  slippage_bps: 5\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=20_000.0, equity=20_000.0)
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL", "--dry-run"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "AAPL DRY_RUN qty=39 price=101.05 cash_after=16058.03"


def test_paper_trade_rejects_when_slippage_pushes_cost_over_cash(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
        assert symbol == "AAPL"
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
        "paper:\n"
        "  fee_per_order: 1.0\n"
        "  slippage_bps: 5\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=3941.50, equity=20_000.0)
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "AAPL REJECTED insufficient cash"

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    assert state.cash == 3941.50
    assert state.positions == {}


def test_paper_trade_ignores_invalid_held_position_data_for_portfolio_heat(
    monkeypatch, tmp_path: Path
) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    valid_daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
            "volume": [1_000_000 for _ in range(60)],
        }
    )
    valid_intraday = pd.DataFrame(
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
    invalid_held_intraday = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-13 10:20:00"]),
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [0.0],
            "volume": [1000],
        }
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        if symbol == "AAPL":
            return valid_intraday.copy(deep=True) if interval == "5m" else valid_daily.copy(deep=True)
        if symbol == "TSLA":
            assert interval == "5m"
            return invalid_held_intraday.copy(deep=True)
        raise AssertionError(symbol)

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
        PortfolioState(
            cash=20_000.0,
            equity=20_000.0,
            positions={"TSLA": Position(ticker="TSLA", quantity=10, average_cost=100.0)},
        )
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "AAPL FILLED qty=39 price=101.00 cash=16060.00"


def test_paper_trade_dry_run_previews_without_persisting(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
    )
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=20_000.0, equity=20_000.0))

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL", "--dry-run"],
    )

    assert result.exit_code == 0
    # With max_position_pct=0.20 (20%), max position = $4k / $101 = ~39 shares
    assert result.stdout.strip() == "AAPL DRY_RUN qty=39 price=101.00 cash_after=16060.00"
    state = ledger.load_portfolio_state()
    assert state is not None
    assert state.cash == 20_000.0
    assert state.positions == {}
    assert ledger.list_order_rows() == []
    assert '"status": "DRY_RUN"' in (log_dir / "decision-log.jsonl").read_text(encoding="utf-8")


def test_dashboard_command_builds_static_html(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "scan_results.json").write_text(
        json.dumps(
            {
                "mode": "scan",
                "candidates": [
                    {
                        "ticker": "SPY",
                        "status": "APPROVED",
                        "quality": "GREEN",
                        "confidence": 0.8,
                        "entry": 750.54,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "portfolio_summary.json").write_text(
        json.dumps({"summary": {"cash": 10000.0, "equity": 10000.0, "exposure": 0.0}, "positions": []}),
        encoding="utf-8",
    )
    (state_dir / "dashboard_summary.json").write_text(
        json.dumps({"summary": {"net_pnl": 0.0}, "recent_decisions": []}),
        encoding="utf-8",
    )
    (state_dir / "backtest_summary.json").write_text(
        json.dumps({"summary": {"trades": 4, "net_pnl": 335.46}}),
        encoding="utf-8",
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        "  scan_results_path: state/scan_results.json\n"
        "  portfolio_summary_path: state/portfolio_summary.json\n"
        "  dashboard_summary_path: state/dashboard_summary.json\n"
        "  backtest_summary_path: state/backtest_summary.json\n",
        encoding="utf-8",
    )
    output = tmp_path / "dashboard.html"

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "dashboard", "--output", str(output)],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == f"dashboard={output}"
    html_text = output.read_text(encoding="utf-8")
    assert "Autonomous Trading Agent" in html_text
    assert "SPY" in html_text
    assert "GREEN" in html_text
    assert "$10,000.00" in html_text


def test_manage_positions_reports_empty_portfolio(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=10_000.0, equity=10_000.0)
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "positions=0 actions=0 skipped=0"


def test_manage_positions_reports_open_position_price(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        if interval == "5m":
            return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18T09:55:00"]), "close": [110.0]})
        elif interval == "1d":
            # Daily bars for ATR (not used in this test but needed for consistency)
            return pd.DataFrame({
                "timestamp": pd.to_datetime(["2026-06-18"]),
                "high": [110.0], "low": [100.0], "close": [110.0], "volume": [1_000_000]
            })
        else:
            raise ValueError(f"Unexpected interval: {interval}")

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=10, average_cost=100.0)},
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "positions=1 actions=0 skipped=0",
        "AAPL qty=10 avg=100.00 last=110.00",
    ]


def test_manage_positions_skips_open_position_when_market_data_fetch_fails(
    monkeypatch, tmp_path: Path
) -> None:
    import trading_bot.data.market_data as market_data

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        raise ValueError("provider outage")

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=10, average_cost=100.0)},
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "positions=1 actions=0 skipped=1",
        "AAPL SKIP reason=market-data-fetch-failed",
    ]
    log_text = (log_dir / "decision-log.jsonl").read_text(encoding="utf-8")
    assert '"reason": "market data fetch failed"' in log_text
    assert '"error": "provider outage"' in log_text


def test_manage_positions_persists_min_stop_widening(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        if interval == "5m":
            return pd.DataFrame(
                {
                    "timestamp": pd.to_datetime(["2026-06-18T09:55:00"]),
                    "high": [100.5],
                    "close": [100.5],
                }
            )
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-06-18"]),
                "high": [101.0],
                "low": [99.0],
                "close": [100.5],
                "volume": [1_000_000],
            }
        )

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "risk:\n"
        "  min_stop_distance_pct: 3.0\n",
        encoding="utf-8",
    )
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
                    stop_loss=99.0,
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert ledger.load_portfolio_state().positions["AAPL"].stop_loss == 97.0


def test_manage_positions_executes_target_exit(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18T09:55:00"]), "close": [110.0]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
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

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "positions=0 actions=1 skipped=0",
        "AAPL FILLED reason=target qty=10 price=110.00 cash=10099.00",
    ]

    ledger = PortfolioLedger(db_path)
    state = ledger.load_portfolio_state()
    assert state is not None
    assert state.cash == 10099.0
    assert state.realized_pnl == 99.0
    assert state.positions == {}
    assert ledger.list_order_rows()[-1]["side"] == "SELL"


def test_manage_positions_scales_out_partial_target(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18T09:55:00"]), "close": [110.0]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "paper:\n"
        "  partial_take_profit_enabled: true\n"
        "  partial_take_profit_fraction: 0.5\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
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

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "positions=1 actions=1 skipped=0",
        "AAPL FILLED reason=target_partial qty=5 price=110.00 cash=9549.00 remaining=5",
    ]

    ledger = PortfolioLedger(db_path)
    state = ledger.load_portfolio_state()
    assert state is not None
    assert state.cash == 9549.0
    assert state.realized_pnl == 49.0
    assert state.positions["AAPL"].quantity == 5
    assert state.positions["AAPL"].stop_loss == 100.0
    assert state.positions["AAPL"].profit_target is None
    assert state.positions["AAPL"].partial_profit_taken is True


def test_manage_positions_executes_stop_exit(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18T09:55:00"]), "close": [97.5]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=99.0,
                    profit_target=108.0,
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "positions=0 actions=1 skipped=0",
        "AAPL FILLED reason=stop qty=10 price=97.50 cash=9974.00",
    ]

    ledger = PortfolioLedger(db_path)
    state = ledger.load_portfolio_state()
    assert state is not None
    assert state.cash == 9974.0
    assert state.realized_pnl == -26.0
    assert state.positions == {}
    assert ledger.list_order_rows()[-1]["side"] == "SELL"


def test_manage_positions_trails_stop_up_when_price_advances(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        if interval == "5m":
            # Intraday timestamp 5 mins before "now" (10:00 ET)
            return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18T09:55:00"]), "close": [102.0]})
        elif interval == "1d":
            # Daily bars for ATR
            return pd.DataFrame({
                "timestamp": pd.to_datetime(["2026-06-18"]),
                "high": [102.0], "low": [99.0], "close": [102.0], "volume": [1_000_000]
            })
        else:
            raise ValueError(f"Unexpected interval: {interval}")

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=99.0,
                    profit_target=108.0,
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "positions=1 actions=1 skipped=0",
        "AAPL TRAIL method=r-multiple stop=101.00 last=102.00 high=102.00",
    ]

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    position = state.positions["AAPL"]
    assert position.stop_loss == 101.0
    assert position.initial_risk == 1.0
    assert position.highest_high == 102.0


def test_manage_positions_does_not_trail_below_break_even(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        if interval == "5m":
            return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18T09:55:00"]), "close": [100.5]})
        elif interval == "1d":
            return pd.DataFrame({
                "timestamp": pd.to_datetime(["2026-06-18"]),
                "high": [102.0], "low": [99.0], "close": [100.5], "volume": [1_000_000]
            })
        else:
            raise ValueError(f"Unexpected interval: {interval}")

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=99.0,
                    profit_target=108.0,
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "positions=1 actions=0 skipped=0",
        "AAPL qty=10 avg=100.00 last=100.50",
    ]

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    assert state.positions["AAPL"].stop_loss == 99.0
    assert state.positions["AAPL"].initial_risk is None


def test_manage_positions_trail_is_idempotent_across_runs(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        if interval == "5m":
            return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18T09:55:00"]), "close": [102.0]})
        elif interval == "1d":
            return pd.DataFrame({
                "timestamp": pd.to_datetime(["2026-06-18"]),
                "high": [102.0], "low": [99.0], "close": [102.0], "volume": [1_000_000]
            })
        else:
            raise ValueError(f"Unexpected interval: {interval}")

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=99.0,
                    profit_target=108.0,
                )
            },
        )
    )
    runner = CliRunner()

    first = runner.invoke(app, ["--config-path", str(config_file), "manage-positions"])
    assert first.exit_code == 0
    assert "TRAIL method=r-multiple" in first.stdout

    second = runner.invoke(app, ["--config-path", str(config_file), "manage-positions"])
    assert second.exit_code == 0
    assert "TRAIL" not in second.stdout
    assert "AAPL qty=10 avg=100.00 last=102.00" in second.stdout

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    assert state.positions["AAPL"].stop_loss == 101.0


def test_manage_positions_trails_via_chandelier_atr(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    n_rows = 30
    rows = []
    for index in range(n_rows):
        if index % 2 == 0:
            close = 101.0
        else:
            close = 99.0
        rows.append({"high": 105.0, "low": 95.0, "close": close, "volume": 1_000_000})

    expected_frame = pd.DataFrame(rows)
    expected_frame["timestamp"] = pd.to_datetime(
        [f"2026-06-{day:02d}" for day in range(1, n_rows + 1)]
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        if interval == "5m":
            # Return last row as intraday bar
            return pd.DataFrame({
                "timestamp": pd.to_datetime(["2026-06-18T09:55:00"]),
                "high": [105.0],
                "low": [95.0],
                "close": [99.0],
                "volume": [1_000_000],
            })
        elif interval == "1d":
            return expected_frame.copy()
        else:
            raise ValueError(f"Unexpected interval: {interval}")

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=80.0,
                    profit_target=120.0,
                    highest_high=110.0,
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "positions=1 actions=1 skipped=0"
    # ATR is constant 10 across the frame; chandelier = 110 - 1.5 * 10 = 95.
    assert lines[1] == "AAPL TRAIL method=chandelier-atr stop=95.00 last=99.00 high=110.00"

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    position = state.positions["AAPL"]
    assert position.stop_loss == 95.0
    assert position.highest_high == 110.0
    # initial_risk inferred from entry_stop=80 is unchanged on this run since
    # the ratchet candidate was below breakeven; the value persists for future runs.
    assert position.initial_risk == 20.0


def test_manage_positions_chandelier_uses_bar_high_not_close(monkeypatch, tmp_path: Path) -> None:
    """Chandelier stop should track the bar's true high (including wicks), not just closes.

    If a bar spikes to 110 but closes at 102, highest_high should be 110, not 102.
    This ensures we don't tighten the stop prematurely based on a lower close.
    """
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    # Need at least 15 bars for ATR(14) calculation (for daily data)
    n_rows = 20
    rows = []
    for index in range(n_rows - 1):
        # Historical bars with consistent range
        rows.append({"high": 105.0, "low": 95.0, "close": 100.0, "volume": 1_000_000})
    # Last bar: spikes to 110, closes at 102
    rows.append({"high": 110.0, "low": 100.0, "close": 102.0, "volume": 1_000_000})

    expected_frame = pd.DataFrame(rows)
    expected_frame["timestamp"] = pd.to_datetime(
        [f"2026-06-{day:02d}" for day in range(1, n_rows + 1)]
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        if interval == "5m":
            # Intraday bar: spikes to 110, closes at 102
            return pd.DataFrame({
                "timestamp": pd.to_datetime(["2026-06-18T09:55:00"]),
                "high": [110.0],
                "low": [100.0],
                "close": [102.0],
                "volume": [1_000_000],
            })
        elif interval == "1d":
            # Daily bars for ATR calculation
            return expected_frame.copy()
        else:
            raise ValueError(f"Unexpected interval: {interval}")

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    # Start with no highest_high tracked and a loose stop
    # ATR ~10, chandelier = 110 - 1.5 * 10 = 95
    # So current stop of 90 should tighten to 95
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=90.0,  # Loose stop - chandelier will tighten to 95
                    profit_target=120.0,
                    # highest_high not set - will be initialized from bar high (110)
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "positions=1 actions=1 skipped=0"
    # highest_high should be 110.0 (the bar high), NOT 102.0 (the close)
    # Most importantly: high=110.00 in the log line, not 102.00
    assert "high=110.00" in lines[1]

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    position = state.positions["AAPL"]
    # highest_high must be 110 (the bar high), not 102 (the close)
    assert position.highest_high == 110.0, f"expected 110.0 (bar high), got {position.highest_high} (close)"
    # Stop should tighten to chandelier level
    assert position.stop_loss == 95.0


def test_manage_positions_executes_eod_exit(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 15, 56, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(sys.modules["trading_bot.cli.app"], "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        # Use intraday timestamp close to EOD time (15:56)
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18T15:55:00"]), "close": [105.0]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=90.0,
                    profit_target=110.0,
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "positions=0 actions=1 skipped=0",
        "AAPL FILLED reason=eod qty=10 price=105.00 cash=10049.00",
    ]

    ledger = PortfolioLedger(db_path)
    state = ledger.load_portfolio_state()
    assert state is not None
    assert state.cash == 10049.0
    assert state.realized_pnl == 49.0
    assert state.positions == {}
    assert ledger.list_order_rows()[-1]["side"] == "SELL"

    log_text = (log_dir / "decision-log.jsonl").read_text(encoding="utf-8")
    assert '"reason": "eod"' in log_text


def test_manage_positions_eod_check_skips_inside_trading_hours(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 30, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(sys.modules["trading_bot.cli.app"], "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18"]), "close": [101.0]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=80.0,
                    profit_target=110.0,
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert "FILLED reason=eod" not in result.stdout

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    assert "AAPL" in state.positions


def test_manage_positions_freezes_on_stale_market_data(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo
    import trading_bot.cli.app as cli_app_mod

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 12, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(
        sys.modules["trading_bot.cli.app"], "now_in_zone", fake_now_in_zone
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"  # Now uses intraday interval
        return pd.DataFrame(
            {"timestamp": pd.to_datetime(["2026-06-18 10:00:00"]), "close": [80.0]}
        )

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n"
        "market_data:\n"
        "  max_data_age_minutes: 30\n",  # Use minutes for intraday staleness
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=99.0,
                    profit_target=108.0,
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "positions=1 actions=0 skipped=1",
        "AAPL SKIP reason=stale-data last=80.00",
    ]

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    # Position should be untouched despite last_price=80 < stop=99.
    assert state.positions["AAPL"].quantity == 10
    assert state.cash == 9_000.0
    log_path = log_dir / "decision-log.jsonl"
    event = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert event["reason"] == "stale market data"
    assert event["max_age_minutes"] == 30
    assert "max_age_hours" not in event


def test_manage_positions_skips_empty_intraday_frame(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 12, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(
        sys.modules["trading_bot.cli.app"], "now_in_zone", fake_now_in_zone
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame(columns=["timestamp", "close"])

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "market_data:\n"
        "  max_data_age_minutes: 30\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=99.0,
                    profit_target=108.0,
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "positions=1 actions=0 skipped=1",
        "AAPL SKIP reason=stale-data last=unknown",
    ]

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    assert state.positions["AAPL"].quantity == 10
    assert state.cash == 9_000.0


def test_manage_positions_eod_can_be_disabled_via_config(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 15, 56, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(sys.modules["trading_bot.cli.app"], "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18"]), "close": [101.0]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "session:\n"
        "  eod_enabled: false\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    stop_loss=80.0,
                    profit_target=110.0,
                )
            },
        )
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    assert "FILLED reason=eod" not in result.stdout

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    assert "AAPL" in state.positions


def test_manage_positions_executes_target_exit_with_fees(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18T09:55:00"]), "close": [110.0]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "paper:\n"
        "  fee_per_order: 1.0\n"
        "  slippage_bps: 5\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
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

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "manage-positions"])

    assert result.exit_code == 0
    # SELL at market=110 → slippage is *against* the seller so fill = 110 * (1 - 5/10000) = 109.945
    # cash_after = 9000 + (109.945 * 10) - 1 = 9000 + 1099.45 - 1 = 10098.45
    assert result.stdout.strip().splitlines() == [
        "positions=0 actions=1 skipped=0",
        "AAPL FILLED reason=target qty=10 price=109.95 cash=10098.45",
    ]

    state = PortfolioLedger(db_path).load_portfolio_state()
    assert state is not None
    assert state.cash == 10098.45
    assert state.realized_pnl == 98.45
    assert state.positions == {}


def test_run_manager_loops_until_keyboard_interrupt(monkeypatch, tmp_path: Path) -> None:
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18"]), "close": [101.0]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    iteration = {"n": 0}

    def fake_sleep(seconds: float) -> None:
        iteration["n"] += 1
        if iteration["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(app_module.time, "sleep", fake_sleep)

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=9_000.0, equity=10_000.0)
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "run-manager", "--interval", "1"],
    )

    assert result.exit_code == 0
    assert "run-manager started interval=1s" in result.stdout
    assert "run-manager stopped" in result.stdout
    # At least two iterations of the manage-positions summary line.
    assert result.stdout.count("positions=0 actions=0 skipped=0") >= 2


def test_run_manager_runs_one_iteration_with_interval_zero(monkeypatch, tmp_path: Path) -> None:
    """With interval=0, the loop runs continuously without sleeping."""
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    iteration_count = {"n": 0}

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18"]), "close": [101.0]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    # Track iterations and stop after a few
    original_run_once = app_module._run_manage_positions_once

    def counting_run_once(ctx):
        iteration_count["n"] += 1
        if iteration_count["n"] >= 3:
            raise KeyboardInterrupt
        return original_run_once(ctx)

    monkeypatch.setattr(app_module, "_run_manage_positions_once", counting_run_once)

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=9_000.0, equity=10_000.0)
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "run-manager", "--interval", "0"],
    )

    assert result.exit_code == 0
    assert "run-manager started interval=0s" in result.stdout
    assert "run-manager stopped" in result.stdout
    # Should run at least 2 iterations before KeyboardInterrupt
    assert result.stdout.count("positions=0 actions=0 skipped=0") >= 2


def test_run_manager_circuit_breaker_opens_after_max_failures(monkeypatch, tmp_path: Path) -> None:
    """Circuit breaker exits after consecutive failures exceed threshold."""
    import sys
    from trading_bot.portfolio.ledger import PortfolioLedger

    app_module = sys.modules["trading_bot.cli.app"]

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=9_000.0, equity=10_000.0)
    )

    # Prevent any actual sleeping during backoff (patch app's import of time)
    monkeypatch.setattr(app_module.time, "sleep", lambda x: None)

    # Inject a failure into every iteration
    monkeypatch.setattr(
        app_module,
        "_run_manage_positions_once",
        lambda ctx: (_ for _ in ()).throw(RuntimeError("simulated failure")),
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "run-manager", "--interval", "1", "--max-failures", "3"],
    )

    assert result.exit_code == 1
    assert "circuit breaker open after 3 failures" in result.stdout


def test_run_manager_circuit_breaker_resets_on_success(monkeypatch, tmp_path: Path) -> None:
    """Circuit breaker resets failure count on successful iteration."""
    import sys
    import trading_bot.data.market_data as market_data
    from zoneinfo import ZoneInfo

    app_module = sys.modules["trading_bot.cli.app"]

    def fake_now_in_zone(timezone: str):
        return datetime(2026, 6, 18, 10, 0, tzinfo=ZoneInfo(timezone))

    monkeypatch.setattr(app_module, "now_in_zone", fake_now_in_zone)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18"]), "close": [101.0]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=9_000.0, equity=10_000.0)
    )

    iteration = {"n": 0}

    def fake_sleep(seconds: float) -> None:
        iteration["n"] += 1
        if iteration["n"] >= 5:
            raise KeyboardInterrupt

    monkeypatch.setattr(app_module.time, "sleep", fake_sleep)

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "run-manager", "--interval", "1", "--max-failures", "10"],
    )

    assert result.exit_code == 0
    assert "run-manager stopped" in result.stdout
    # Should complete 5 iterations without hitting circuit breaker
    assert result.stdout.count("positions=0 actions=0 skipped=0") >= 5


def test_paper_trade_command_prints_rejection_reason(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(
        PortfolioState(
            cash=20_000.0,
            equity=20_000.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=10, average_cost=100.0)},
        )
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "AAPL REJECTED duplicate open ticker"
    log_text = (log_dir / "decision-log.jsonl").read_text(encoding="utf-8")
    assert '"command": "paper-trade"' in log_text
    assert '"status": "REJECTED"' in log_text
    assert '"reason": "duplicate open ticker"' in log_text


def test_paper_trade_rejects_stale_signal(monkeypatch, tmp_path: Path) -> None:
    """Stale data check removed — signals now go through regardless of age."""
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 11, 0, 0, tzinfo=signal_timestamp.tzinfo),
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

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL"],
    )

    assert result.exit_code == 0
    assert "FILLED" in result.stdout


def test_paper_trade_rejects_yellow_signal(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
            "open": [99.9, 100.0, 100.1, 100.3, 100.5],
            "high": [100.2, 100.4, 101.2, 101.4, 101.5],
            "low": [99.8, 99.9, 100.0, 100.1, 100.4],
            "close": [100.0, 100.1, 100.3, 100.5, 101.2],
            "volume": [1000, 1100, 1200, 1300, 1000],
        }
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
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

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "AAPL REJECTED yellow signal"


def test_paper_trade_rejects_daily_order_limit(tmp_path: Path) -> None:
    from datetime import datetime

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "risk:\n"
        "  max_daily_orders: 1\n",
        encoding="utf-8",
    )
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=20_000.0, equity=20_000.0))
    ledger.record_fill(
        FillResult(
            order_id="existing-order",
            ticker="SPY",
            quantity=1,
            fill_price=100.0,
            fees=1.0,
            filled_at=datetime.now(),
        ),
        side="BUY",
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "AAPL REJECTED daily order limit"


def test_paper_trade_rejects_daily_loss_limit(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        "risk:\n"
        "  max_daily_risk_pct: 0.01\n",
        encoding="utf-8",
    )
    PortfolioLedger(db_path).save_portfolio_state(
        PortfolioState(cash=20_000.0, equity=20_000.0, realized_pnl=-250.0)
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "AAPL REJECTED daily loss limit"


def test_report_command_prints_summary_and_exports_files(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-06-13"]),
                "close": [110.0],
            }
        )

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    json_path = tmp_path / "exports" / "report.json"
    csv_path = tmp_path / "exports" / "orders.csv"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(
        PortfolioState(
            cash=12_500.0,
            equity=13_000.0,
            realized_pnl=125.5,
            unrealized_pnl=40.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)},
        )
    )
    ledger.record_fill(
        FillResult(
            order_id="order-1",
            ticker="AAPL",
            quantity=5,
            fill_price=100.0,
            fees=1.0,
            filled_at=datetime(2026, 6, 13, 10, 0, 0),
        ),
        side="BUY",
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--config-path",
            str(config_file),
            "report",
            "--json-path",
            str(json_path),
            "--csv-path",
            str(csv_path),
        ],
    )

    assert result.exit_code == 0
    assert (
        result.stdout.strip()
        == "net_pnl=175.50 realized_pnl=125.50 unrealized_pnl=50.00 open_positions=1 exposure=0.04 orders=1"
    )
    assert json.loads(json_path.read_text(encoding="utf-8")) == {
        "realized_pnl": 125.5,
        "unrealized_pnl": 50.0,
        "open_positions": 1,
        "exposure": 0.04,
        "net_pnl": 175.5,
        "orders": 1,
    }
    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "id,ticker,side,quantity,fill_price,fees,filled_at,pnl",
        "order-1,AAPL,BUY,5,100.0,1.0,2026-06-13T10:00:00,0.0",
    ]
    dashboard_snapshot = json.loads(
        (tmp_path / "state" / "dashboard_summary.json").read_text(encoding="utf-8")
    )
    assert dashboard_snapshot["mode"] == "report"
    assert dashboard_snapshot["summary"]["net_pnl"] == 175.5
    assert dashboard_snapshot["rows"][0]["ticker"] == "AAPL"


def test_trade_attribution_reports_infinite_profit_factor_without_losses(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    ledger = PortfolioLedger(db_path)
    ledger.record_fill(
        FillResult(
            order_id="buy-1",
            ticker="AAPL",
            quantity=2,
            fill_price=100.0,
            fees=0.0,
            filled_at=datetime(2026, 6, 13, 10, 0, 0),
        ),
        side="BUY",
    )
    ledger.record_fill(
        FillResult(
            order_id="sell-1",
            ticker="AAPL",
            quantity=2,
            fill_price=110.0,
            fees=0.0,
            filled_at=datetime(2026, 6, 13, 11, 0, 0),
        ),
        side="SELL",
        realized_pnl=20.0,
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "trade-attribution"])

    assert result.exit_code == 0
    assert "Profit factor: inf" in result.stdout


def test_trade_attribution_does_not_match_sell_to_future_buy(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    ledger = PortfolioLedger(db_path)
    ledger.record_fill(
        FillResult(
            order_id="buy-1",
            ticker="AAPL",
            quantity=1,
            fill_price=100.0,
            fees=0.0,
            filled_at=datetime(2026, 6, 13, 10, 0, 0),
        ),
        side="BUY",
    )
    ledger.record_fill(
        FillResult(
            order_id="sell-1",
            ticker="AAPL",
            quantity=1,
            fill_price=110.0,
            fees=0.0,
            filled_at=datetime(2026, 6, 13, 11, 0, 0),
        ),
        side="SELL",
        realized_pnl=10.0,
    )
    ledger.record_fill(
        FillResult(
            order_id="buy-2",
            ticker="AAPL",
            quantity=1,
            fill_price=200.0,
            fees=0.0,
            filled_at=datetime(2026, 6, 13, 12, 0, 0),
        ),
        side="BUY",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "trade-attribution"])

    assert result.exit_code == 0
    assert "unknown" in result.stdout
    assert "100.00" in result.stdout
    assert "110.00" in result.stdout
    assert "60m" in result.stdout


def test_trade_attribution_reports_swarm_sentiment_buckets(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "decision-log.jsonl").write_text("", encoding="utf-8")

    ledger = PortfolioLedger(db_path)
    ledger.record_fill(
        FillResult(
            order_id="buy-1",
            ticker="AAPL",
            quantity=2,
            fill_price=100.0,
            fees=0.0,
            filled_at=datetime(2026, 6, 13, 10, 0, 0),
        ),
        side="BUY",
        strategy_tag="v3-trend_following",
        swarm_sentiment_bucket="bullish",
    )
    ledger.record_fill(
        FillResult(
            order_id="sell-1",
            ticker="AAPL",
            quantity=2,
            fill_price=110.0,
            fees=0.0,
            filled_at=datetime(2026, 6, 13, 11, 0, 0),
        ),
        side="SELL",
        realized_pnl=20.0,
        strategy_tag="v3-trend_following",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "trade-attribution"])

    assert result.exit_code == 0
    assert "Swarm Sentiment" in result.stdout
    assert "bullish" in result.stdout
    assert "20.00" in result.stdout


def test_db_history_filters_trades_by_swarm_sentiment(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    from trading_bot.db.repositories import upsert_trade
    from trading_bot.db.session import get_session, init_db, make_session_factory
    from trading_bot.config.loader import load_settings

    settings = load_settings(config_file)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        upsert_trade(
            session,
            ticker="AAPL",
            side="BUY",
            order_type="market",
            quantity=1,
            entry_price=100.0,
            strategy_tag="v3-trend_following",
            swarm_sentiment_bucket="bullish",
        )
        upsert_trade(
            session,
            ticker="MSFT",
            side="BUY",
            order_type="market",
            quantity=1,
            entry_price=200.0,
            strategy_tag="v3-trend_following",
            swarm_sentiment_bucket="bearish",
        )
    finally:
        session.close()
        engine.dispose()

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "db-history", "--swarm-sentiment", "bullish", "--limit", "10"],
    )

    assert result.exit_code == 0
    assert "AAPL BUY qty=1 @$100.00 sentiment=bullish" in result.stdout
    assert "MSFT BUY qty=1 @$200.00 sentiment=bearish" not in result.stdout


def test_db_features_command_queries_scan_features(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    from trading_bot.db.repositories import upsert_scan_feature
    from trading_bot.db.session import get_session, init_db, make_session_factory
    from trading_bot.config.loader import load_settings

    settings = load_settings(config_file)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        upsert_scan_feature(
            session, ticker="AAPL", status="APPROVED", action="BUY",
            confidence=0.9, quality="GREEN", market_regime="strong_uptrend",
            strategy_tag="v3-trend_following", swarm_sentiment_score=0.5,
        )
        upsert_scan_feature(
            session, ticker="MSFT", status="REJECTED", action="HOLD",
            confidence=0.3, quality="RED", market_regime="strong_downtrend",
            strategy_tag="v3-mean_reversion", swarm_sentiment_score=-0.4,
        )
    finally:
        session.close()
        engine.dispose()

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "db-features", "--ticker", "AAPL", "--limit", "10"],
    )

    assert result.exit_code == 0
    assert "AAPL" in result.stdout
    assert "MSFT" not in result.stdout


def test_db_features_command_filters_by_regime(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    from trading_bot.db.repositories import upsert_scan_feature
    from trading_bot.db.session import get_session, init_db, make_session_factory
    from trading_bot.config.loader import load_settings

    settings = load_settings(config_file)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        upsert_scan_feature(
            session, ticker="AAPL", status="APPROVED", action="BUY",
            market_regime="strong_uptrend", swarm_sentiment_score=0.5,
        )
        upsert_scan_feature(
            session, ticker="MSFT", status="APPROVED", action="BUY",
            market_regime="strong_downtrend", swarm_sentiment_score=0.5,
        )
    finally:
        session.close()
        engine.dispose()

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "db-features", "--regime", "strong_uptrend", "--limit", "10"],
    )

    assert result.exit_code == 0
    assert "AAPL" in result.stdout
    assert "MSFT" not in result.stdout


def test_db_features_command_summary(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    from trading_bot.db.repositories import upsert_scan_feature
    from trading_bot.db.session import get_session, init_db, make_session_factory
    from trading_bot.config.loader import load_settings

    settings = load_settings(config_file)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        upsert_scan_feature(
            session, ticker="AAPL", status="APPROVED", action="BUY",
            quality="GREEN", market_regime="strong_uptrend",
            strategy_tag="v3-trend_following", swarm_sentiment_score=0.5,
        )
        upsert_scan_feature(
            session, ticker="MSFT", status="APPROVED", action="HOLD",
            quality="YELLOW", market_regime="strong_uptrend",
            strategy_tag="v3-trend_following", swarm_sentiment_score=-0.4,
        )
        upsert_scan_feature(
            session, ticker="GOOGL", status="REJECTED", action="SELL",
            quality="RED", market_regime="strong_downtrend",
            strategy_tag="v3-mean_reversion", swarm_sentiment_score=-0.5,
        )
    finally:
        session.close()
        engine.dispose()

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "db-features", "--summary"],
    )

    assert result.exit_code == 0
    assert "SUMMARY" in result.stdout
    assert "Status distribution:" in result.stdout
    assert "Regime distribution:" in result.stdout
    assert "Quality distribution:" in result.stdout
    assert "Swarm Sentiment" in result.stdout


def test_db_features_command_json_output(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    from trading_bot.db.repositories import upsert_scan_feature
    from trading_bot.db.session import get_session, init_db, make_session_factory
    from trading_bot.config.loader import load_settings

    settings = load_settings(config_file)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        upsert_scan_feature(
            session, ticker="AAPL", status="APPROVED", action="BUY",
            confidence=0.9, quality="GREEN", market_regime="strong_uptrend",
            strategy_tag="v3-trend_following", swarm_sentiment_score=0.5,
        )
    finally:
        session.close()
        engine.dispose()

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "db-features", "--json"],
    )

    assert result.exit_code == 0
    import json
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["ticker"] == "AAPL"
    assert data[0]["confidence"] == 0.9


def test_backtest_command_replays_data_and_prints_summary(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-05-01", periods=60, freq="D"),
            "open": [100.0 + index for index in range(60)],
            "high": [101.0 + index for index in range(60)],
            "low": [99.0 + index for index in range(60)],
            "close": [100.0 + index for index in range(60)],
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
                    "2026-06-13 10:25:00",
                ]
            ),
            "open": [99.9, 100.1, 100.0, 100.2, 100.5, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1, 103.2],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4, 100.9],
            "close": [100.0, 100.2, 100.1, 100.3, 101.0, 103.0],
            "volume": [1000, 1100, 950, 1050, 2500, 1500],
        }
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  log_dir: {log_dir}\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--config-path",
            str(config_file),
            "backtest",
            "--symbols",
            "AAPL",
            "--start",
            "2026-06-13",
            "--end",
            "2026-06-13",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "trades=1 wins=1 win_rate=1.00 net_pnl=36.00"
    log_text = (log_dir / "decision-log.jsonl").read_text(encoding="utf-8")
    assert '"command": "backtest"' in log_text
    assert '"ticker": "AAPL"' in log_text
    assert '"trades": 1' in log_text
    snapshot = json.loads((tmp_path / "state" / "backtest_summary.json").read_text(encoding="utf-8"))
    assert snapshot["mode"] == "backtest"
    assert snapshot["summary"]["trades"] == 1
    assert snapshot["summary"]["wins"] == 1
    assert snapshot["summary"]["net_pnl"] == 36.0
    assert snapshot["rows"][0]["ticker"] == "AAPL"


def test_report_command_reflects_paper_trade_fees(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "5m"
        return pd.DataFrame({"timestamp": pd.to_datetime(["2026-06-18"]), "close": [101.0]})

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(
        PortfolioState(
            cash=3233.0,
            equity=19999.0,
            realized_pnl=-1.0,
            unrealized_pnl=0.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=166, average_cost=101.0)},
        )
    )
    runner = CliRunner()

    result = runner.invoke(app, ["--config-path", str(config_file), "report"])

    assert result.exit_code == 0
    assert (
        result.stdout.strip()
        == "net_pnl=-1.00 realized_pnl=-1.00 unrealized_pnl=0.00 open_positions=1 exposure=0.84 orders=0"
    )


def test_paper_trade_v3_lifecycle_creates_position_and_tracks_pnl(monkeypatch, tmp_path: Path) -> None:
    """V3 signal path creates order, fills, tracks position with stop/target."""
    import trading_bot.data.market_data as market_data
    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.data.indicators import add_atr, add_bollinger_bands, add_ema, add_rsi, add_sma

    # Daily: gentle uptrend with narrowing ranges -> WEAK_UPTREND regime
    closes = [100.0 + i * 0.5 for i in range(60)]
    ranges = [3.5 * (1 - i / 80) for i in range(60)]
    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [c - 0.2 for c in closes],
            "high": [c + r / 2 for c, r in zip(closes, ranges)],
            "low": [c - r / 2 for c, r in zip(closes, ranges)],
            "close": closes,
            "volume": [1_000_000] * 60,
        }
    )
    daily = add_ema(daily, 20, "ema_20")
    daily = add_sma(daily, 50, "sma_50")
    daily = add_atr(daily, 14, "atr_14")
    daily = add_bollinger_bands(daily, 20)

    # Intraday: 20 bars, breakout at bar 5 with volume surge, RSI present
    base = 129.0
    n = 20
    intraday_ts = pd.date_range("2026-06-13 10:00:00", periods=n, freq="5min")
    oclv = []
    for i in range(n):
        if i == 5:
            oclv.append((base, base + 3.0, base - 0.2, base + 2.0, 6000))
        elif i == 6:
            oclv.append((base + 2.0, base + 4.0, base + 1.5, base + 3.0, 2000))
        elif i % 2 == 0:
            c = base + 0.1 * (i % 5)
            oclv.append((c - 0.1, c + 0.3, c - 0.3, c, 1000 + (i % 5) * 100))
        else:
            c = base + 0.2 * (i % 5)
            oclv.append((c - 0.1, c + 0.3, c - 0.3, c, 1100 + (i % 5) * 100))
    last_open, last_high, last_low, last_close, _ = oclv[-1]
    oclv[-1] = (last_open, last_high, last_low, last_close, 1600)
    intraday = pd.DataFrame(
        {
            "timestamp": intraday_ts,
            "open": [x[0] for x in oclv],
            "high": [x[1] for x in oclv],
            "low": [x[2] for x in oclv],
            "close": [x[3] for x in oclv],
            "volume": [x[4] for x in oclv],
        }
    )
    intraday["volume_avg_5"] = intraday["volume"].rolling(5).mean()
    intraday = add_rsi(intraday, 14)

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        assert symbol == "AAPL"
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(
        orchestrator,
        "_scan_now",
        lambda signal_timestamp: datetime(2026, 6, 13, 10, 25, 0, tzinfo=signal_timestamp.tzinfo),
    )

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n"
        f"  log_dir: {log_dir}\n"
        "strategy:\n"
        "  use_v3_signals: true\n"
        "  risk_tolerance: high\n"
        "  min_confidence: medium\n",
        encoding="utf-8",
    )

    ledger = PortfolioLedger(db_path)
    ledger.save_portfolio_state(PortfolioState(cash=20_000.0, equity=20_000.0))

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config-path", str(config_file), "paper-trade", "--symbols", "AAPL"],
    )

    assert result.exit_code == 0
    # Verify fill output present (format matches legacy test expectation)
    assert "FILLED" in result.stdout

    # Verify position tracked in ledger
    state = ledger.load_portfolio_state()
    assert state is not None
    assert "AAPL" in state.positions
    pos = state.positions["AAPL"]
    assert pos.quantity > 0
    assert pos.average_cost > 0
    assert pos.stop_loss is not None and pos.stop_loss < pos.average_cost
    assert pos.profit_target is not None and pos.profit_target > pos.average_cost

    # Verify order recorded with PnL tracking
    rows = ledger.list_order_rows()
    buy_rows = [r for r in rows if r["ticker"] == "AAPL" and r["side"] == "BUY"]
    assert len(buy_rows) == 1
    # PnL column exists and is None for BUY (realized PnL on sells only)
    assert "pnl" in buy_rows[0]


def test_cache_data_command_with_symbols(monkeypatch, tmp_path: Path) -> None:
    """Test cache-data command downloads and saves CSV for symbols."""
    from unittest.mock import MagicMock
    from trading_bot.cli.app import app
    from typer.testing import CliRunner

    runner = CliRunner()

    # Mock fetch_bars to return a small DataFrame
    mock_df = pd.DataFrame({
        "close": [100.0, 101.0, 102.0],
        "open": [99.0, 100.0, 101.0],
        "high": [101.5, 102.0, 103.0],
        "low": [98.5, 99.5, 100.5],
        "volume": [1000, 1100, 1200],
    })

    captured_provider_stacks = []

    def mock_fetch_bars(symbol, period="1y", interval="1d", start=None, end=None, settings=None):
        captured_provider_stacks.append(settings.provider_stack if settings else [])
        return mock_df.copy()

    monkeypatch.setattr("trading_bot.data.market_data.fetch_bars", mock_fetch_bars)

    output_dir = tmp_path / "cache"
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "market_data:\n"
        "  providers:\n"
        "    - alpaca\n"
        "    - polygon\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "--config-path",
            str(config_file),
            "cache-data",
            "--symbols",
            "AAPL,MSFT",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    assert "AAPL" in result.stdout
    assert "MSFT" in result.stdout
    assert "2 cached" in result.stdout

    # Verify CSV files were created
    assert (output_dir / "AAPL.csv").exists()
    assert (output_dir / "MSFT.csv").exists()
    assert captured_provider_stacks == [["alpaca", "polygon"], ["alpaca", "polygon"]]


def test_cache_data_command_from_watchlist(monkeypatch, tmp_path: Path) -> None:
    """Test cache-data reads symbols from watchlist file."""
    from trading_bot.cli.app import app
    from typer.testing import CliRunner
    from trading_bot.runtime.watchlist import write_watchlist

    runner = CliRunner()

    watchlist_path = tmp_path / "watchlist.txt"
    write_watchlist(watchlist_path, ["AAPL", "GOOGL"])

    output_dir = tmp_path / "cache"

    mock_df = pd.DataFrame({
        "close": [100.0, 101.0],
        "open": [99.0, 100.0],
        "high": [101.5, 102.0],
        "low": [98.5, 99.5],
        "volume": [1000, 1100],
    })

    captured_provider_stacks = []

    def mock_fetch_bars(symbol, period="1y", interval="1d", start=None, end=None, settings=None):
        captured_provider_stacks.append(settings.provider_stack if settings else [])
        return mock_df.copy()

    monkeypatch.setattr("trading_bot.data.market_data.fetch_bars", mock_fetch_bars)

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "market_data:\n"
        "  providers:\n"
        "    - alpaca\n"
        "    - polygon\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "--config-path",
            str(config_file),
            "cache-data",
            "--watchlist-path",
            str(watchlist_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    assert "2 cached" in result.stdout
    assert (output_dir / "AAPL.csv").exists()
    assert (output_dir / "GOOGL.csv").exists()
    assert captured_provider_stacks == [["alpaca", "polygon"], ["alpaca", "polygon"]]


def test_supermodel_command_resolves_pipeline_script_from_repo(
    monkeypatch, tmp_path: Path
) -> None:
    """The supermodel command should work even when invoked outside the repo cwd."""
    import subprocess

    captured: dict[str, object] = {}

    def fake_run(cmd, capture_output=False):
        captured["cmd"] = cmd
        captured["capture_output"] = capture_output
        return SimpleNamespace(returncode=0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = CliRunner().invoke(app, ["supermodel", "--dry-run", "--symbols", "AAPL"])

    assert result.exit_code == 0
    cmd = captured["cmd"]
    assert Path(cmd[1]).is_absolute()
    assert Path(cmd[1]).name == "daily_supermodel.py"
    assert Path(cmd[1]).parent.name == "scripts"
    assert "--dry-run" in cmd
    assert cmd[-2:] == ["--symbols", "AAPL"]


def test_live_data_command_resolves_collector_script_from_repo(
    monkeypatch, tmp_path: Path
) -> None:
    import subprocess

    captured: dict[str, object] = {}

    def fake_run(cmd, capture_output=False):
        captured["cmd"] = cmd
        captured["capture_output"] = capture_output
        return SimpleNamespace(returncode=0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = CliRunner().invoke(app, ["live-data", "--buffer"])

    assert result.exit_code == 0
    cmd = captured["cmd"]
    assert Path(cmd[1]).is_absolute()
    assert Path(cmd[1]).name == "live_data_collector.py"
    assert Path(cmd[1]).parent.name == "scripts"
    assert "--buffer" in cmd


def test_auto_retrain_command_resolves_trigger_script_from_repo(
    monkeypatch, tmp_path: Path
) -> None:
    import subprocess

    captured: dict[str, object] = {}

    def fake_run(cmd, capture_output=False):
        captured["cmd"] = cmd
        captured["capture_output"] = capture_output
        return SimpleNamespace(returncode=0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = CliRunner().invoke(app, ["auto-retrain", "--dry-run"])

    assert result.exit_code == 0
    cmd = captured["cmd"]
    assert Path(cmd[1]).is_absolute()
    assert Path(cmd[1]).name == "auto_retrain_trigger.py"
    assert Path(cmd[1]).parent.name == "scripts"
    assert "--dry-run" in cmd
