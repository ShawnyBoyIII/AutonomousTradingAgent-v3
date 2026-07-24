"""
Phase 1 Monitoring Dashboard - V3 Shadow Mode
Read-only dashboard for portfolio monitoring with real-time updates.
"""

import asyncio
import json
import logging
import math
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Make repo root importable so `trading_bot.*` resolves regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trading_bot.config.loader import load_settings
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.monitoring.health import check_system_health
from trading_bot.monitoring.performance import calculate_performance_metrics
from trading_bot.safety.kill_switch import (
    is_trading_halted,
    halt_trading,
    resume_trading,
)


class DashboardState:
    """Holds settings, ledger, and a connection list for SSE subscribers."""

    def __init__(self) -> None:
        self.settings = load_settings()
        self.ledger = PortfolioLedger(Path(self.settings.app.state_db_path))


state = DashboardState()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    mode = "LIVE" if state.settings.app.live_trading_enabled else "PAPER"
    print(f"🚀 Dashboard starting · mode={mode} · db={state.settings.app.state_db_path}")
    yield
    print("👋 Dashboard shutting down...")


app = FastAPI(
    title="Trading Bot Dashboard",
    description="Phase 1: Read-only monitoring for Shadow Mode",
    version="3.0.0",
    lifespan=lifespan,
)

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_position_marks(symbols: list[str]) -> dict[str, dict[str, float]]:
    """Best-effort live marks for the listed symbols.

    Falls back to ``{}`` on any provider error so the dashboard API can
    always render positions even when the live market-data path is slow
    or unavailable.
    """
    try:
        import concurrent.futures as _cf
        import pandas as _pd
        from trading_bot.data.providers.yfinance_provider import YFinanceProvider

        provider = YFinanceProvider()
    except Exception:
        return {}

    def _fetch_one(symbol: str):
        try:
            frame = provider.fetch_bars(symbol, "5d", "1d")
        except Exception:
            return None
        if frame is None or frame.empty or "close" not in getattr(frame, "columns", _pd.Index([])):
            return None
        try:
            price = float(frame.iloc[-1]["close"])
        except Exception:
            return None
        if _pd.notna(price) and math.isfinite(price):
            return price
        return None

    out: dict[str, dict[str, float]] = {}
    if not symbols:
        return out
    try:
        with _cf.ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as pool:
            futures = {pool.submit(_fetch_one, s): s for s in symbols}
            for fut in _cf.as_completed(futures, timeout=6):
                sym = futures[fut]
                try:
                    price = fut.result()
                except Exception:
                    continue
                if price is not None:
                    out[sym] = {"current_price": price}
    except _cf.TimeoutError:
        return out
    return out


def _position_market_snapshot(pstate) -> dict[str, dict[str, float]]:
    symbols = sorted(pstate.positions.keys())
    if not symbols:
        return {}
    try:
        return _load_position_marks(symbols)
    except Exception:
        return {}


def _portfolio_payload() -> dict:
    """Snapshot the current portfolio as a JSON-friendly dict."""
    pstate = state.ledger.load_portfolio_state()
    if pstate is None:
        # Initialise so the dashboard never crashes on a fresh DB.
        pstate = state.ledger.ensure_portfolio_state(
            starting_cash=float(getattr(state.settings.app, "starting_cash", 100_000.0))
            if hasattr(state.settings.app, "starting_cash")
            else 100_000.0
        )

    marks = _position_market_snapshot(pstate)
    marks_loaded = bool(marks)
    positions = []
    total_basis = 0.0
    total_unrealized_pnl = 0.0
    winners = 0
    losers = 0
    for ticker, pos in pstate.positions.items():
        qty = int(pos.quantity)
        avg = float(pos.average_cost)
        live_mark = marks.get(ticker, {}).get("current_price")
        mark_is_live = live_mark is not None
        current = float(live_mark) if mark_is_live else avg
        market_value = qty * current
        basis = qty * avg
        unrealized = market_value - basis
        unrealized_pct = (unrealized / basis) if basis > 0 else 0.0
        if unrealized > 0:
            winners += 1
        elif unrealized < 0:
            losers += 1
        total_basis += basis
        total_unrealized_pnl += unrealized
        positions.append({
            "symbol": ticker,
            "quantity": qty,
            "avg_cost": avg,
            "current_price": current,
            "market_value": market_value,
            "unrealized_pnl": unrealized,
            "unrealized_pct": unrealized_pct,
            "mark_is_live": mark_is_live,
        })

    total_unrealized_pct = (total_unrealized_pnl / total_basis) if total_basis > 0 else 0.0

    return {
        "cash": float(pstate.cash),
        "equity": float(pstate.equity),
        "starting_equity": _resolve_starting_equity(pstate),
        "positions": positions,
        "position_count": len(positions),
        "unrealized_pnl": total_unrealized_pnl,
        "total_unrealized_pnl": total_unrealized_pnl,
        "total_unrealized_pct": total_unrealized_pct,
        "winning_positions": winners,
        "losing_positions": losers,
        "marks_loaded": marks_loaded,
        "timestamp": datetime.now().isoformat(),
    }


