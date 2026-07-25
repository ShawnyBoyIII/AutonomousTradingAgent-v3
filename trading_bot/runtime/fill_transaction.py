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
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from trading_bot.learning.experiments.runtime_canary import RuntimeCanaryContext

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
    canary_experiment_id: str | None = None,
    canary_baseline_quantity: int | None = None,
    runtime_canary: RuntimeCanaryContext | None = None,
    pre_policy_size: int | None = None,
) -> FillTransaction:
    """Build the canonical BUY transaction.

    Steps in order:
        1. ``ledger.record_fill`` — orders table + fills JSON.
        2. ``sql_persist`` — SQL trades + positions.
        3. ``strategy_tracker.record_entry`` — adaptive strategy state.
        4. (optional) snapshot_callable — caller-provided final durable step.
        5. (optional) runtime canary shadow recording.
    """
    tx = FillTransaction()

    def step_record_order(fill: Any, side: str, **ctx: Any) -> None:
        canary_kwargs: dict[str, Any] = {}
        if canary_experiment_id is not None:
            canary_kwargs["canary_experiment_id"] = canary_experiment_id
        if canary_baseline_quantity is not None:
            canary_kwargs["canary_baseline_quantity"] = canary_baseline_quantity
        ledger.record_fill(
            fill,
            side=side,
            strategy_tag=strategy_tag,
            **canary_kwargs,
        )

    def step_strategy_tracker(fill: Any, side: str, **ctx: Any) -> None:
        from trading_bot.strategy.strategy_tracker import record_entry

        record_entry(
            log_dir_path,
            strategy_tag,
            fill.ticker,
            fill.fill_price,
            getattr(fill, "filled_at", None) or ctx.get("filled_at"),
        )

    def step_runtime_canary(fill: Any, side: str, **ctx: Any) -> None:
        if runtime_canary is None or pre_policy_size is None:
            return
        filled_at = getattr(fill, "filled_at", None)
        runtime_canary.record_entry(
            operation_id=fill.order_id,
            ticker=fill.ticker,
            baseline_quantity=pre_policy_size,
            candidate_quantity=fill.quantity,
            fill_price=fill.fill_price,
            fees=fill.fees,
            session_date=(
                filled_at.date().isoformat()
                if getattr(filled_at, "date", None)
                else None
            ),
        )

    tx.register(step_record_order)
    tx.register(sql_persist)
    if strategy_tag:
        tx.register(step_strategy_tracker)
    if snapshot_callable is not None:
        tx.register(snapshot_callable)
    if runtime_canary is not None:
        tx.register(step_runtime_canary)
    return tx


def build_sell_transaction(
    *,
    ledger: Any,
    sql_persist: Callable[..., Any],
    strategy_tag: str,
    snapshot_callable: Callable[..., Any] | None = None,
    canary_experiment_id: str | None = None,
    runtime_canary: RuntimeCanaryContext | None = None,
) -> FillTransaction:
    """Build the canonical SELL transaction.

    Steps in order:
        1. ``ledger.record_fill`` — orders table with realized_pnl.
        2. ``sql_persist`` — SQL trades UPDATE + positions close.
        3. (optional) snapshot_callable — caller-provided final durable step.
        4. (optional) runtime canary shadow recording.
    """
    tx = FillTransaction()

    def step_record_order(fill: Any, side: str, **ctx: Any) -> None:
        realized_pnl = float(ctx.get("realized_pnl", 0.0) or 0.0)
        canary_kwargs: dict[str, Any] = {}
        if canary_experiment_id is not None:
            canary_kwargs["canary_experiment_id"] = canary_experiment_id
        ledger.record_fill(
            fill,
            side=side,
            realized_pnl=realized_pnl,
            strategy_tag=strategy_tag,
            **canary_kwargs,
        )

    def step_runtime_canary(fill: Any, side: str, **ctx: Any) -> None:
        if runtime_canary is None:
            return
        filled_at = getattr(fill, "filled_at", None)
        runtime_canary.record_exit(
            operation_id=fill.order_id,
            ticker=fill.ticker,
            candidate_quantity=fill.quantity,
            fill_price=fill.fill_price,
            fees=fill.fees,
            session_date=(
                filled_at.date().isoformat()
                if getattr(filled_at, "date", None)
                else None
            ),
        )

    tx.register(step_record_order)
    tx.register(sql_persist)
    if snapshot_callable is not None:
        tx.register(snapshot_callable)
    if runtime_canary is not None:
        tx.register(step_runtime_canary)
    return tx
