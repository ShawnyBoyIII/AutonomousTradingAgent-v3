from datetime import datetime
from pathlib import Path

from trading_bot.runtime.decision_log import append_decision_event


def test_append_decision_event_writes_jsonl(tmp_path: Path) -> None:
    log_path = tmp_path / "decision-log.jsonl"

    append_decision_event(log_path, {"event": "signal_rejected", "ticker": "AAPL"})

    assert log_path.exists()
    assert log_path.read_text(encoding="utf-8") == (
        '{"event": "signal_rejected", "ticker": "AAPL"}\n'
    )


def test_append_decision_event_appends_and_serializes_datetimes(tmp_path: Path) -> None:
    log_path = tmp_path / "decision-log.jsonl"

    append_decision_event(
        log_path,
        {"event": "fill", "filled_at": datetime(2026, 6, 13, 10, 0, 0)},
    )
    append_decision_event(log_path, {"event": "risk_rejected", "ticker": "AAPL"})

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"event": "fill"' in lines[0]
    assert "2026-06-13 10:00:00" in lines[0]
    assert '"event": "risk_rejected"' in lines[1]
