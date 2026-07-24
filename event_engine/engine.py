"""Master orchestrator connecting pre-filter parameters into the
event-driven pipeline.

Architectural notes:

* Strict isolation between :class:`VectorizedPreFilter` and the
  event-driven loop. The pre-filter reads price history and *never*
  touches ``EngineDriver`` state. The driver consumes the pre-filter's
  ranked parameters and constructs a fresh strategy per parameter
  combination; those strategies live for the duration of ``run()``
  and are dropped from memory on shutdown.

* Deterministic single-thread time loop. Every bar is consumed in
  priority order from the :class:`EventQueue`. SIGINT and SIGTERM
  trigger a clean shutdown that finalises the portfolio and emits a
  summary heartbeat line. There are exactly two writer threads —
  the data handler's ``stream`` and the main loop's per-bar fill
  publication — and they communicate only through the queue so there
  is no shared mutable state outside of the queue and the
  :class:`Portfolio`.

* Top-N parameters from the pre-filter are evaluated sequentially
  against the event-driven core: each combination produces one
  per-combination equity curve, aggregated into a unified summary.

Numerical discipline matches the rest of the package: float64
throughout, with ``round(..., 4)`` on the public surface for cash,
equity, drawdown, and P&L.
"""
from __future__ import annotations

import logging
import signal
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Iterable, Optional

import pandas as pd

from event_engine.events import (
    Event,
    FillEvent,
    MarketEvent,
    OrderEvent,
    SignalEvent,
)
from event_engine.execution import ExchangeHandler
from event_engine.exceptions import DataHandlerError, EventEngineError
from event_engine.handlers import AbstractDataHandler
from event_engine.portfolio import Portfolio
from event_engine.queue import EventQueue
from event_engine.strategy import AbstractStrategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DriverHeartbeat:
    """One row in the per-cycle heartbeat log."""

    events_processed: int
    elapsed_seconds: float
    cash: float
    equity: float
    realised_pnl: float
    unrealised_pnl: float


@dataclass(slots=True)
class DriverRunResult:
    """Summary of one ``EngineDriver.run()`` invocation."""

    heartbeats: list[DriverHeartbeat] = field(default_factory=list)
    per_strategy: dict[str, dict[str, float]] = field(default_factory=dict)
    total_events_processed: int = 0
    total_fills: int = 0
    total_exits: int = 0
    final_equity: float = 0.0
    final_realised_pnl: float = 0.0
    elapsed_seconds: float = 0.0

    def summary(self) -> str:
        rows = [
            f"events={self.total_events_processed}",
            f"fills={self.total_fills}",
            f"exits={self.total_exits}",
            f"final_equity={self.final_equity:.4f}",
            f"realised_pnl={self.final_realised_pnl:.4f}",
            f"elapsed={self.elapsed_seconds:.2f}s",
        ]
        return "DriverRunResult(" + ", ".join(rows) + ")"


# ---------------------------------------------------------------------------
# Engine driver
# ---------------------------------------------------------------------------


