"""Single-source fill transaction.

Replaces the multi-write pattern where BUY and SELL paths persisted
fill data to ``orders``, SQL ``trades``, SQL ``positions``, the JSON
``portfolio_state``, and the equity history table in independent
operations. Each path opened its own connection and the SQL writes
were even wrapped in ``except Exception: logger.exception(...)``, so a
corrupt database silently desynced from the JSON state.

The transaction is intentionally simple: a single callable that does
all writes in one helper. Callers catch a single
:class:`FillTransactionError` to roll back. The intent is to make the
five stores either all-update or fail-fast.

Current scope: ledger + SQL. Broker is already atomic at fill time.
JSON snapshots are persisted last so a JSON failure cannot leave the
DB ahead of the user-facing summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


class FillTransactionError(RuntimeError):
    """Raised when a fill transaction cannot complete atomically."""


@dataclass
class FillTransaction:
    """A composable fill transaction.

    Each ``register()`` callable receives ``(fill, side, **ctx)`` and
    must raise :class:`FillTransactionError` on a permanent failure
    (e.g. database corruption). Transient failures should be retried
    inside the callable; the transaction itself does not retry.

    ``run()`` executes the registered callables in order. If any
    raises, it stops immediately and re-raises; the caller decides
    whether to roll back. Successful runs return ``None``.

    Example::

        tx = FillTransaction()
        tx.register(ledger_persist_callable)
        tx.register(sql_persist_callable)
        tx.register(equity_snapshot_callable)
        tx.run(fill=fill, side="BUY")
    """

    steps: list[Callable[..., Any]] = field(default_factory=list)

    def register(self, fn: Callable[..., Any]) -> None:
        self.steps.append(fn)

    def run(self, *, fill: Any, side: str, **ctx: Any) -> None:
        for step in self.steps:
            try:
                step(fill=fill, side=side, **ctx)
            except FillTransactionError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise FillTransactionError(
                    f"fill transaction step {step.__name__} failed"
                ) from exc


def build_buy_transaction(
    *,
    ledger: Any,
    sql_persist: Callable[..., Any],
    strategy_tag: str,
    log_dir_path: Any,
    snapshot_callable: Callable[..., Any] | None = None,
) -> FillTransaction:
    """Build the canonical BUY transaction.

    Steps in order:
        1. ``ledger.record_fill`` — orders table + fills JSON.
        2. ``sql_persist`` — SQL trades + positions.
        3. ``strategy_tracker.record_entry`` — adaptive strategy state.
        4. (optional) snapshot_callable — caller-provided final step
           that persists portfolio_state and equity history.
    """
    tx = FillTransaction()

    def step_record_order(fill: Any, side: str, **ctx: Any) -> None:
        ledger.record_fill(fill, side=side, strategy_tag=strategy_tag)

    def step_strategy_tracker(fill: Any, side: str, **ctx: Any) -> None:
        from trading_bot.strategy.strategy_tracker import record_entry

        record_entry(
            log_dir_path,
            strategy_tag,
            fill.ticker,
            fill.fill_price,
            getattr(fill, "filled_at", None) or ctx.get("filled_at"),
        )

    tx.register(step_record_order)
    tx.register(sql_persist)
    if strategy_tag:
        tx.register(step_strategy_tracker)
    if snapshot_callable is not None:
        tx.register(snapshot_callable)
    return tx


def build_sell_transaction(
    *,
    ledger: Any,
    sql_persist: Callable[..., Any],
    strategy_tag: str,
    snapshot_callable: Callable[..., Any] | None = None,
) -> FillTransaction:
    """Build the canonical SELL transaction.

    Steps in order:
        1. ``ledger.record_fill`` — orders table with realized_pnl.
        2. ``sql_persist`` — SQL trades UPDATE + positions close.
        3. (optional) snapshot_callable — caller-provided final step.
    """
    tx = FillTransaction()

    def step_record_order(fill: Any, side: str, **ctx: Any) -> None:
        realized_pnl = float(ctx.get("realized_pnl", 0.0) or 0.0)
        ledger.record_fill(
            fill, side=side, realized_pnl=realized_pnl, strategy_tag=strategy_tag
        )

    tx.register(step_record_order)
    tx.register(sql_persist)
    if snapshot_callable is not None:
        tx.register(snapshot_callable)
    return tx
