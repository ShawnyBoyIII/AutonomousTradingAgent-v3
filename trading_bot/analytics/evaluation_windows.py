"""Shared cohort-evaluation analytics for dashboard and CLI.

Computes Today, Trade Cohort, and Equity Cohort windows from the
ledger. Owns timestamp normalization (aware UTC + naive legacy),
boundary resolution, and JSON-safe serialization so every consumer
reports the same numbers.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from trading_bot.analytics.paper_performance import PaperPerformanceReport
    from trading_bot.config.settings import Settings
    from trading_bot.portfolio.ledger import PortfolioLedger


def normalize_timestamp(
    value: Any, naive_timezone: str | None
) -> datetime | None:
    """Return an aware UTC datetime for ``value`` or ``None`` if unparseable.

    Aware timestamps are converted to UTC. Naive legacy strings are
    interpreted in ``naive_timezone`` (defaults to UTC). Strings
    without timezone info and a missing ``naive_timezone`` are treated
    as UTC. Returns ``None`` for empty, malformed, or null inputs.
    """
    if value is None or value == "":
        return None

    candidate: datetime | None
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if candidate.tzinfo is None:
        if naive_timezone:
            try:
                candidate = candidate.replace(tzinfo=ZoneInfo(naive_timezone))
            except Exception:
                candidate = candidate.replace(tzinfo=timezone.utc)
        else:
            candidate = candidate.replace(tzinfo=timezone.utc)

    return candidate.astimezone(timezone.utc)


def _start_of_trading_day_local(now_utc: datetime, tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    local_now = now_utc.astimezone(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


def _json_safe(value: Any) -> Any:
    """Return a JSON-safe value, scrubbing NaN/Infinity to ``None``."""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if is_dataclass(value):
        return {f.name: _json_safe(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@dataclass
class WindowStatus:
    """Cohort window status envelope."""

    available: bool
    state: str
    detail: str = ""
    boundary: str | None = None
    boundary_source: str | None = None


@dataclass
class TodayMetrics:
    """Metrics restricted to the configured trading date."""

    realized_pnl: float | None = None
    closed_exits: int = 0
    wins: int = 0
    losses: int = 0
    profit_factor: float | None = None
    profit_factor_state: str = "ready"
    average_exit_pnl: float | None = None
    trading_date: str | None = None


@dataclass
class TradeCohortMetrics:
    """Metrics restricted to the trade-quality cohort."""

    realized_pnl: float | None = None
    closed_exits: int = 0
    target_trades: int = 100
    wins: int = 0
    losses: int = 0
    profit_factor: float | None = None
    profit_factor_state: str = "ready"
    average_exit_pnl: float | None = None


@dataclass
class EquityCohortMetrics:
    """Metrics restricted to the equity-risk cohort."""

    starting_equity: float | None = None
    current_equity: float | None = None
    peak_equity: float | None = None
    return_amount: float | None = None
    return_pct: float | None = None
    max_drawdown_pct: float | None = None
    snapshot_count: int = 0
    boundary_source: str | None = None


@dataclass
class EvaluationWindows:
    """Three-window cohort snapshot returned by the analytics layer."""

    generated_at: str
    today: WindowStatus = field(default_factory=lambda: WindowStatus(False, "ready"))
    trade_cohort: WindowStatus = field(default_factory=lambda: WindowStatus(False, "ready"))
    equity_cohort: WindowStatus = field(default_factory=lambda: WindowStatus(False, "ready"))
    today_metrics: TodayMetrics = field(default_factory=TodayMetrics)
    trade_cohort_metrics: TradeCohortMetrics = field(default_factory=TradeCohortMetrics)
    equity_cohort_metrics: EquityCohortMetrics = field(default_factory=EquityCohortMetrics)

    def to_dict(self) -> dict[str, Any]:
        """Recursive JSON-safe serialization. Scrubs NaN/Infinity to None."""
        return _json_safe(asdict(self))


def _populate_trade_window(
    target_metrics: TodayMetrics | TradeCohortMetrics,
    report: PaperPerformanceReport,
    *,
    target_trades: int | None,
) -> None:
    """Copy trade-window fields from the report onto the target dataclass.

    ``profit_factor`` is recomputed from gross values so an infinite PF
    (gross_wins > 0 with zero gross_losses) is represented as
    ``None + profit_factor_state='infinite'`` — JSON cannot encode
    Python's ``math.inf`` so we serialize the state alongside a null.
    """
    wins = int(report.winning_trades)
    losses = int(report.losing_trades)
    closed = wins + losses
    gross_wins = float(report.gross_wins)
    gross_losses = float(report.gross_losses)
    realized = float(report.realized_pnl) if report.realized_pnl is not None else 0.0

    if closed == 0:
        target_metrics.realized_pnl = None
        target_metrics.closed_exits = 0
        target_metrics.wins = 0
        target_metrics.losses = 0
        target_metrics.profit_factor = None
        target_metrics.profit_factor_state = "ready"
        target_metrics.average_exit_pnl = None
    else:
        if gross_losses <= 0 and gross_wins > 0:
            pf: float | None = None
            pf_state = "infinite"
        else:
            pf = gross_wins / gross_losses if gross_losses > 0 else 0.0
            pf_state = "ready"
        target_metrics.realized_pnl = realized
        target_metrics.closed_exits = closed
        target_metrics.wins = wins
        target_metrics.losses = losses
        target_metrics.profit_factor = pf
        target_metrics.profit_factor_state = pf_state
        target_metrics.average_exit_pnl = realized / closed

    if isinstance(target_metrics, TradeCohortMetrics) and target_trades is not None:
        target_metrics.target_trades = target_trades


def build_evaluation_windows(
    settings: Settings,
    ledger: PortfolioLedger,
    *,
    now: datetime | None = None,
    target_trades: int = 100,
) -> EvaluationWindows:
    """Compose the three-window snapshot for the dashboard payload.

    ``now`` is injected so tests can pin the trading date.
    """
    from trading_bot.analytics.paper_performance import (
        PaperPerformanceReport,
        summarize_paper_performance,
    )
    from trading_bot.monitoring.drawdown import compute_drawdown_from_ledger

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    naive_tz = getattr(settings.app, "timezone", None) or "UTC"
    paper = getattr(settings, "paper", None)
    graduation_since = getattr(paper, "graduation_since", None) if paper else None
    equity_evaluation_since = (
        getattr(paper, "equity_evaluation_since", None) if paper else None
    )

    windows = EvaluationWindows(generated_at=now_utc.isoformat())

    today_start = _start_of_trading_day_local(now_utc, naive_tz)
    today_local_date = today_start.astimezone(
        ZoneInfo(naive_tz)
    ).date()

    windows.today_metrics.trading_date = today_local_date.isoformat()
    windows.today = WindowStatus(True, "ready", boundary=today_start.isoformat())
    try:
        today_report = summarize_paper_performance(
            db_path=ledger.db_path,
            since=today_start,
            until=now_utc,
            naive_timezone=naive_tz,
        )
        _populate_trade_window(windows.today_metrics, today_report, target_trades=None)
        if today_report.total_trades == 0:
            windows.today.state = "empty"
    except Exception as exc:
        windows.today = WindowStatus(False, "error", detail=str(exc))

    if graduation_since is None:
        windows.trade_cohort = WindowStatus(False, "unconfigured")
    else:
        windows.trade_cohort = WindowStatus(
            True,
            "ready",
            boundary=graduation_since.isoformat(),
            boundary_source="graduation_since",
        )
        try:
            cohort_report = summarize_paper_performance(
                db_path=ledger.db_path,
                since=graduation_since,
                until=None,
                naive_timezone=naive_tz,
            )
            _populate_trade_window(
                windows.trade_cohort_metrics, cohort_report, target_trades=target_trades
            )
            if cohort_report.total_trades == 0:
                windows.trade_cohort.state = "empty"
        except Exception as exc:
            windows.trade_cohort = WindowStatus(False, "error", detail=str(exc))

    if equity_evaluation_since is None and graduation_since is None:
        windows.equity_cohort = WindowStatus(False, "unconfigured")
    else:
        if equity_evaluation_since is not None:
            effective_boundary = equity_evaluation_since
            source = "equity_evaluation_since"
        else:
            effective_boundary = graduation_since
            source = "graduation_fallback"
        windows.equity_cohort = WindowStatus(
            True,
            "ready",
            boundary=normalize_timestamp(effective_boundary, naive_tz).isoformat()
            if normalize_timestamp(effective_boundary, naive_tz)
            else None,
            boundary_source=source,
        )
        try:
            dd = compute_drawdown_from_ledger(
                ledger,
                since=effective_boundary,
                naive_timezone=naive_tz,
            )
            windows.equity_cohort_metrics.starting_equity = dd.starting_equity
            windows.equity_cohort_metrics.current_equity = dd.current_equity
            windows.equity_cohort_metrics.peak_equity = dd.peak_equity
            windows.equity_cohort_metrics.return_amount = dd.total_return_amount
            windows.equity_cohort_metrics.return_pct = dd.total_return_pct
            windows.equity_cohort_metrics.max_drawdown_pct = dd.max_drawdown_pct
            windows.equity_cohort_metrics.snapshot_count = dd.sample_size
            windows.equity_cohort_metrics.boundary_source = source
            if not dd.sufficient_evidence:
                windows.equity_cohort.state = "insufficient"
                windows.equity_cohort.detail = (
                    f"{dd.sample_size} cohort snapshot(s); need at least 2"
                )
        except Exception as exc:
            windows.equity_cohort = WindowStatus(False, "error", detail=str(exc))

    return windows
