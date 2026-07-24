"""Stage 4c: EngineDriver — orchestrating the hybrid pipeline."""
from __future__ import annotations

import signal
from datetime import datetime, timezone
from typing import Iterable
from unittest.mock import MagicMock

import pytest

from event_engine.engine import (
    DriverHeartbeat,
    DriverRunResult,
    EngineDriver,
)
from event_engine.events import (
    BarType,
    FillEvent,
    MarketEvent,
    OrderDirection,
    OrderEvent,
    OrderType,
    TimeInForce,
)
from event_engine.exceptions import EventEngineError
from event_engine.execution import (
    AlmgrenChrissParams,
    SimulatedExchangeConfig,
    SimulatedExecutionHandler,
)
from event_engine.handlers import HistoricCSVDataHandler
from event_engine.portfolio import Portfolio, PortfolioPolicy
from event_engine.strategy import BollingerZScoreReversionStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(offset_seconds: int = 0) -> int:
    return int(
        datetime(2026, 7, 24, 9, 30 + offset_seconds, tzinfo=timezone.utc)
        .timestamp()
        * 1_000_000_000
    )


def _market(symbol: str, ts_offset: int, price: float,
            *, base: int | None = None,
            volume: float = 50_000.0) -> MarketEvent:
    return MarketEvent(
        timestamp_ns=(base or _ts(0)) + ts_offset * 1_000_000_000,
        symbol=symbol,
        open=price, high=price + 0.1, low=price - 0.1,
        close=price, volume=volume,
        bid_ask_spread=0.02,
        bar_type=BarType.BAR_1M,
    )


def _make_driver(
    *,
    symbols: Iterable[tuple[str, list[float]]] = (),
    strategy_factory=BollingerZScoreReversionStrategy,
    portfolio_kwargs: dict | None = None,
    heartbeat_every: int = 100,
    record_heartbeats: bool = True,
):
    """Build an EngineDriver backed by an in-memory data handler."""
    data = HistoricCSVDataHandler()
    base = _ts(0)
    for symbol, prices in symbols:
        rows = []
        for i, p in enumerate(prices):
            low = min(p, p - 0.5)
            open_ = p - 0.25
            high = max(p, p + 0.5)
            close = p
            ts = base + i * 60_000_000_000
            rows.append((ts, open_, high, low, close, 50_000.0, 0.02))
        data.register_in_memory_series(symbol, rows, bar_type=BarType.BAR_1M)

    defaults = {
        "initial_cash": 100_000.0,
        "max_position_value": 10_000.0,
        "leverage_limit": 2.0,
        "max_symbol_weight": 1.0,
    }
    if portfolio_kwargs:
        defaults.update(portfolio_kwargs)
    portfolio = Portfolio(PortfolioPolicy(**defaults))
    execution = SimulatedExecutionHandler(
        AlmgrenChrissParams(theta=0.0, eta=0.0, Y=0.0, dt=60.0),
        SimulatedExchangeConfig(
            max_participation_pct=0.10, commission_per_share=0.0,
        ),
    )
    strategies = [strategy_factory()]
    return EngineDriver(
        data_handler=data,
        execution_handler=execution,
        portfolio=portfolio,
        strategies=strategies,
        heartbeat_every=heartbeat_every,
        record_heartbeats=record_heartbeats,
    )


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_requires_at_least_one_strategy():
    with pytest.raises(EventEngineError):
        EngineDriver(
            data_handler=MagicMock(),
            execution_handler=MagicMock(),
            portfolio=MagicMock(),
            strategies=[],
        )


# ---------------------------------------------------------------------------
# Run + lifecycle
# ---------------------------------------------------------------------------


def test_run_with_no_market_data_drains_quickly():
    """An empty data handler should produce a no-op run with
    zero events processed."""
    driver = _make_driver(symbols=[])
    with driver:
        result = driver.run()
    assert result.total_events_processed == 0
    assert result.total_fills == 0
    assert result.elapsed_seconds >= 0


