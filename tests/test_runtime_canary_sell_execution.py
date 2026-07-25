"""Tests for wiring RuntimeCanaryContext through fill_sell_position and the
partial-profit helper. The seam is the single point every SELL record flows
through, so instrumenting it once covers all exit-priority branches
(stop, target, EOD, time, counter-thesis, trailing) as well as partial
profit-taking.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from trading_bot.config.settings import (
    PaperSettings,
    Settings,
    StrategySettings,
    StrategyTrackerSettings,
    SupermodelSettings,
)
from trading_bot.learning.experiments.controller import ExperimentController
from trading_bot.learning.experiments.models import (
    ExperimentState,
    MetricSet,
    ParameterChange,
)
from trading_bot.learning.experiments.store import ExperimentStore


class _Harness:
    """Stand-in harness that captures record_exit calls verbatim."""

    def __init__(self) -> None:
        self.exits: list[dict[str, Any]] = []

    def record_exit(self, **kwargs) -> None:
        self.exits.append(kwargs)

    def record_entry(self, **kwargs) -> None:  # pragma: no cover - unused
        pass

    def candidate_metrics(self):
        return MetricSet(trades=0, profit_factor=0.0, net_pnl=0.0, max_drawdown_pct=0.0)

    def baseline_metrics(self):
        return MetricSet(trades=0, profit_factor=0.0, net_pnl=0.0, max_drawdown_pct=0.0)

    def closed_trade_counts_match(self) -> bool:
        return True


def _settings(state_db_path: Path) -> Settings:
    settings = Settings(
        paper=PaperSettings(),
        supermodel=SupermodelSettings(),
        strategy_tracker=StrategyTrackerSettings(),
        strategy=StrategySettings(use_v3_signals=True),
    )
    settings.app.state_db_path = str(state_db_path)
    return settings


def _build_runtime_canary(
    settings: Settings,
    tmp_path: Path,
    harness: _Harness,
):
    from trading_bot.learning.experiments.runtime_canary import RuntimeCanaryContext

    store = ExperimentStore(root=tmp_path / "experiments")
    state = ExperimentState(
        experiment_id="sell-wiring",
        status="CANARY",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=True,
        baseline_metrics=MetricSet(
            trades=24,
            profit_factor=1.20,
            net_pnl=320.0,
            max_drawdown_pct=2.0,
        ),
    )
    store.save_current(state)
    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )
    return RuntimeCanaryContext(
        state=state,
        controller=controller,
        store=store,
        harness=harness,
        artifacts_dir=tmp_path / "experiments" / "sell-wiring",
        starting_cash=99_000.0,
    )


def test_fill_sell_position_accepts_runtime_canary(tmp_path: Path) -> None:
    """The fill_sell_position signature exposes the runtime_canary kwarg."""
    from trading_bot.runtime.position_exit import fill_sell_position

    # Test the kwarg is accepted; we don't drive a real sell here because
    # that requires broker/ledger dance. The plumbing contract is what
    # the test guards.
    import inspect

    sig = inspect.signature(fill_sell_position)
    assert "runtime_canary" in sig.parameters


def test_fill_partial_take_profit_position_accepts_runtime_canary(
    tmp_path: Path,
) -> None:
    """The partial-profit helper accepts and forwards runtime_canary."""
    from trading_bot.runtime.position_exit import fill_partial_take_profit_position

    import inspect

    sig = inspect.signature(fill_partial_take_profit_position)
    assert "runtime_canary" in sig.parameters


def test_position_exit_instruments_record_exit(tmp_path: Path) -> None:
    """A successful SELL records an exit into the harness."""
    from trading_bot.execution.paper_broker import PaperBroker
    from trading_bot.models.order import OrderRequest
    from trading_bot.models.portfolio import PortfolioState, Position
    from trading_bot.portfolio.ledger import PortfolioLedger
    from trading_bot.runtime.position_exit import fill_sell_position

    state_db = tmp_path / "state.db"
    log_path = tmp_path / "decision-log.jsonl"
    settings = _settings(state_db)
    settings.paper.fee_per_order = 1.0

    store = ExperimentStore(root=tmp_path / "experiments")
    state = ExperimentState(
        experiment_id="sell-wiring",
        status="CANARY",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=True,
        baseline_metrics=MetricSet(
            trades=24,
            profit_factor=1.20,
            net_pnl=320.0,
            max_drawdown_pct=2.0,
        ),
    )
    store.save_current(state)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    ledger = PortfolioLedger(state_db)
    pos = Position(
        ticker="AAPL",
        quantity=10,
        average_cost=100.0,
        entry_fees=1.0,
    )
    ledger.save_portfolio_state(
        PortfolioState(
            cash=99_000.0,
            equity=99_000.0,
            positions={"AAPL": pos},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
        )
    )
    state_obj = ledger.ensure_portfolio_state()

    # Populate the SQL ORM so _sell_sql_persist can find the trade/position
    from trading_bot.db.session import init_db, make_session_factory, get_session
    from trading_bot.db.models import Trade, Position as ORMPosition
    from trading_bot.db.repositories.trades import upsert_trade
    from trading_bot.db.repositories.positions import upsert_position
    from datetime import datetime as _dt

    engine = init_db(settings)
    session_factory = make_session_factory(engine)
    session = get_session(session_factory)
    try:
        upsert_trade(
            session,
            ticker="AAPL",
            side="BUY",
            order_type="market",
            quantity=10,
            entry_price=100.0,
            strategy_tag="v3-trend_following",
        )
        upsert_position(
            session,
            ticker="AAPL",
            quantity=10,
            average_cost=100.0,
            stop_loss=95.0,
            profit_target=110.0,
            strategy_tag="v3-trend_following",
        )
    finally:
        session.close()
        engine.dispose()

    broker = PaperBroker(
        starting_cash=state_obj.cash,
        fee_per_order=settings.paper.fee_per_order,
        slippage_bps=settings.paper.slippage_bps,
        dynamic_slippage_enabled=False,
        dynamic_slippage_notional_bps_per_10k=0.0,
        dynamic_slippage_low_price_boost_bps=0.0,
        dynamic_slippage_max_extra_bps=0.0,
    )
    # The fill_sell_position helper submits its own SELL order, so the
    # broker must hold enough quantity to satisfy both that order and the
    # later assertions. Set positions to 10 here; the helper's SELL
    # reduces it to zero.
    broker.positions = {"AAPL": 10}
    broker.position_costs = {"AAPL": 100.0}

    harness = _Harness()
    from trading_bot.learning.experiments.runtime_canary import RuntimeCanaryContext

    ctx = RuntimeCanaryContext(
        state=state,
        controller=controller,
        store=store,
        harness=harness,
        artifacts_dir=tmp_path / "experiments" / "sell-wiring",
        starting_cash=99_000.0,
    )

    new_state, event, line = fill_sell_position(
        ticker="AAPL",
        position=pos,
        reason="stop_loss",
        submitted_at=datetime.now(timezone.utc),
        last_price=110.0,
        broker=broker,
        ledger=ledger,
        state=state_obj,
        log_path=log_path,
        runtime_canary=ctx,
    )

    assert harness.exits, "expected at least one record_exit call"
    last = harness.exits[-1]
    assert last["ticker"] == "AAPL"
    assert last["candidate_quantity"] == 10
    assert last["fill_price"] == 110.0


def test_failed_sell_transaction_does_not_record_shadow(tmp_path: Path) -> None:
    from trading_bot.execution.paper_broker import PaperBroker
    from trading_bot.models.portfolio import PortfolioState, Position
    from trading_bot.runtime.fill_transaction import FillTransactionError
    from trading_bot.runtime.position_exit import fill_sell_position

    class FailingLedger:
        def __init__(self) -> None:
            self.canary_experiment_id: str | None = None

        def record_fill(
            self,
            fill,
            side,
            realized_pnl=0.0,
            strategy_tag="",
            canary_experiment_id=None,
            canary_baseline_quantity=None,
        ) -> None:
            self.canary_experiment_id = canary_experiment_id
            raise RuntimeError("db failed")

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    position = Position(
        ticker="AAPL",
        quantity=10,
        average_cost=100.0,
        entry_fees=1.0,
    )
    state = PortfolioState(
        cash=99_000.0,
        equity=100_000.0,
        positions={"AAPL": position},
    )
    broker = PaperBroker(
        starting_cash=state.cash,
        fee_per_order=settings.paper.fee_per_order,
        slippage_bps=0,
        dynamic_slippage_enabled=False,
        dynamic_slippage_notional_bps_per_10k=0.0,
        dynamic_slippage_low_price_boost_bps=0.0,
        dynamic_slippage_max_extra_bps=0.0,
    )
    broker.positions = {"AAPL": 10}
    broker.position_costs = {"AAPL": 100.0}
    ledger = FailingLedger()
    harness = _Harness()
    ctx = _build_runtime_canary(settings, tmp_path, harness)

    with pytest.raises(FillTransactionError):
        fill_sell_position(
            ticker="AAPL",
            position=position,
            reason="stop_loss",
            submitted_at=datetime.now(timezone.utc),
            last_price=110.0,
            broker=broker,
            ledger=ledger,
            state=state,
            log_path=tmp_path / "decision-log.jsonl",
            settings=settings,
            runtime_canary=ctx,
        )

    assert harness.exits == []
    assert ledger.canary_experiment_id == "sell-wiring"


def test_successful_sell_records_canary_experiment_id(tmp_path: Path) -> None:
    from trading_bot.execution.paper_broker import PaperBroker
    from trading_bot.models.portfolio import PortfolioState, Position
    from trading_bot.portfolio.ledger import PortfolioLedger
    from trading_bot.runtime.position_exit import fill_sell_position

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    ledger = PortfolioLedger(state_db)
    position = Position(
        ticker="AAPL",
        quantity=10,
        average_cost=100.0,
        entry_fees=1.0,
    )
    ledger.save_portfolio_state(
        PortfolioState(
            cash=99_000.0,
            equity=100_000.0,
            positions={"AAPL": position},
        )
    )
    state = ledger.ensure_portfolio_state()
    broker = PaperBroker(
        starting_cash=state.cash,
        fee_per_order=settings.paper.fee_per_order,
        slippage_bps=0,
        dynamic_slippage_enabled=False,
        dynamic_slippage_notional_bps_per_10k=0.0,
        dynamic_slippage_low_price_boost_bps=0.0,
        dynamic_slippage_max_extra_bps=0.0,
    )
    broker.positions = {"AAPL": 10}
    broker.position_costs = {"AAPL": 100.0}
    harness = _Harness()
    ctx = _build_runtime_canary(settings, tmp_path, harness)

    fill_sell_position(
        ticker="AAPL",
        position=position,
        reason="stop_loss",
        submitted_at=datetime.now(timezone.utc),
        last_price=110.0,
        broker=broker,
        ledger=ledger,
        state=state,
        log_path=tmp_path / "decision-log.jsonl",
        settings=settings,
        runtime_canary=ctx,
    )

    with sqlite3.connect(state_db) as connection:
        row = connection.execute(
            """
            SELECT id, canary_experiment_id, canary_baseline_quantity
            FROM orders
            WHERE side = 'SELL'
            """
        ).fetchone()
    assert row is not None
    assert row[1:] == ("sell-wiring", None)
    assert harness.exits[-1]["operation_id"] == row[0]
