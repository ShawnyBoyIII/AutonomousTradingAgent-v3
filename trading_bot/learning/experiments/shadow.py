from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from trading_bot.learning.experiments.models import MetricSet


@dataclass(frozen=True)
class ShadowFill:
    ticker: str
    side: Literal["BUY", "SELL"]
    quantity: int
    fill_price: float
    fees: float


class ShadowLedger:
    """Append-only ledger for paired-baseline paper simulation."""

    def __init__(self, artifacts_dir: Path, starting_cash: float) -> None:
        self.artifacts_dir = Path(artifacts_dir)
        self.starting_cash = float(starting_cash)
        self._cash = float(starting_cash)
        self._positions: dict[str, dict[str, float]] = {}
        self._trade_count = 0
        self._fills_path = self.artifacts_dir / "shadow-fills.jsonl"
        self._equity_path = self.artifacts_dir / "shadow-equity.jsonl"

    def record(self, fill: ShadowFill) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        cost = fill.fill_price * fill.quantity + fill.fees
        if fill.side == "BUY":
            self._cash -= cost
            pos = self._positions.setdefault(
                fill.ticker, {"qty": 0, "cost_basis": 0.0}
            )
            pos["qty"] += fill.quantity
            pos["cost_basis"] += cost
            self._trade_count += 1
        else:
            self._cash += fill.fill_price * fill.quantity - fill.fees
            pos = self._positions.get(fill.ticker, {"qty": 0, "cost_basis": 0.0})
            pos["qty"] -= fill.quantity
            if pos["qty"] <= 0:
                self._positions.pop(fill.ticker, None)
        self._append_line(self._fills_path, fill.__dict__)
        self._append_line(
            self._equity_path,
            {"equity": self.metrics().net_pnl + self.starting_cash},
        )

    def metrics(self) -> MetricSet:
        realized = self._cash - self.starting_cash
        return MetricSet(
            trades=self._trade_count,
            profit_factor=0.0,
            net_pnl=realized,
            max_drawdown_pct=0.0,
        )

    def restore_positions(self, positions: dict[str, dict[str, float]]) -> None:
        self._positions = dict(positions)

    def snapshot_positions(self) -> dict[str, dict[str, float]]:
        return {ticker: dict(values) for ticker, values in self._positions.items()}

    def _append_line(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)