def test_run_drains_market_events_through_the_loop():
    """Synthetic price series that produces one LONG entry and a
    follow-up exit. The driver must publish at least one fill and
    process at least as many events as there are market bars.
    """
    # Build a price series: 20 bars around 100 (warmup), then a big
    # drop that exceeds entry_z, then a reversion back.
    warmup = [100.0 + (i % 3) * 0.01 for i in range(15)]
    drop = [100.0 - 3.0 * i for i in range(1, 6)]   # 97, 94, 91, 88, 85
    reversion = [85.0 + 1.0 * i for i in range(1, 6)]  # 86, 87, 88, 89, 90
    prices = warmup + drop + reversion

    driver = _make_driver(
        symbols=[("AAPL", prices)],
        heartbeat_every=5,
    )
    with driver:
        result = driver.run()
    # ``total_events_processed`` includes the MarketEvents plus any
    # OrderEvents / FillEvents the strategy publishes onto the
    # queue. We assert >= |prices| to allow non-MARKET traffic.
    assert result.total_events_processed >= len(prices)
    assert result.total_fills >= 1
    assert result.total_events_processed > 0


def test_run_records_heartbeats_when_requested():
    driver = _make_driver(
        symbols=[("AAPL", [100.0 + 0.01 * (i % 3) for i in range(50)])],
        heartbeat_every=10,
        record_heartbeats=True,
    )
    with driver:
        result = driver.run()
    # We process 50 events with heartbeat_every=10 → 5 heartbeats
    # (at 10, 20, 30, 40, 50).
    assert len(result.heartbeats) >= 4
    for hb in result.heartbeats:
        assert isinstance(hb, DriverHeartbeat)
        assert hb.events_processed > 0


def test_run_omits_heartbeats_when_record_false():
    driver = _make_driver(
        symbols=[("AAPL", [100.0 + 0.01 * (i % 3) for i in range(50)])],
        heartbeat_every=10,
        record_heartbeats=False,
    )
    with driver:
        result = driver.run()
    assert result.heartbeats == []


def test_run_is_idempotent_under_reset_and_re_run():
    """Calling reset then run again starts a fresh pass; the queues
    and heartbeats do not bleed from the prior run."""
    prices = [100.0 + (i % 3) * 0.01 for i in range(30)]
    driver = _make_driver(symbols=[("AAPL", prices)])
    with driver:
        first = driver.run()
        driver.reset()
        second = driver.run()
    assert first.total_events_processed == second.total_events_processed


def test_context_manager_exits_cleanly_on_exception():
    driver = _make_driver(symbols=[])
    try:
        with driver:
            raise RuntimeError("simulated failure")
    except RuntimeError:
        pass
    # No exception propagated out of __exit__.
    assert driver._shutdown_requested is False  # type: ignore[attr-defined]


def test_signal_handler_invokes_shutdown(monkeypatch):
    """The driver installs SIGINT/SIGTERM handlers on __enter__ and
    invokes ``shutdown`` when a signal arrives."""
    driver = _make_driver(symbols=[])
    with driver:
        # Simulate SIGINT.
        signal_value = (
            driver._old_handlers[signal.SIGINT]
            if signal.SIGINT in driver._old_handlers
            else None
        )
        # Call the registered handler directly.
        driver._signal_handler(signal.SIGINT, None)
        assert driver._shutdown_requested is True  # type: ignore[attr-defined]


def test_signal_handler_restore_after_with(monkeypatch):
    """``__exit__`` must restore the previous SIGINT/SIGTERM handlers,
    not leave the process pointing at the driver's method."""
    saved_int = signal.getsignal(signal.SIGINT)
    driver = _make_driver(symbols=[])
    with driver:
        pass
    # After context exit, the handler should be restored.
    if saved_int is not None:
        assert signal.getsignal(signal.SIGINT) == saved_int


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def test_heartbeats_generator_yields_in_order():
    driver = _make_driver(
        symbols=[("AAPL", [100.0 + 0.01 * (i % 3) for i in range(50)])],
        heartbeat_every=10,
    )
    with driver:
        driver.run()
    beats = list(driver.heartbeats())
    assert beats == driver._heartbeats  # type: ignore[attr-defined]


def test_summary_includes_events_fills_equity():
    driver = _make_driver(symbols=[])
    with driver:
        result = driver.run()
    summary = result.summary()
    assert "events=0" in summary
    assert "fills=0" in summary
    assert "final_equity" in summary