def _resolve_starting_equity(pstate) -> float:
    """Compute the cohort starting equity used for P&L baseline.

    Priority:
    1. ``settings.paper.equity_evaluation_since`` (the dedicated
       equity-risk boundary) paired with the oldest cohort-equity
       snapshot at or after that boundary.
    2. ``settings.paper.graduation_since`` as a fallback.
    3. ``settings.app.starting_cash`` / ``settings.paper.starting_cash``.
    4. The portfolio's current equity (freshly seeded cohorts).

    Legacy naive ``timestamp`` rows in ``equity_history`` are
    interpreted in ``settings.app.timezone`` so the baseline matches
    every other consumer in the analytics layer.
    """
    settings = state.settings
    equity_boundary = getattr(settings.paper, "equity_evaluation_since", None)
    if equity_boundary is None:
        equity_boundary = getattr(settings.paper, "graduation_since", None)
    if equity_boundary is not None:
        try:
            history = state.ledger.list_recent_equity_history(
                limit=None,
                since=equity_boundary,
                naive_timezone=getattr(settings.app, "timezone", None) or "UTC",
            )
            if history:
                first = history[0]
                eq = float(first.get("equity", 0.0) or 0.0)
                if eq > 0:
                    return eq
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read cohort equity history: %s", exc)
    try:
        cash_seed = float(
            getattr(settings.app, "starting_cash", None)
            or getattr(settings.paper, "starting_cash", None)
            or 0.0
        )
        if cash_seed > 0:
            return cash_seed
    except (TypeError, ValueError):
        pass
    return float(pstate.equity or 0.0)


def _evaluation_windows_payload() -> dict:
    """Compose the three-window cohort snapshot for the dashboard."""
    from trading_bot.analytics.evaluation_windows import build_evaluation_windows

    try:
        windows = build_evaluation_windows(
            state.settings,
            state.ledger,
        )
        return windows.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not build evaluation windows: %s", exc)
        return {
            "generated_at": datetime.now().isoformat(),
            "error": str(exc),
        }


def _health_payload() -> dict:
    health = check_system_health(state.settings, state.ledger)
    ks = is_trading_halted(state.ledger)

    checks = []
    for name, (status, message) in health.checks.items():
        checks.append({
            "name": name,
            "status": "ok" if status else "critical",
            "message": message,
        })

    return {
        "status": "ok" if health.is_healthy() else "critical",
        "checks": checks,
        "kill_switch": {
            "active": bool(ks.enabled),
            "reason": ks.reason,
            "since": ks.triggered_at.isoformat() if ks.triggered_at else None,
            "triggered_by": ks.triggered_by,
        },
        "timestamp": datetime.now().isoformat(),
    }


def _alerts_payload() -> dict:
    perf = calculate_performance_metrics(state.ledger, days=7)
    alerts: list[dict] = []

    if perf.total_trades > 0:
        if perf.win_rate < 0.40:
            alerts.append({
                "level": "warning",
                "category": "performance",
                "message": f"Win rate below 40%: {perf.win_rate:.1%}",
                "timestamp": datetime.now().isoformat(),
            })
        if perf.profit_factor < 1.0:
            alerts.append({
                "level": "critical",
                "category": "performance",
                "message": f"Profit factor below 1.0: {perf.profit_factor:.2f}",
                "timestamp": datetime.now().isoformat(),
            })
        if perf.max_consecutive_losses >= 5:
            alerts.append({
                "level": "warning",
                "category": "risk",
                "message": f"{perf.max_consecutive_losses} consecutive losses",
                "timestamp": datetime.now().isoformat(),
            })

    ks = is_trading_halted(state.ledger)
    if ks.enabled:
        alerts.append({
            "level": "critical",
            "category": "safety",
            "message": f"Kill switch active: {ks.reason or 'manual'}",
            "timestamp": ks.triggered_at.isoformat() if ks.triggered_at else datetime.now().isoformat(),
        })

    return {
        "alerts": alerts,
        "count": len(alerts),
        "has_critical": any(a["level"] == "critical" for a in alerts),
        "timestamp": datetime.now().isoformat(),
    }


