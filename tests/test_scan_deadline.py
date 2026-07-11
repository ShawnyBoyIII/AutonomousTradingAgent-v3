"""Tests for the wall-clock scan deadline (2026-07-09 incident).

On 2026-07-09 the burn-in's main loop hung for 7+ hours at the first scan
step because the Polygon call chain had no global deadline.  This module
verifies the deadline cap added to ``run_scan``:

1.  When the per-symbol work exceeds the deadline, ``run_scan`` returns
    a result with ``summary.deadline_exceeded=True`` and lists the
    symbols that were skipped.
2.  A ``DEADLINE_EXCEEDED`` decision event is appended to decision-log.
3.  When the loop finishes within budget, the deadline is reported as
    not exceeded.
4.  The deadline is configurable via ``market_data.scan_deadline_minutes``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from trading_bot.config.settings import Settings
from trading_bot.runtime import orchestrator


def _settings(tmp_path: Path, deadline_minutes: int = 1) -> Settings:
    """Build minimal Settings with a 1-minute scan deadline."""
    return Settings(
        app={
            "state_db_path": str(tmp_path / "state.db"),
            "log_dir": str(tmp_path / "logs"),
            "scan_results_path": str(tmp_path / "scan_results.json"),
        },
        market_data={"scan_deadline_minutes": deadline_minutes},
    )


def test_run_scan_completes_within_deadline(tmp_path: Path, monkeypatch) -> None:
    """When the loop finishes within the deadline, deadline_exceeded is False."""
    settings = _settings(tmp_path, deadline_minutes=5)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    def fast_build(symbol, _settings):
        return (None, "no_signal", {"quality": "RED"})

    monkeypatch.setattr(orchestrator, "_build_signal_result", fast_build)
    monkeypatch.setattr(orchestrator, "_evaluate_counter_thesis_for_signal", lambda *a, **k: None)

    result = orchestrator.run_scan(["AAPL", "MSFT"], settings)
    summary = result["summary"]
    assert summary["deadline_exceeded"] is False
    assert summary["deadline_skipped_count"] == 0
    assert summary["deadline_skipped_symbols"] == []
    assert summary["elapsed_seconds"] >= 0
    assert summary["deadline_minutes"] == 5


def test_run_scan_exits_at_deadline(tmp_path: Path, monkeypatch) -> None:
    """When per-symbol work exceeds the deadline, scan returns deadline_exceeded=True."""
    # 0-minute deadline (rounded up to 1 second by the loop check) is awkward;
    # use 1 minute deadline but inject 90 seconds of work per symbol so the
    # second iteration trips the deadline.
    settings = _settings(tmp_path, deadline_minutes=1)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    call_count = {"value": 0}

    def slow_build(symbol, _settings):
        # First call: take 65s to exceed the 1-minute budget.  Subsequent
        # calls should be skipped because the deadline check fires before
        # the next iteration begins.
        if call_count["value"] == 0:
            call_count["value"] += 1
            # Sleep a long time but bound it so the test doesn't actually
            # wait that long — instead, fast-forward by patching time.
            time.sleep(0.05)  # placeholder; real slowness is mocked below
        return (None, "no_signal", {"quality": "RED"})

    # Mock time.monotonic so we control the deadline clock
    fake_now = {"value": 0.0}

    def fake_monotonic():
        return fake_now["value"]

    monkeypatch.setattr(orchestrator.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(orchestrator, "_build_signal_result", slow_build)
    monkeypatch.setattr(orchestrator, "_evaluate_counter_thesis_for_signal", lambda *a, **k: None)

    # Walk through the loop manually:
    # - call #1: t=0, deadline=60s.  After call, advance fake clock to 70s.
    # - iteration 2: t=70s, deadline exceeded.  Loop breaks.
    fake_now["value"] = 0.0
    # First symbol processes (no actual waiting because slow_build is fast)
    # Then we bump the clock to simulate the elapsed wall time
    real_run_scan = orchestrator.run_scan

    # We can't easily inject "advance clock mid-run" without rewriting the
    # loop.  Instead, mock the function so each call advances the clock
    # to push past the deadline.

    def advancing_build(symbol, _settings):
        # Each call: bump the clock by 90s to push past the 60s deadline
        fake_now["value"] += 90
        return (None, "no_signal", {"quality": "RED"})

    monkeypatch.setattr(orchestrator, "_build_signal_result", advancing_build)

    result = orchestrator.run_scan(["AAPL", "MSFT", "GOOG", "AMZN"], settings)
    summary = result["summary"]

    assert summary["deadline_exceeded"] is True
    # The first call processes; the deadline check at the start of the
    # second iteration trips because fake_now has advanced by 90s.
    assert summary["deadline_skipped_count"] >= 1
    assert len(summary["deadline_skipped_symbols"]) >= 1
    # At least one of the unprocessed tickers is in the skipped list
    assert any(s in {"MSFT", "GOOG", "AMZN"} for s in summary["deadline_skipped_symbols"])


def test_deadline_exceeded_writes_decision_event(tmp_path: Path, monkeypatch) -> None:
    """A DEADLINE_EXCEEDED event is appended to logs/decision-log.jsonl."""
    settings = _settings(tmp_path, deadline_minutes=1)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "decision-log.jsonl"

    fake_now = {"value": 0.0}

    def fake_monotonic():
        return fake_now["value"]

    def advancing_build(symbol, _settings):
        fake_now["value"] += 90
        return (None, "no_signal", {"quality": "RED"})

    monkeypatch.setattr(orchestrator.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(orchestrator, "_build_signal_result", advancing_build)
    monkeypatch.setattr(orchestrator, "_evaluate_counter_thesis_for_signal", lambda *a, **k: None)

    orchestrator.run_scan(["AAPL", "MSFT"], settings)

    # Read back the decision log
    assert log_path.exists()
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    deadline_events = [e for e in events if e.get("status") == "DEADLINE_EXCEEDED"]
    assert len(deadline_events) == 1
    assert deadline_events[0]["command"] == "scan"
    assert deadline_events[0]["deadline_minutes"] == 1
    assert deadline_events[0]["elapsed_seconds"] >= 60
    assert "skipped_symbols" in deadline_events[0]


def test_deadline_minutes_default_value() -> None:
    """Default deadline is 5 minutes (matches the burn-in-config.yaml setting)."""
    settings = Settings()
    assert settings.market_data.scan_deadline_minutes == 5


def test_deadline_minutes_validator_rejects_zero() -> None:
    """scan_deadline_minutes must be >= 1 to keep the loop sane."""
    import pydantic

    settings_kwargs = {
        "market_data": {"scan_deadline_minutes": 0},
    }
    raised = False
    try:
        Settings(**settings_kwargs)
    except pydantic.ValidationError:
        raised = True
    assert raised, "scan_deadline_minutes=0 should be rejected by Field(ge=1)"
