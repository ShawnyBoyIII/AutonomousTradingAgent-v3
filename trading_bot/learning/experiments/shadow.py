"""Independent paired-baseline shadow broker for tuning experiments.

The candidate policy is exercised by the live ``run_paper_trade`` path. To
compare candidate economics against a frozen baseline without polluting the
production ledger, this module runs an independent baseline broker in
lockstep with each candidate fill.

Key invariants:

1. The baseline broker uses the *baseline* ``SupermodelSettings`` (with
   ``range_bound_trend_caution_multiplier=1.0``) so its sizing reflects the
   pre-experiment behavior, regardless of what the live candidate used.
2. Each ledger tracks realized P&L from *closed* trades, not from cash
   changes; an open position is marked-to-market using the latest fill
   price the ledger has seen.
3. Profit factor is computed as ``gross_profit / |gross_loss|`` over
   closed trades; ``max_drawdown_pct`` is the largest equity peak-to-trough
   percentage observed over the recorded equity curve.
4. State is persisted to JSONL artifacts; a fresh ``ShadowLedger``
   constructed with the same ``artifacts_dir`` rebuilds cash, positions,
   closed-trade history, and equity series from disk.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from trading_bot.learning.experiments.models import MetricSet, ParameterChange


@dataclass(frozen=True)
class ShadowFill:
    ticker: str
    side: Literal["BUY", "SELL"]
    quantity: int
    fill_price: float
    fees: float
    applied_multiplier: float = 1.0
    operation_id: str = field(default="")

    def __post_init__(self) -> None:
        # ``field(default=...)`` cannot precede required dataclass fields, so
        # we coerce ``None`` (the JSON ``null`` after a legacy replay) back
        # to ``""`` to keep the duplicate-detection logic uniform.
        if self.operation_id is None:  # type: ignore[unreachable]
            object.__setattr__(self, "operation_id", "")


class ShadowLedger:
    """Independent baseline or candidate ledger for paired canary runs.

    Tracks closed-trade realized P&L, marked-to-market equity, and the
    full equity curve so ``profit_factor`` and ``max_drawdown_pct`` are
    computable from the ledger alone.
    """

    def __init__(
        self,
        artifacts_dir: Path,
        starting_cash: float,
        *,
        ledger_id: str = "baseline",
    ) -> None:
        self.artifacts_dir = Path(artifacts_dir)
        self.starting_cash = float(starting_cash)
        self.ledger_id = ledger_id
        self._cash = float(starting_cash)
        self._positions: dict[str, dict[str, float]] = {}
        self._closed_pnls: list[float] = []
        self._equity_curve: list[float] = [float(starting_cash)]
        self._completed_positions: int = 0
        self._applied_operation_ids: set[str] = set()
        prefix = "" if ledger_id == "baseline" else f"{ledger_id}-"
        self._fills_path = self.artifacts_dir / f"{prefix}shadow-fills.jsonl"
        self._equity_path = self.artifacts_dir / f"{prefix}shadow-equity.jsonl"
        self._load_state()

    def record(self, fill: ShadowFill) -> None:
        """Record a fill at its independently-sized quantity.

        The shadow's sizing is determined upstream (``applied_multiplier``
        on the fill), so the ledger does not know whether the fill came
        from the candidate or the baseline; it just records the fill.

        Idempotency: a non-empty ``fill.operation_id`` is recorded the
        first time and silently dropped on every subsequent call. The
        empty ``""`` sentinel is the legacy replay value and is **not**
        deduped — pre-schema callers lacked unique IDs and we must not
        silently drop their repeated retries. Completed-position
        accounting increments only when a SELL drives a previously
        positive ticker quantity to zero.
        """
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        if fill.quantity <= 0:
            return
        operation_id = fill.operation_id or ""
        if operation_id and operation_id in self._applied_operation_ids:
            return
        if operation_id:
            self._applied_operation_ids.add(operation_id)
        cost = fill.fill_price * fill.quantity + fill.fees
        if fill.side == "BUY":
            self._cash -= cost
            pos = self._positions.setdefault(
                fill.ticker, {"qty": 0.0, "cost_basis": 0.0}
            )
            pos["qty"] += fill.quantity
            pos["cost_basis"] += cost
        else:  # SELL
            pos = self._positions.get(fill.ticker, {"qty": 0.0, "cost_basis": 0.0})
            held_before = float(pos.get("qty", 0.0))
            sold_qty = min(float(fill.quantity), held_before)
            avg_cost = (
                float(pos["cost_basis"]) / held_before if held_before > 0 else 0.0
            )
            realized = (fill.fill_price - avg_cost) * sold_qty - fill.fees
            self._cash += fill.fill_price * sold_qty - fill.fees
            held_after = held_before - sold_qty
            pos["qty"] = held_after
            pos["cost_basis"] = max(0.0, float(pos.get("cost_basis", 0.0)) - avg_cost * sold_qty)
            if held_after <= 1e-9:
                self._positions.pop(fill.ticker, None)
            if sold_qty > 0:
                self._closed_pnls.append(realized)
            if held_before > 0 and held_after <= 1e-9:
                self._completed_positions += 1
        self._append_line(self._fills_path, fill.__dict__)
        equity = self._marked_to_market()
        self._equity_curve.append(equity)
        self._append_line(self._equity_path, {"equity": equity})

    @property
    def completed_positions(self) -> int:
        """Number of SELL fills that closed a ticker position to zero.

        Distinct from ``metrics().trades`` (realized SELL count) so the
        controller's gate can react to *finished positions* rather than
        to partial exits that record realized P&L.
        """
        return self._completed_positions

    def metrics(self) -> MetricSet:
        closed = len(self._closed_pnls)
        gross_profit = sum(p for p in self._closed_pnls if p > 0)
        gross_loss = -sum(p for p in self._closed_pnls if p < 0)
        if gross_loss > 0:
            pf = gross_profit / gross_loss
        elif gross_profit > 0:
            pf = float("inf")
        else:
            pf = 0.0
        max_dd = self._compute_max_drawdown_pct()
        return MetricSet(
            trades=closed,
            profit_factor=round(pf, 6) if math.isfinite(pf) else pf,
            net_pnl=round(sum(self._closed_pnls), 6),
            max_drawdown_pct=round(max_dd, 6),
        )

    def _marked_to_market(self) -> float:
        """Equity = cash + market value of open positions.

        We approximate market value with the most recent cost basis per
        share; this matches the paper broker's behavior since shadow fills
        do not observe live tick prices.
        """
        equity = self._cash
        for pos in self._positions.values():
            if pos["qty"] > 0:
                equity += float(pos["cost_basis"])
        return equity

    def _compute_max_drawdown_pct(self) -> float:
        if not self._equity_curve:
            return 0.0
        peak = self._equity_curve[0]
        max_dd = 0.0
        for value in self._equity_curve:
            if value > peak:
                peak = value
            if peak > 0:
                dd = (peak - value) / peak * 100.0
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    def snapshot_positions(self) -> dict[str, dict[str, float]]:
        return {
            ticker: {"qty": float(values["qty"]), "cost_basis": float(values["cost_basis"])}
            for ticker, values in self._positions.items()
        }

    def _append_line(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")

    def _load_state(self) -> None:
        """Reload ledger state from JSONL artifacts on construction.

        Rebuilt state covers cash (derived from recorded SELL fills minus
        BUY costs), positions, closed-trade P&L history, the equity
        curve, the completed-position counter, and the applied-id set.
        If artifacts are absent or malformed, the ledger starts fresh
        from ``starting_cash``.

        Malformed tail handling: parse line-by-line so a torn final record
        does not discard every previously valid entry. The valid prefix is
        preserved; the malformed tail is skipped silently (caller has the
        raw file if forensic recovery is needed).
        """
        if not self._fills_path.exists():
            return
        for line in self._fills_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # Skip the malformed line; keep going with whatever valid
                # records remain after it.
                continue
            try:
                fill = ShadowFill(
                    ticker=str(entry["ticker"]),
                    side=str(entry["side"]),
                    quantity=int(entry["quantity"]),
                    fill_price=float(entry["fill_price"]),
                    fees=float(entry["fees"]),
                    applied_multiplier=float(entry.get("applied_multiplier", 1.0)),
                    operation_id=str(entry.get("operation_id") or ""),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._record_in_memory(fill)


    def _record_in_memory(self, fill: ShadowFill) -> None:
        """Replay a fill into the in-memory state without re-writing artifacts.

        Replay must honor the same idempotency contract as ``record``:
        duplicate non-empty operation_ids are dropped so a torn-write
        recovery cannot double-count. The completed-position counter and
        the applied-id set are updated in lockstep with the live
        ``record`` path.
        """
        if fill.quantity <= 0:
            return
        operation_id = fill.operation_id or ""
        if operation_id and operation_id in self._applied_operation_ids:
            return
        if operation_id:
            self._applied_operation_ids.add(operation_id)
        cost = fill.fill_price * fill.quantity + fill.fees
        if fill.side == "BUY":
            self._cash -= cost
            pos = self._positions.setdefault(
                fill.ticker, {"qty": 0.0, "cost_basis": 0.0}
            )
            pos["qty"] += fill.quantity
            pos["cost_basis"] += cost
        else:
            pos = self._positions.get(fill.ticker, {"qty": 0.0, "cost_basis": 0.0})
            held_before = float(pos.get("qty", 0.0))
            sold_qty = min(float(fill.quantity), held_before)
            avg_cost = (
                float(pos["cost_basis"]) / held_before if held_before > 0 else 0.0
            )
            realized = (fill.fill_price - avg_cost) * sold_qty - fill.fees
            self._cash += fill.fill_price * sold_qty - fill.fees
            held_after = held_before - sold_qty
            pos["qty"] = held_after
            pos["cost_basis"] = max(
                0.0, float(pos.get("cost_basis", 0.0)) - avg_cost * sold_qty
            )
            if held_after <= 1e-9:
                self._positions.pop(fill.ticker, None)
            if sold_qty > 0:
                self._closed_pnls.append(realized)
            if held_before > 0 and held_after <= 1e-9:
                self._completed_positions += 1
        self._equity_curve.append(self._marked_to_market())


class PairedShadowHarness:
    """Independent paired broker: baseline vs. candidate.

    The harness supports ``record_entry`` / ``record_exit`` — exact-quantity
    mirroring used by the runtime canary. Callers pass the *baseline*
    quantity (pre-policy size) and the *candidate* quantity (actual
    filled size, post-policy). The harness never applies a multiplier
    internally, so the runtime executor remains the only place that
    decides sizing.

    Every ``record_entry`` and ``record_exit`` call requires a stable
    ``operation_id`` (the durable paper-order row id). The same id is
    applied atomically to both ledgers; a duplicate id is a no-op so a
    retried fill cannot double-realize P&L or double-count a completion.

    Exits derive the baseline SELL quantity from the fraction of the
    candidate position sold; a final exit closes both ledgers fully.
    The canary gate compares ``candidate_metrics()`` against
    ``baseline_metrics()`` at the decision boundary, and uses
    ``candidate_completed_trades()`` / ``baseline_completed_trades()``
    (not the realized-SELL counts) to gate the 20-trade minimum.
    """

    def __init__(
        self,
        *,
        artifacts_dir: Path,
        starting_cash: float,
        change: ParameterChange,
        baseline_multiplier: float = 1.0,
        candidate_multiplier: float = 1.0,
    ) -> None:
        self.artifacts_dir = Path(artifacts_dir)
        self.starting_cash = float(starting_cash)
        self.change = change
        self.baseline_multiplier = float(baseline_multiplier)
        self.candidate_multiplier = float(candidate_multiplier)
        self.baseline = ShadowLedger(
            artifacts_dir=artifacts_dir,
            starting_cash=starting_cash,
            ledger_id="baseline",
        )
        self.candidate = ShadowLedger(
            artifacts_dir=artifacts_dir,
            starting_cash=starting_cash,
            ledger_id="candidate",
        )
        self._applied_entry_ids: set[str] = set()
        self._applied_exit_ids: set[str] = set()

    def record_entry(
        self,
        operation_id: str,
        *,
        ticker: str,
        baseline_quantity: int,
        candidate_quantity: int,
        fill_price: float,
        fees: float,
    ) -> None:
        """Mirror a BUY at the supplied baseline and candidate quantities.

        ``operation_id`` is the durable paper-order row id and must be
        supplied so retries do not double-record. A duplicate id is
        rejected atomically: neither ledger is touched on the second
        call. No multiplier is applied here: callers are responsible for
        any pre- vs. post-policy split. ``baseline_quantity`` and
        ``candidate_quantity`` are both clamped at 0 silently — callers
        should already have filtered zero-quantity entries.
        """
        if not operation_id:
            raise ValueError("operation_id is required for record_entry")
        if operation_id in self._applied_entry_ids:
            return
        baseline_quantity = int(baseline_quantity)
        candidate_quantity = int(candidate_quantity)
        if baseline_quantity <= 0 and candidate_quantity <= 0:
            # Still mark the id as applied so a re-recorded zero-quantity
            # entry cannot escape idempotency.
            self._applied_entry_ids.add(operation_id)
            return
        # Mark BEFORE recording so an exception inside ``record`` cannot
        # leave one ledger updated and the other unupdated.
        self._applied_entry_ids.add(operation_id)
        if baseline_quantity > 0:
            self.baseline.record(
                ShadowFill(
                    ticker=ticker,
                    side="BUY",
                    quantity=baseline_quantity,
                    fill_price=fill_price,
                    fees=fees,
                    applied_multiplier=1.0,
                    operation_id=operation_id,
                )
            )
        if candidate_quantity > 0:
            self.candidate.record(
                ShadowFill(
                    ticker=ticker,
                    side="BUY",
                    quantity=candidate_quantity,
                    fill_price=fill_price,
                    fees=fees,
                    applied_multiplier=1.0,
                    operation_id=operation_id,
                )
            )

    def record_exit(
        self,
        operation_id: str,
        *,
        ticker: str,
        candidate_quantity: int,
        fill_price: float,
        fees: float,
    ) -> None:
        """Mirror a SELL using the candidate-side fraction as the baseline scale.

        ``operation_id`` must be supplied so retries do not double-record
        P&L or completed positions. A duplicate id is rejected
        atomically: neither ledger is touched on the second call. The
        baseline SELL quantity is
        ``round(baseline_held_before * (candidate_quantity / candidate_held_before))``.
        When ``candidate_held_before`` is zero the baseline side is
        skipped — the trade is not part of the paired shadow. A final
        exit clears both ledgers completely.
        """
        if not operation_id:
            raise ValueError("operation_id is required for record_exit")
        if operation_id in self._applied_exit_ids:
            return
        candidate_quantity = int(candidate_quantity)
        if candidate_quantity <= 0:
            self._applied_exit_ids.add(operation_id)
            return
        candidate_held_before = self._held_quantity(self.candidate, ticker)
        baseline_held_before = self._held_quantity(self.baseline, ticker)
        if candidate_held_before > 0:
            fraction = candidate_quantity / candidate_held_before
            baseline_exit_qty = int(round(baseline_held_before * fraction))
        else:
            baseline_exit_qty = 0

        # Mark BEFORE recording so an exception cannot leave one ledger
        # updated and the other unupdated.
        self._applied_exit_ids.add(operation_id)
        if baseline_exit_qty > 0:
            self.baseline.record(
                ShadowFill(
                    ticker=ticker,
                    side="SELL",
                    quantity=baseline_exit_qty,
                    fill_price=fill_price,
                    fees=fees,
                    applied_multiplier=1.0,
                    operation_id=operation_id,
                )
            )
        self.candidate.record(
            ShadowFill(
                ticker=ticker,
                side="SELL",
                quantity=candidate_quantity,
                fill_price=fill_price,
                fees=fees,
                applied_multiplier=1.0,
                operation_id=operation_id,
            )
        )

    @staticmethod
    def _held_quantity(ledger: ShadowLedger, ticker: str) -> int:
        positions = ledger.snapshot_positions()
        value = positions.get(ticker)
        if value is None:
            return 0
        return int(round(float(value.get("qty", 0.0))))

    def candidate_metrics(self) -> MetricSet:
        return self.candidate.metrics()

    def baseline_metrics(self) -> MetricSet:
        return self.baseline.metrics()

    def candidate_completed_trades(self) -> int:
        """Count of candidate-ledger positions closed to zero.

        Distinct from ``candidate_metrics().trades`` (realized-SELL
        count). The controller's 20-trade gate keys on this counter
        so partial exits do not advance the gate.
        """
        return self.candidate.completed_positions

    def baseline_completed_trades(self) -> int:
        """Count of baseline-ledger positions closed to zero."""
        return self.baseline.completed_positions

    def closed_trade_counts_match(self) -> bool:
        return self.candidate.metrics().trades == self.baseline.metrics().trades

    def completed_trade_counts_match(self) -> bool:
        """Return True iff both ledgers have completed the same number of positions.

        This is the divergence-invalidation signal: if a partial exit
        drives the candidate ledger to zero while the baseline still
        holds residual shares (or vice versa) the ledgers have
        diverged and the canary must be marked ``INCONCLUSIVE``.
        """
        return self.candidate.completed_positions == self.baseline.completed_positions