import json
from pathlib import Path

from trading_bot.reports.summaries import build_daily_summary
from trading_bot.reports.exporters import export_csv, export_json


def test_build_daily_summary_returns_expected_metrics() -> None:
    summary = build_daily_summary(
        realized_pnl=125.5,
        unrealized_pnl=40.0,
        open_positions=2,
    )

    assert summary == {
        "realized_pnl": 125.5,
        "unrealized_pnl": 40.0,
        "open_positions": 2,
        "net_pnl": 165.5,
    }


def test_export_json_writes_expected_payload(tmp_path: Path) -> None:
    path = tmp_path / "summary.json"

    export_json({"net_pnl": 165.5}, path)

    assert json.loads(path.read_text(encoding="utf-8")) == {"net_pnl": 165.5}


def test_export_csv_writes_header_and_rows(tmp_path: Path) -> None:
    path = tmp_path / "summary.csv"

    export_csv(
        [{"ticker": "AAPL", "net_pnl": 12.5}, {"ticker": "MSFT", "net_pnl": 8.0}],
        path,
    )

    assert path.read_text(encoding="utf-8").splitlines() == [
        "ticker,net_pnl",
        "AAPL,12.5",
        "MSFT,8.0",
    ]


def test_export_csv_handles_empty_rows_without_crashing(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"

    export_csv([], path)

    assert path.read_text(encoding="utf-8") == ""