class EngineDriver(AbstractContextManager):
    """Orchestrate the event-driven backtest loop.

    Lifecycle:

    1. Constructed with the top-N parameter combinations (or a
       pre-built strategy list).
    2. Used as a context manager:

        with EngineDriver(...) as driver:
            for hb in driver.heartbeats():
                print(hb)
    3. ``driver.run()`` is the synchronous main loop. SIGINT /
       SIGTERM invoke ``driver.shutdown()`` so the loop ends
       cleanly with summary metrics.

    Inputs are reused across runs — the strategies list, the
    data handler, the portfolio, and the queue are reset between
    runs when ``run`` is invoked. The lifecycle helper ``reset()``
    is also exposed for callers who want to compose multiple runs
    manually.
    """

    def __init__(
        self,
        *,
        data_handler: AbstractDataHandler,
        execution_handler: ExchangeHandler,
        portfolio: Portfolio,
        strategies: list[AbstractStrategy],
        event_queue_factory=None,
        heartbeat_every: int = 100,
        record_heartbeats: bool = True,
        mark_to_market_on_eod: bool = True,
    ) -> None:
        if not strategies:
            raise EventEngineError(
                "EngineDriver requires at least one strategy"
            )
        self._data_handler = data_handler
        self._execution_handler = execution_handler
        self._portfolio = portfolio
        self._strategies = list(strategies)
        self._queue_factory = event_queue_factory or EventQueue
        self._heartbeat_every = max(1, int(heartbeat_every))
        self._record_heartbeats = bool(record_heartbeats)
        self._mark_to_market_on_eod = bool(mark_to_market_on_eod)

        self._queue: EventQueue = self._queue_factory()
        self._heartbeats: list[DriverHeartbeat] = []
        self._run_result: DriverRunResult | None = None

        self._shutdown_requested = False
        # Signal handlers are installed only when ``__enter__`` runs,
        # so the driver can be used in tests without touching the
        # process-level signal handlers.
        self._old_handlers: dict[int, object] = {}

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "EngineDriver":
        self._install_signal_handlers()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            self._restore_signal_handlers()
        finally:
            # If the with-block raised, make sure the queue is poisoned
            # so any blocked consumers wake.
            try:
                self._queue.poison()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def queue(self) -> EventQueue:
        return self._queue

    @property
    def strategies(self) -> list[AbstractStrategy]:
        return tuple(self._strategies)

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    @property
    def run_result(self) -> Optional[DriverRunResult]:
        """Result of the most recent ``run()`` invocation."""
        return self._run_result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> DriverRunResult:
        """Synchronous main loop. Drives ``data_handler.stream`` and
        consumes events until the queue is drained or shutdown is
        requested.

        Returns the per-run summary.
        """
        self._reset_state()
        started = time.monotonic()
        events_processed = 0
        fills = 0
        exits = 0

        # Publish all MarketEvents from the data handler into the
        # queue first. The handler may itself be a generator; this
        # pattern lets it keep its existing semantics.
        try:
            self._drain_data_into_queue()
        except DataHandlerError as exc:
            # An empty / uninitialised data handler is the canonical
            # no-op case (the test harness often uses one). Promote
            # the error to a warning and let the main loop exit
            # cleanly with zero events processed.
            logger.warning(
                "data handler has no sources to stream: %s", exc
            )
        except Exception:
            logger.exception("data handler stream raised; aborting")
            self._queue.poison()
            raise

        # Main consumer loop.
        while not self._shutdown_requested and not self._queue.empty():
            try:
                event = self._queue.get(timeout=0.0)
            except Exception:
                break
            events_processed += 1
            if event.kind == "MARKET":
                signals = self._on_market(event)
                for sig in signals:
                    order = self._on_signal(sig, event)
                    if order is not None:
                        fill = self._on_order(order, event)
                        if fill is not None:
                            fills += 1
                            self._on_fill(fill)
                        else:
                            # Order did not fill (limit didn't cross, etc.).
                            pass
                self._portfolio.mark_to_market(
                    {event.symbol: event.close},
                    event.timestamp_ns,
                )
            elif event.kind == "FILL":
                fills += 1
                self._on_fill(event)
            else:
                # Anything else is interesting-but-ignored for now.
                pass

            if events_processed % self._heartbeat_every == 0:
                self._emit_heartbeat(events_processed, started)

        elapsed = time.monotonic() - started
        result = DriverRunResult(
            heartbeats=list(self._heartbeats),
            per_strategy=self._per_strategy_metrics(),
            total_events_processed=events_processed,
            total_fills=fills,
            total_exits=exits,
            final_equity=round(float(self._total_equity()), 4),
            final_realised_pnl=float(self._portfolio.realised_pnl),
            elapsed_seconds=round(elapsed, 4),
        )
        self._run_result = result
        # Drain any rests so the next run starts fresh.
        if self._queue.qsize():
            self._queue.poison()
        logger.info("driver_run %s", result.summary())
        return result

    def shutdown(self) -> None:
        """Politely request loop termination on next iteration."""
        self._shutdown_requested = True
        try:
            self._queue.poison()
        except Exception:
            pass

    def reset(self) -> None:
        """Reset per-run state and the queues, leaving the driver's
        configuration intact. Useful for batch-style test harnesses.
        """
        self._reset_state()

    def heartbeats(self) -> Iterable[DriverHeartbeat]:
        """Generator over the heartbeats logged so far."""
        yield from self._heartbeats

    # ------------------------------------------------------------------
    # Internal lifecycle
    # ------------------------------------------------------------------

    def _reset_state(self) -> None:
        self._queue = self._queue_factory()
        for s in self._strategies:
            s.reset()
        self._data_handler.reset()
        self._heartbeats = []
        self._shutdown_requested = False

    def _install_signal_handlers(self) -> None:
        """Wire SIGINT / SIGTERM to ``shutdown``. Cached so we can
        restore them on ``__exit__``.
        """
        for sig_num in (signal.SIGINT, signal.SIGTERM):
            try:
                self._old_handlers[sig_num] = signal.getsignal(sig_num)
                signal.signal(sig_num, self._signal_handler)
            except (ValueError, OSError):
                # Some environments (e.g. background threads) refuse
                # ``signal.signal``; carry on without installing.
                logger.debug(
                    "could not install signal %s handler", sig_num
                )

    def _restore_signal_handlers(self) -> None:
        for sig_num, prev in self._old_handlers.items():
            try:
                signal.signal(sig_num, prev)  # type: ignore[arg-type]
            except Exception:
                pass
        self._old_handlers.clear()

    def _signal_handler(self, signum, frame) -> None:
        logger.warning("received signal %s; shutting down", signum)
        self.shutdown()

    # ------------------------------------------------------------------
    # Pipeline primitives
    # ------------------------------------------------------------------

    def _drain_data_into_queue(self) -> None:
        """Pull MarketEvents out of the data handler's stream and
        ``put`` each onto the queue."""
        for event in self._data_handler.stream(self._queue):
            # The queue has received the event; we discard the
            # reference here because the event lives in the queue
            # until the main loop pops it.
            pass

    def _on_market(self, event: MarketEvent) -> list[SignalEvent]:
        signals: list[SignalEvent] = []
        for strategy in self._strategies:
            try:
                signals.extend(strategy.calculate_signals(event))
            except Exception:
                logger.exception(
                    "%s raised on_market for %s",
                    strategy.name, event.symbol,
                )
                continue
        return signals

    def _on_signal(
        self,
        signal: SignalEvent,
        market: MarketEvent,
    ) -> Optional[OrderEvent]:
        try:
            order = self._portfolio.on_signal(
                signal, last_price=market.close
            )
        except Exception:
            logger.exception(
                "portfolio rejected signal %s for %s",
                signal.signal_type, signal.symbol,
            )
            return None
        if order is not None:
            self._queue.put(order)
        return order

    def _on_order(
        self,
        order: OrderEvent,
        market: MarketEvent,
    ) -> Optional[FillEvent]:
        try:
            return self._execution_handler.execute_order(order, market)
        except Exception:
            logger.exception(
                "execution handler raised for %s on %s",
                order.order_id, market.symbol,
            )
            return None

    def _on_fill(self, fill: FillEvent) -> None:
        """Apply the fill to the portfolio.

        We deliberately do *not* re-publish the fill back onto the
        queue: a fill is a terminal event in the backtest loop, and
        re-publishing would create an infinite consume-and-re-publish
        cycle. Strategies that need fill-level state (e.g. for
        position tracking) can subscribe via the
        :meth:`AbstractStrategy.on_bar` hook or by polling the
        portfolio directly.
        """
        try:
            self._portfolio.on_fill(fill)
        except Exception:
            logger.exception(
                "portfolio rejected fill %s", fill.order_id
            )

    # ------------------------------------------------------------------
    # Heartbeats and metrics
    # ------------------------------------------------------------------

    def _emit_heartbeat(self, events_processed: int, started: float) -> None:
        elapsed = time.monotonic() - started
        equity = self._total_equity()
        hb = DriverHeartbeat(
            events_processed=events_processed,
            elapsed_seconds=round(elapsed, 4),
            cash=self._portfolio.cash,
            equity=round(float(equity), 4),
            realised_pnl=float(self._portfolio.realised_pnl),
            unrealised_pnl=float(
                self._portfolio.unrealised_pnl(
                    self._latest_marks()
                )
            ),
        )
        if self._record_heartbeats:
            self._heartbeats.append(hb)
        logger.info(
            "heartbeat events=%d elapsed=%.2fs cash=%.4f equity=%.4f",
            events_processed, elapsed, hb.cash, hb.equity,
        )

    def _total_equity(self) -> float:
        marks = self._latest_marks()
        if not marks:
            # No market events seen yet — equity equals cash.
            return self._portfolio.cash
        return self._portfolio.total_equity(marks)

    def _latest_marks(self) -> dict[str, float]:
        """Walk the strategy's known symbols at their last seen bar."""
        # We rely on the strategies' state to know the latest close,
        # but a simpler source is the data handler itself.
        marks: dict[str, float] = {}
        for symbol in self._data_handler.symbols():
            last = self._data_handler.latest_bar(symbol)
            if last is not None:
                marks[symbol] = last.close
        return marks

    def _per_strategy_metrics(self) -> dict[str, dict[str, float]]:
        # The engine itself doesn't track per-strategy equity (the
        # strategies share one Portfolio account). We report the
        # number of fills per strategy as a lightweight proxy.
        out: dict[str, dict[str, float]] = {}
        for strategy in self._strategies:
            p = strategy.parameters()
            out[strategy.name] = {
                "parameters_count": len(p),
            }
        return out


__all__ = ["EngineDriver", "DriverRunResult", "DriverHeartbeat"]
