"""Paper-trading performance analytics.

Pure, network-free analytics that summarise the closed trades in the
burn-in SQLite database. The goal is to make PF/expectancy/strategy
attribution a single CLI call away without an ad-hoc SQL query.

Public API:
- ``summarize_paper_performance`` — build a :class:`PaperPerformanceReport`
  from the burn-in DB.
- ``format_paper_performance_report`` — render the report as text suitable
  for a CLI.
- ``PaperPerformanceReport`` — the data structure.

The module is intentionally isolated from market-data paths and from
the live broker adapters so it can be exercised under ``pytest`` with a
fixture SQLite file (per AGENTS.md safety contract).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class BucketAggregate:
    """A single dimension slice of paper-trade performance."""

    label: str
    trades: int
    wins: int
    losses: int
    net_pnl: float
    gross_wins: float
    gross_losses: float

    @property
    def win_rate(self) -> float:
        if self.trades == 0:
            return 0.0
        return self.wins / self.trades

    @property
    def avg_pnl_per_trade(self) -> float:
        if self.trades == 0:
            return 0.0
        return self.net_pnl / self.trades

    @property
    def profit_factor(self) -> float:
        if self.gross_losses <= 0:
            return float("inf") if self.gross_wins > 0 else 0.0
        return self.gross_wins / self.gross_losses


@dataclass(frozen=True)
class EvaluationWindow:
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class PaperPerformanceReport:
    total_trades: int
    winning_trades: int
    losing_trades: int
    realized_pnl: float
    gross_wins: float
    gross_losses: float
    by_strategy: list[BucketAggregate] = field(default_factory=list)
    by_hour: list[BucketAggregate] = field(default_factory=list)
    by_ticker: list[BucketAggregate] = field(default_factory=list)
    evaluation_window: EvaluationWindow = field(
        default_factory=lambda: EvaluationWindow(start=None, end=None)
    )

    @property
    def profit_factor(self) -> float:
        if self.gross_losses <= 0:
            return float("inf") if self.gross_wins > 0 else 0.0
        return self.gross_wins / self.gross_losses

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def avg_pnl_per_trade(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.realized_pnl / self.total_trades


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_key_and_pnl(row: dict) -> tuple[str, float]:
    """Returns the strategy bucket key and the realised pnl for a SELL row.

    Rows missing either side="SELL" or pnl are ignored.
    """
    if row.get("side") != "SELL":
        return "", 0.0
    try:
        pnl = float(row.get("pnl") or 0.0)
    except (TypeError, ValueError):
        pnl = 0.0
    return "ok", pnl


def _bucket(rows: Sequence[dict], key_fn) -> list[BucketAggregate]:
    aggregator: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "net": 0.0,
            "gross_wins": 0.0,
            "gross_losses": 0.0,
        }
    )
    for row in rows:
        _, pnl = _row_key_and_pnl(row)
        if _row_key_and_pnl(row)[0] != "ok":
            continue
        label = key_fn(row)
        bucket = aggregator[label]
        bucket["trades"] += 1
        if pnl > 0:
            bucket["wins"] += 1
            bucket["gross_wins"] += pnl
        else:
            bucket["losses"] += 1
            bucket["gross_losses"] += abs(pnl)
        bucket["net"] += pnl

    out: list[BucketAggregate] = []
    for label, bucket in aggregator.items():
        out.append(
            BucketAggregate(
                label=label,
                trades=int(bucket["trades"]),
                wins=int(bucket["wins"]),
                losses=int(bucket["losses"]),
                net_pnl=float(bucket["net"]),
                gross_wins=float(bucket["gross_wins"]),
                gross_losses=float(bucket["gross_losses"]),
            )
        )
    out.sort(key=lambda b: (b.net_pnl, b.label))
    return out


def _in_window(filled_at: str, since: datetime | None, until: datetime | None) -> bool:
    if since is None and until is None:
        return True
    parsed = _parse_iso(filled_at)
    if parsed is None:
        return False
    if since is not None and parsed < since:
        return False
    if until is not None and parsed > until:
        return False
    return True


def summarize_paper_performance(
    db_path: str | Path,
    since: datetime | None = None,
    until: datetime | None = None,
) -> PaperPerformanceReport:
    """Read closed SELL orders from the burn-in DB and aggregate P&L.

    Args:
        db_path: Path to a SQLite database containing an ``orders`` table
            (matches the burn-in ledger schema).
        since/until: Optional UTC-aware datetimes used to filter rows by
            ``filled_at``. Either or both may be None.

    Returns:
        :class:`PaperPerformanceReport` with overall totals plus
        per-strategy, per-hour, and per-ticker breakdowns.
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"DB not found: {path}")

    conn = sqlite3.connect(str(path))
    try:
        try:
            rows = list(
                conn.execute(
                    """
                    SELECT id, ticker, side, quantity, fill_price, fees,
                           filled_at, pnl, strategy_tag
                    FROM orders
                    WHERE side = 'SELL' AND pnl IS NOT NULL
                    """
                )
            )
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"DB at {path} is missing the orders table: {exc}"
            ) from exc

        cols = (
            "id",
            "ticker",
            "side",
            "quantity",
            "fill_price",
            "fees",
            "filled_at",
            "pnl",
            "strategy_tag",
        )
    finally:
        conn.close()

    sells: list[dict] = []
    for raw in rows:
        record = dict(zip(cols, raw))
        if not _in_window(record.get("filled_at", ""), since, until):
            continue
        sells.append(record)

    overall = _bucket(sells, lambda _r: "overall")

    if overall:
        agg = overall[0]
        total_trades = agg.trades
        realized_pnl = agg.net_pnl
        gross_wins = agg.gross_wins
        gross_losses = agg.gross_losses
        winning_trades = agg.wins
        losing_trades = agg.losses
    else:
        total_trades = 0
        realized_pnl = 0.0
        gross_wins = 0.0
        gross_losses = 0.0
        winning_trades = 0
        losing_trades = 0

    def _by_strategy(row):
        return row.get("strategy_tag") or "untagged"

    def _by_hour(row):
        dt = _parse_iso(row.get("filled_at", ""))
        return dt.hour if dt is not None else -1

    def _by_ticker(row):
        return row.get("ticker") or "unknown"

    return PaperPerformanceReport(
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        realized_pnl=realized_pnl,
        gross_wins=gross_wins,
        gross_losses=gross_losses,
        by_strategy=_bucket(sells, _by_strategy),
        by_hour=_bucket(sells, _by_hour),
        by_ticker=_bucket(sells, _by_ticker),
        evaluation_window=EvaluationWindow(start=since, end=until),
    )


