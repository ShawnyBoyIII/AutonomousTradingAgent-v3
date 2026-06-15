from pathlib import Path

from trading_bot.runtime.decision_log import append_decision_event


def test_append_decision_event_writes_jsonl(tmp_path: Path) -> None:
    log_path = tmp_path / "decision-log.jsonl"

    append_decision_event(log_path, {"event": "signal_rejected", "ticker": "AAPL"})

    assert log_path.exists()
    assert log_path.read_text(encoding="utf-8") == (
        '{"event": "signal_rejected", "ticker": "AAPL"}\n'
    )
