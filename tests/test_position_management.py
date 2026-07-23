"""Regression tests for the shared position-management evaluator.

After consolidation, the CLI manage-positions command and the
continuous loop's _run_manage_positions_once should produce the
same canonical exit reasons for the same conditions. Previously
the CLI wrote short forms ("eod" / "stop" / "target") and the
continuous loop wrote long forms ("eod_exit" / "stop_loss" /
"profit_target") — the dashboard and audit logs could not reconcile
the two streams.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from trading_bot.config.settings import (
    AppSettings,
    PaperSettings,
    SessionSettings,
    Settings,
    StrategySettings,
)
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.runtime.position_management import (
    NO_EXIT,
    ExitDecision,
    evaluate_exit_priority,
)


class _BrokerStub:
    cash = 10_000.0
    positions: dict[str, int] = {}
    fee_per_order = 1.0

    def submit_order(self, order, market_price: float):
        from datetime import datetime, timezone
        from trading_bot.models.order import FillResult

        return FillResult(
            order_id="stub",
            ticker=order.ticker,
            quantity=order.quantity,
            fill_price=market_price,
            fees=self.fee_per_order,
            filled_at=datetime.now(timezone.utc),
        )


class _LedgerStub:
    def record_fill(self, *args, **kwargs): pass
    def save_portfolio_state(self, *args, **kwargs): pass
    def record_equity_snapshot(self, *args, **kwargs): pass


def _settings() -> Settings:
    return Settings(
        app=AppSettings(
            log_dir="/tmp/phase2-test",
            state_db_path="/tmp/phase2-test.db",
        ),
        paper=PaperSettings(
            partial_take_profit_enabled=False,
            partial_take_profit_min_qty=10,
            partial_take_profit_fraction=0.5,
        ),
        session=SessionSettings(time_exit_minutes=0),
        strategy=StrategySettings(),
    )


def _position(
    quantity: int = 10,
    average_cost: float = 100.0,
    stop_loss: float | None = 95.0,
    profit_target: float | None = 110.0,
    entry_at: datetime | None = None,
    partial_profit_taken: bool = False,
) -> Position:
    return Position(
        ticker="AAPL",
        quantity=quantity,
        average_cost=average_cost,
        stop_loss=stop_loss,
        profit_target=profit_target,
        entry_at=entry_at or datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc),
        strategy_tag="v3-trend_following",
        partial_profit_taken=partial_profit_taken,
    )


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame()


def test_no_exit_when_no_priority_fires(monkeypatch) -> None:
    """When no priority fires, the evaluator must return NO_EXIT.
    To avoid EOD-window flake, monkeypatch now_in_zone to mid-day."""
    from trading_bot.runtime import session as session_mod

    monkeypatch.setattr(
        session_mod,
        "now_in_zone",
        lambda tz: datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc),
    )
    settings = _settings()
    pos = _position()
    state = PortfolioState(cash=10_000.0, equity=10_000.0, positions={"AAPL": pos})
    exit_events: list[dict] = []
    line_parts: list[str] = []

    new_state, decision = evaluate_exit_priority(
        ticker="AAPL",
        position=pos,
        current_price=100.0,
        intraday_frame=_empty_frame(),
        settings=settings,
        now=datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc),
        broker=_BrokerStub(),
        ledger=_LedgerStub(),
        state=state,
        log_path=Path("/tmp/x"),
        exit_events=exit_events,
        line_parts=line_parts,
    )

    assert decision is NO_EXIT
    assert not decision.should_exit
    assert exit_events == []


def test_stop_loss_canonical_reason(monkeypatch) -> None:
    """stop_loss exit must use the canonical long form, not the legacy
    short form ("stop").
    """
    from trading_bot.runtime import session as session_mod

    monkeypatch.setattr(
        session_mod,
        "now_in_zone",
        lambda tz: datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc),
    )
    settings = _settings()
    pos = _position(stop_loss=95.0)
    state = PortfolioState(cash=10_000.0, equity=10_000.0, positions={"AAPL": pos})
    exit_events: list[dict] = []
    line_parts: list[str] = []

    new_state, decision = evaluate_exit_priority(
        ticker="AAPL",
        position=pos,
        current_price=94.0,
        intraday_frame=_empty_frame(),
        settings=settings,
        now=datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc),
        broker=_BrokerStub(),
        ledger=_LedgerStub(),
        state=state,
        log_path=Path("/tmp/x"),
        exit_events=exit_events,
        line_parts=line_parts,
    )

    assert decision.reason == "stop_loss"


def test_profit_target_canonical_reason(monkeypatch) -> None:
    from trading_bot.runtime import session as session_mod

    monkeypatch.setattr(
        session_mod,
        "now_in_zone",
        lambda tz: datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc),
    )
    settings = _settings()
    pos = _position(profit_target=110.0)
    state = PortfolioState(cash=10_000.0, equity=10_000.0, positions={"AAPL": pos})
    exit_events: list[dict] = []
    line_parts: list[str] = []

    new_state, decision = evaluate_exit_priority(
        ticker="AAPL",
        position=pos,
        current_price=111.0,
        intraday_frame=_empty_frame(),
        settings=settings,
        now=datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc),
        broker=_BrokerStub(),
        ledger=_LedgerStub(),
        state=state,
        log_path=Path("/tmp/x"),
        exit_events=exit_events,
        line_parts=line_parts,
    )

    assert decision.reason == "profit_target"


def test_time_exit_canonical_reason(monkeypatch) -> None:
    """time_exit reason must include the held-minute count."""
    from trading_bot.runtime import session as session_mod

    monkeypatch.setattr(
        session_mod,
        "now_in_zone",
        lambda tz: datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc),
    )
    settings = _settings()
    settings.session.time_exit_minutes = 30
    entry_at = datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc)
    pos = _position(
        stop_loss=None,
        profit_target=None,
        entry_at=entry_at,
    )
    state = PortfolioState(cash=10_000.0, equity=10_000.0, positions={"AAPL": pos})
    exit_events: list[dict] = []
    line_parts: list[str] = []

    new_state, decision = evaluate_exit_priority(
        ticker="AAPL",
        position=pos,
        current_price=100.0,
        intraday_frame=_empty_frame(),
        settings=settings,
        now=datetime(2026, 7, 22, 13, 45, tzinfo=timezone.utc),
        broker=_BrokerStub(),
        ledger=_LedgerStub(),
        state=state,
        log_path=Path("/tmp/x"),
        exit_events=exit_events,
        line_parts=line_parts,
    )

    assert decision.reason.startswith("time_exit_")
    assert "m" in decision.reason
