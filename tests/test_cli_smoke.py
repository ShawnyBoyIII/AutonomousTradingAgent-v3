import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from typer.testing import CliRunner

from trading_bot.cli.app import app
from trading_bot.main import main
from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.portfolio.ledger import PortfolioLedger


def test_cli_shows_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["tradebot", "--help"])

    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()
    assert "scan" in captured.out
    assert "paper-trade" in captured.out


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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
        == "AAPL APPROVED quality=GREEN status=fresh age=5m ts=2026-06-13T10:20:00 last=101.00 qty=166 rr=2.00 conf=0.90 risk=$199.20 alloc=0.84 entry=101.00 stop=99.80 target=103.40 reasons=bullish daily regime; intraday breakout"
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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
        ["--config-path", str(config_file), "scan", "--symbols", "MSFT,AAPL"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == [
        "AAPL APPROVED quality=GREEN status=fresh age=5m ts=2026-06-13T10:20:00 last=101.00 qty=166 rr=2.00 conf=0.90 risk=$199.20 alloc=0.84 entry=101.00 stop=99.80 target=103.40 reasons=bullish daily regime; intraday breakout",
        "MSFT APPROVED quality=GREEN status=fresh age=5m ts=2026-06-13T10:20:00 last=201.00 qty=166 rr=2.00 conf=0.80 risk=$199.20 alloc=1.67 entry=201.00 stop=199.80 target=203.40 reasons=bullish daily regime; intraday breakout",
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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
    assert "status=stale age=40m ts=2026-06-13T10:20:00" in result.stdout
    snapshot = json.loads((tmp_path / "state" / "scan_results.json").read_text(encoding="utf-8"))
    assert snapshot["candidates"][0]["freshness"] == "stale"


def test_scan_command_writes_empty_snapshot_for_no_signal(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 for _ in range(60)],
            "high": [101.0 for _ in range(60)],
            "low": [99.0 for _ in range(60)],
            "close": [100.0 for _ in range(60)],
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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
        }
    ]


def test_scan_command_prints_no_signal_reason(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [100.0 for _ in range(60)],
            "high": [101.0 for _ in range(60)],
            "low": [99.0 for _ in range(60)],
            "close": [98.0 for _ in range(60)],
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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
            "low": [99.0 for _ in range(60)],
            "close": [98.0 for _ in range(60)],
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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
        "daily_close=98.00 ema_20=98.00 sma_50=98.00 "
        "intraday_close=101.00 range_high=100.40 volume=2500 volume_avg=1320.00 volume_ratio=1.89"
    )
    snapshot = json.loads((tmp_path / "state" / "scan_results.json").read_text(encoding="utf-8"))
    assert snapshot["candidates"][0]["details"] == {
        "daily_close": 98.0,
        "ema_20": 98.0,
        "sma_50": 98.0,
        "intraday_close": 101.0,
        "range_high": 100.4,
        "volume": 2500,
        "volume_avg": 1320.0,
        "volume_ratio": 1.89,
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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
    assert "ts=2026-06-13T10:20:00" in result.stdout
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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
    assert "ts=2026-06-13T10:20:00" in result.stdout
    assert "intraday_close=101.00" in result.stdout
    assert "volume=2500" in result.stdout
    assert "intraday_close=100.70" not in result.stdout


def test_portfolio_command_prints_saved_summary(monkeypatch, tmp_path: Path) -> None:
    import trading_bot.data.market_data as market_data

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "1d"
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-06-13"]),
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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
    assert result.stdout.strip() == "AAPL FILLED qty=166 price=101.00 cash=3233.00"

    state = ledger.load_portfolio_state()
    assert state is not None
    assert state.cash == 3233.0
    assert state.equity == 19999.0
    assert state.positions["AAPL"].quantity == 166
    assert state.positions["AAPL"].average_cost == 101.0

    rows = ledger.list_order_rows()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["side"] == "BUY"

    log_text = (log_dir / "decision-log.jsonl").read_text(encoding="utf-8")
    assert '"command": "paper-trade"' in log_text
    assert '"status": "FILLED"' in log_text


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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
    assert result.stdout.strip() == "AAPL REJECTED stale market data"


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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
        assert symbol == "AAPL"
        assert interval == "1d"
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
        "id,ticker,side,quantity,fill_price,fees,filled_at",
        "order-1,AAPL,BUY,5,100.0,1.0,2026-06-13T10:00:00",
    ]
    dashboard_snapshot = json.loads(
        (tmp_path / "state" / "dashboard_summary.json").read_text(encoding="utf-8")
    )
    assert dashboard_snapshot["mode"] == "report"
    assert dashboard_snapshot["summary"]["net_pnl"] == 175.5
    assert dashboard_snapshot["rows"][0]["ticker"] == "AAPL"


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

    def fake_fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
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
    assert result.stdout.strip() == "trades=1 wins=1 win_rate=1.00 net_pnl=164.00"
    log_text = (log_dir / "decision-log.jsonl").read_text(encoding="utf-8")
    assert '"command": "backtest"' in log_text
    assert '"ticker": "AAPL"' in log_text
    assert '"trades": 1' in log_text
    snapshot = json.loads((tmp_path / "state" / "backtest_summary.json").read_text(encoding="utf-8"))
    assert snapshot["mode"] == "backtest"
    assert snapshot["summary"]["trades"] == 1
    assert snapshot["summary"]["wins"] == 1
    assert snapshot["summary"]["net_pnl"] == 164.0
    assert snapshot["rows"][0]["ticker"] == "AAPL"


def test_report_command_reflects_paper_trade_fees(tmp_path: Path) -> None:
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
