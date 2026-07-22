"""TDD: dashboard resolves starting equity from cohort, not hardcoded."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


class _DummySettings:
    def __init__(
        self,
        graduation_since=None,
        equity_evaluation_since=None,
        starting_cash=None,
    ):
        self.paper = _PaperSettings(graduation_since, equity_evaluation_since, starting_cash)
        self.app = _AppSettings(starting_cash)


class _PaperSettings:
    def __init__(self, graduation_since, equity_evaluation_since, starting_cash):
        self.graduation_since = graduation_since
        self.equity_evaluation_since = equity_evaluation_since
        self.starting_cash = starting_cash


class _AppSettings:
    def __init__(self, starting_cash):
        self.starting_cash = starting_cash


class _DummyLedger:
    def __init__(self, history):
        self._history = history

    def list_recent_equity_history(self, *, limit=None, since=None):
        return self._history


class _DummyState:
    def __init__(self, settings, ledger):
        self.settings = settings
        self.ledger = ledger


class _DummyPState:
    def __init__(self, equity):
        self.equity = equity


def _patch_dashboard_state(monkeypatch, settings, ledger):
    from ui.dashboard import main

    monkeypatch.setattr(main, "state", _DummyState(settings, ledger))


def test_resolve_starting_equity_uses_oldest_cohort_snapshot(monkeypatch):
    from ui.dashboard import main

    settings = _DummySettings(
        graduation_since=datetime(2026, 7, 11, tzinfo=timezone.utc)
    )
    ledger = _DummyLedger(
        [
            {"timestamp": "2026-07-11T00:00:00+00:00", "equity": 100_000.0},
            {"timestamp": "2026-07-15T00:00:00+00:00", "equity": 99_500.0},
        ]
    )
    _patch_dashboard_state(monkeypatch, settings, ledger)

    pstate = _DummyPState(equity=50_000.0)
    assert main._resolve_starting_equity(pstate) == 100_000.0


def test_resolve_starting_equity_falls_back_to_equity_when_no_history(monkeypatch):
    from ui.dashboard import main

    settings = _DummySettings()
    ledger = _DummyLedger([])
    _patch_dashboard_state(monkeypatch, settings, ledger)

    pstate = _DummyPState(equity=100_000.0)
    assert main._resolve_starting_equity(pstate) == 100_000.0


def test_resolve_starting_equity_uses_configured_starting_cash(monkeypatch):
    from ui.dashboard import main

    settings = _DummySettings(starting_cash=100_000.0)
    ledger = _DummyLedger([])
    _patch_dashboard_state(monkeypatch, settings, ledger)

    pstate = _DummyPState(equity=50_000.0)
    assert main._resolve_starting_equity(pstate) == 100_000.0