def _row(bucket: BucketAggregate, attrs: Sequence[str]) -> str:
    values = [getattr(bucket, attr) for attr in attrs]
    if all(isinstance(v, (int, float)) for v in values):
        return " ".join(f"{v:>8}" for v in values)
    return " ".join(f"{str(v):>{len(str(v)) + 4}}" for v in values)


def format_paper_performance_report(report: PaperPerformanceReport) -> str:
    """Render a ``PaperPerformanceReport`` as a CLI-friendly summary."""
    lines: list[str] = []
    win = report.evaluation_window
    win_label = "all-time"
    if win.start or win.end:
        win_label = f"{win.start.isoformat() if win.start else 'open'} → "
        win_label += win.end.isoformat() if win.end else "open"

    lines.append("=" * 78)
    lines.append(f"PAPER PERFORMANCE — {win_label}")
    lines.append("=" * 78)
    if report.total_trades == 0:
        lines.append("No closed trades in window.")
        return "\n".join(lines)

    pf_label = (
        f"{report.profit_factor:.2f}"
        if report.profit_factor != float("inf")
        else "inf"
    )
    lines.append(
        f"Overall: trades={report.total_trades} "
        f"win_rate={report.win_rate:.1%} "
        f"net=${report.realized_pnl:+.2f} "
        f"avg_trade=${report.avg_pnl_per_trade:+.2f} "
        f"PF={pf_label}"
    )
    lines.append(
        f"          wins={report.gross_wins:+.2f} "
        f"losses={report.gross_losses:+.2f} "
        f"win_count={report.winning_trades} loss_count={report.losing_trades}"
    )
    lines.append("")

    if report.by_strategy:
        lines.append("By strategy (worst first):")
        lines.append(
            f"  {'strategy':<28} {'N':>4} {'W':>4} {'L':>4} {'net':>11} {'avg':>9} {'PF':>7}"
        )
        for row in report.by_strategy:
            pf = (
                f"{row.profit_factor:.2f}"
                if row.profit_factor != float("inf")
                else "inf"
            )
            lines.append(
                f"  {row.label[:28]:<28} {row.trades:>4} "
                f"{row.wins:>4} {row.losses:>4} "
                f"${row.net_pnl:>+10.2f} ${row.avg_pnl_per_trade:>+8.2f} "
                f"{pf:>6}"
            )
        lines.append("")

    if report.by_hour:
        lines.append("By hour (UTC, worst first):")
        lines.append(
            f"  {'hour':>5} {'N':>4} {'W':>4} {'L':>4} {'net':>11} {'avg':>9}"
        )
        for row in report.by_hour:
            lines.append(
                f"  {row.label:>5} {row.trades:>4} "
                f"{row.wins:>4} {row.losses:>4} "
                f"${row.net_pnl:>+10.2f} ${row.avg_pnl_per_trade:>+8.2f}"
            )
        lines.append("")

    if report.by_ticker:
        losers = sorted(
            (r for r in report.by_ticker if r.net_pnl < 0),
            key=lambda r: r.net_pnl,
        )[:5]
        if losers:
            lines.append("Top 5 losers by ticker:")
            lines.append(
                f"  {'ticker':<8} {'N':>3} {'net':>11} {'avg':>9} {'PF':>7}"
            )
            for row in losers:
                pf = (
                    f"{row.profit_factor:.2f}"
                    if row.profit_factor != float("inf")
                    else "inf"
                )
                lines.append(
                    f"  {row.label:<8} {row.trades:>3} "
                    f"${row.net_pnl:>+10.2f} ${row.avg_pnl_per_trade:>+8.2f} "
                    f"{pf:>6}"
                )

    lines.append("=" * 78)
    return "\n".join(lines)
