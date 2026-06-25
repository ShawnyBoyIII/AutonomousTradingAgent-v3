from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


STRATEGY_LOG = "strategy_results.jsonl"


def _log_path(log_dir: Path) -> Path:
    return log_dir / STRATEGY_LOG


def record_entry(log_dir: Path, strategy_tag: str, ticker: str, entry_price: float, timestamp: datetime) -> None:
    """Record a strategy entry event."""
    path = _log_path(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "event": "entry",
                    "strategy_tag": strategy_tag,
                    "ticker": ticker,
                    "entry_price": entry_price,
                    "timestamp": timestamp.isoformat(),
                }
            )
            + "\n"
        )


def record_exit(
    log_dir: Path,
    strategy_tag: str,
    ticker: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
    fees: float,
    pnl: float,
    reason: str,
    timestamp: datetime,
) -> None:
    """Record a strategy exit event with PnL."""
    path = _log_path(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    win = pnl > 0
    with path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "event": "exit",
                    "strategy_tag": strategy_tag,
                    "ticker": ticker,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "quantity": quantity,
                    "fees": fees,
                    "pnl": round(pnl, 2),
                    "win": win,
                    "reason": reason,
                    "timestamp": timestamp.isoformat(),
                }
            )
            + "\n"
        )


def _read_events(log_dir: Path) -> list[dict[str, Any]]:
    path = _log_path(log_dir)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def rolling_win_rate(log_dir: Path, strategy_tag: str, window: int = 20) -> float:
    """Compute the win rate for *strategy_tag* over the last *window* exits."""
    events = _read_events(log_dir)
    exits = [e for e in events if e.get("event") == "exit" and e.get("strategy_tag") == strategy_tag]
    recent = exits[-window:]
    if not recent:
        return 0.0
    wins = sum(1 for e in recent if e.get("win"))
    return wins / len(recent)


def allocation_multiplier(
    log_dir: Path,
    strategy_tag: str,
    window: int = 20,
    min_win_rate: float = 0.40,
    full_allocation_rate: float = 0.50,
) -> float:
    """Return an allocation multiplier for *strategy_tag* based on recent performance.

    Rules
    -----
    * Fewer than *window* exits → 1.0 (insufficient data to penalise).
    * Win rate ≥ *full_allocation_rate* → 1.0.
    * Win rate between *min_win_rate* and *full_allocation_rate* → 0.5.
    * Win rate < *min_win_rate* → 0.0 (skip the strategy).
    """
    events = _read_events(log_dir)
    exits = [e for e in events if e.get("event") == "exit" and e.get("strategy_tag") == strategy_tag]
    recent = exits[-window:]
    if len(recent) < window:
        return 1.0
    wins = sum(1 for e in recent if e.get("win"))
    rate = wins / len(recent)

    if rate >= full_allocation_rate:
        return 1.0
    if rate >= min_win_rate:
        return 0.5
    return 0.0


def strategy_summary(log_dir: Path, window: int = 20) -> list[dict[str, object]]:
    """Return a summary of all tracked strategies with recent performance."""
    events = _read_events(log_dir)
    strategy_exits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e.get("event") == "exit":
            tag = e.get("strategy_tag", "")
            if tag:
                strategy_exits[tag].append(e)

    results: list[dict[str, object]] = []
    for tag in sorted(strategy_exits):
        exits = strategy_exits[tag]
        recent = exits[-window:]
        total_pnl = sum(e.get("pnl", 0.0) for e in recent)
        wins = sum(1 for e in recent if e.get("win"))
        rate = wins / len(recent) if recent else 0.0
        results.append(
            {
                "strategy": tag,
                "total_exits": len(exits),
                "recent_exits": len(recent),
                "recent_wins": wins,
                "recent_losses": len(recent) - wins,
                "recent_win_rate": round(rate, 4),
                "recent_net_pnl": round(total_pnl, 2),
                "allocation": allocation_multiplier(log_dir, tag, window=window),
            }
        )
    return results