def _trades_payload(limit: int = 20) -> dict:
    try:
        rows = state.ledger.list_recent_order_rows(
            limit=limit,
            naive_timezone=state.settings.app.timezone,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read recent fills: %s", exc)
        rows = []

    trades = [
        {
            "timestamp": row["filled_at"],
            "type": "FILL",
            "symbol": row["ticker"],
            "side": row["side"],
            "quantity": row["quantity"],
            "price": row["fill_price"],
            "fees": row["fees"],
            "pnl": row["pnl"],
            "strategy_tag": row["strategy_tag"],
            "order_id": row["id"],
        }
        for row in rows
    ]

    return {
        "trades": trades,
        "count": len(trades),
        "timestamp": datetime.now().isoformat(),
    }


def _closed_trades_payload(limit: int = 50) -> dict:
    """Lifecycle view of closed round-trip trades from the SQLAlchemy `trades` table.

    Surfaces rich attribution (exit_reason, exit_regime, hold_duration,
    signal_quality, market_regime, supermodel_decision) that the legacy
    dashboard reconstructed by re-pairing BUY+SELL from the `orders` table.

    Falls back to an empty list if the SQLAlchemy session cannot be opened
    (fresh DB, missing tables, etc.) so the dashboard never crashes.
    """
    closed: list[dict] = []
    try:
        from trading_bot.db.session import (
            _make_engine,
            init_db,
            make_session_factory,
            get_session,
        )
        from trading_bot.db.repositories import trades as trades_repo

        # Lazy-init schema; cheap if already present (init_db has its own
        # try/except for column-already-exists ALTERs).
        engine = init_db(state.settings)
        session_factory = make_session_factory(engine)
        with get_session(session_factory) as session:
            rows = trades_repo.get_trades(session, limit=limit)
        for r in rows:
            if r.status != "CLOSED":
                continue
            closed.append({
                "id": r.id,
                "ticker": r.ticker,
                "quantity": int(r.quantity),
                "entry_price": float(r.entry_price),
                "exit_price": float(r.exit_price) if r.exit_price is not None else None,
                "filled_at": r.filled_at.isoformat() if r.filled_at else None,
                "exited_at": r.exited_at.isoformat() if r.exited_at else None,
                "pnl": float(r.pnl) if r.pnl is not None else None,
                "exit_reason": r.exit_reason,
                "exit_strategy": r.exit_strategy,
                "exit_regime": r.exit_regime,
                "market_regime": r.market_regime,
                "hold_duration_minutes": (
                    float(r.hold_duration_minutes)
                    if r.hold_duration_minutes is not None
                    else None
                ),
                "strategy_tag": r.strategy_tag,
                "signal_quality": r.signal_quality,
                "supermodel_decision": r.supermodel_decision,
            })
    except Exception as e:  # noqa: BLE001 - fallback to empty list on DB errors
        return {
            "trades": [],
            "count": 0,
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        }

    return {
        "trades": closed,
        "count": len(closed),
        "timestamp": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "mode": "LIVE" if state.settings.app.live_trading_enabled else "PAPER",
            "version": "3.0.0",
        },
    )


@app.get("/api/portfolio")
async def get_portfolio():
    return _portfolio_payload()


@app.get("/api/health")
async def get_health():
    return _health_payload()


@app.get("/api/alerts")
async def get_alerts():
    return _alerts_payload()


@app.get("/api/trades")
async def get_recent_trades(limit: int = 20):
    return _trades_payload(limit=limit)


@app.get("/api/closed-trades")
async def get_closed_trades(limit: int = 50):
    """Lifecycle view of closed round-trip trades with full attribution.

    Reads from the SQLAlchemy `trades` table — the richest closed-trade
    source, including exit_reason, exit_regime, hold_duration_minutes,
    signal_quality, market_regime, and supermodel_decision.
    """
    return _closed_trades_payload(limit=limit)


@app.get("/api/evaluation-windows")
async def get_evaluation_windows():
    """Three-window cohort snapshot: Today, Trade Cohort, Equity Cohort.

    Each window reports a status envelope (``available``,
    ``state`` ∈ ``ready``/``empty``/``insufficient``/``unconfigured``/
    ``error``), a boundary, and JSON-safe metrics. The legacy $1.27M
    pre-cohort peak is excluded from the equity cohort when the
    dedicated ``equity_evaluation_since`` boundary is configured.
    """
    return _evaluation_windows_payload()


async def event_generator():
    while True:
        try:
            data = {
                "portfolio": _portfolio_payload(),
                "health": _health_payload(),
                "alerts": _alerts_payload(),
                "trades": _trades_payload(),
                "closed_trades": _closed_trades_payload(),
                "evaluation_windows": _evaluation_windows_payload(),
                "timestamp": datetime.now().isoformat(),
            }
            yield f"data: {json.dumps(data)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        await asyncio.sleep(5)


@app.get("/api/stream")
async def stream():
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/kill-switch/halt")
async def kill_switch_halt(reason: str = "Dashboard emergency stop"):
    halt_trading(state.ledger, reason=reason, triggered_by="dashboard")
    return {"status": "halted", "reason": reason}


@app.post("/api/kill-switch/resume")
async def kill_switch_resume():
    resume_trading(state.ledger)
    return {"status": "resumed"}


if __name__ == "__main__":
    import uvicorn
    print("Starting dashboard on http://127.0.0.1:8080")
    print("⚠️  No authentication — local use only (127.0.0.1 bind)")
    uvicorn.run(app, host="127.0.0.1", port=8080)
