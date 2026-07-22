"""TDD: paper-trade consumes ApprovedCandidate when available."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trading_bot.config.settings import AppSettings, Settings
from trading_bot.runtime.approved_candidate import (
    ApprovedCandidate,
    write_candidates_jsonl,
)
from trading_bot.runtime.orchestrator import run_paper_trade


def _settings(tmp_path: Path) -> Settings:
    app = AppSettings(state_db_path=str(tmp_path / "ledger.db"), log_dir=str(tmp_path / "logs"))
    return Settings(app=app)


def _candidate(tmp_path: Path, ticker: str = "AAPL") -> Path:
    cand = ApprovedCandidate(
        ticker=ticker,
        quality="GREEN",
        timestamp="2026-07-21T13:30:00+00:00",
        entry=190.50,
        stop=187.20,
        target=196.30,
        qty=10,
        rr=1.6,
        confidence=0.78,
        risk=33.0,
        allocation=0.01,
        strategy="v3-trend_following",
        supermodel_decision="caution",
        scan_id="20260721T133000",
    )
    return write_candidates_jsonl([cand], tmp_path / "approved.jsonl")


def test_run_paper_trade_consumes_approved_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When an ApprovedCandidate exists, paper-trade uses it without re-fetching."""
    settings = _settings(tmp_path)
    cand_path = _candidate(tmp_path)
    settings.app.approved_candidates_path = str(cand_path)

    fetched: list[str] = []

    def fake_fetch_bars(*args: Any, **kwargs: Any) -> Any:
        fetched.append(args[0] if args else kwargs.get("symbol", ""))
        import pandas as pd

        idx = pd.date_range("2026-07-21 09:30", periods=200, freq="5min")
        return pd.DataFrame(
            {
                "open": [190.0] * 200,
                "high": [192.0] * 200,
                "low": [189.0] * 200,
                "close": [190.5] * 200,
                "volume": [1_000_000] * 200,
            },
            index=idx,
        )

    monkeypatch.setattr(
        "trading_bot.data.market_data.fetch_bars", fake_fetch_bars
    )
    monkeypatch.setattr(
        "trading_bot.runtime.orchestrator._persist_scan_results_to_db",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "trading_bot.runtime.orchestrator._persist_trade_to_db",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
        lambda *a, **kw: (True, ""),
    )
    monkeypatch.setattr(
        "trading_bot.safety.circuit_breaker.check_circuit_breakers",
        lambda *a, **kw: (True, ""),
    )

    results = run_paper_trade(["AAPL"], settings, dry_run=True)
    assert any("AAPL" in line for line in results)


def test_run_paper_trade_rejects_when_no_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No matching candidate row -> paper-trade does not consult candidate contract."""
    settings = _settings(tmp_path)
    cand_path = tmp_path / "approved.jsonl"
    cand_path.write_text("", encoding="utf-8")
    settings.app.approved_candidates_path = str(cand_path)

    # Per-symbol contract: no candidate row -> legacy path runs (not a rejection).
    # Patch fetch_bars to a benign stub so legacy path completes.
    import pandas as pd
    monkeypatch.setattr(
        "trading_bot.data.market_data.fetch_bars",
        lambda *a, **kw: pd.DataFrame(
            {"open": [190.0] * 50, "high": [192.0] * 50, "low": [189.0] * 50,
             "close": [190.5] * 50, "volume": [1_000_000] * 50},
            index=pd.date_range("2026-07-21 09:30", periods=50, freq="5min"),
        ),
    )
    monkeypatch.setattr(
        "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
        lambda *a, **kw: (True, ""),
    )
    monkeypatch.setattr(
        "trading_bot.safety.circuit_breaker.check_circuit_breakers",
        lambda *a, **kw: (True, ""),
    )
    results = run_paper_trade(["AAPL"], settings, dry_run=True)
    assert not any("no_approved_candidate" in line for line in results)


def test_run_paper_trade_falls_back_when_no_candidate_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No candidate file at all -> legacy scan-and-execute path runs."""
    settings = _settings(tmp_path)
    settings.app.approved_candidates_path = str(tmp_path / "missing.jsonl")
    import pandas as pd

    def fake_fetch(*a: Any, **kw: Any) -> Any:
        idx = pd.date_range("2026-07-21 09:30", periods=200, freq="5min")
        return pd.DataFrame(
            {
                "open": [190.0] * 200,
                "high": [192.0] * 200,
                "low": [189.0] * 200,
                "close": [190.5] * 200,
                "volume": [1_000_000] * 200,
            },
            index=idx,
        )

    monkeypatch.setattr("trading_bot.data.market_data.fetch_bars", fake_fetch)
    monkeypatch.setattr(
        "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
        lambda *a, **kw: (True, ""),
    )
    monkeypatch.setattr(
        "trading_bot.safety.circuit_breaker.check_circuit_breakers",
        lambda *a, **kw: (True, ""),
    )
    results = run_paper_trade(["AAPL"], settings, dry_run=True)
    assert not any("no_approved_candidate" in line for line in results)


def test_run_paper_trade_rejects_when_candidate_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Candidate older than max_age_minutes is rejected before fetch."""
    settings = _settings(tmp_path)
    cand = ApprovedCandidate(
        ticker="AAPL",
        quality="GREEN",
        timestamp="2020-01-01T00:00:00+00:00",  # very old
        entry=190.0,
        stop=187.0,
        target=196.0,
        qty=10,
        rr=1.5,
        confidence=0.7,
        risk=30.0,
        allocation=0.01,
        strategy="v3-trend_following",
        supermodel_decision="caution",
        scan_id="old",
    )
    cand_path = write_candidates_jsonl([cand], tmp_path / "approved.jsonl")
    settings.app.approved_candidates_path = str(cand_path)

    monkeypatch.setattr(
        "trading_bot.data.market_data.fetch_bars",
        MagicMock(side_effect=AssertionError("should not fetch")),
    )
    results = run_paper_trade(["AAPL"], settings, dry_run=True)
    assert any("REJECTED" in line and "candidate_stale" in line for line in results)
