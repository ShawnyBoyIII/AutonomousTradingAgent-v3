import json
from datetime import datetime
import math
from pathlib import Path
import time
import logging

import typer

logger = logging.getLogger(__name__)

from trading_bot.config.loader import load_settings
from trading_bot.data.indicators import add_atr
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.order import OrderRequest
from trading_bot.models.portfolio import PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.portfolio.performance import (
    compute_exposure_ratio,
    compute_position_market_value,
    compute_unrealized_pnl,
)
from trading_bot.reports.exporters import export_csv, export_json
from trading_bot.reports.summaries import build_daily_summary
from trading_bot.runtime import alerts as runtime_alerts
from trading_bot.runtime import session as runtime_session
from trading_bot.runtime.decision_log import append_decision_event
from trading_bot.runtime.latency import frame_last_timestamp, is_stale
from trading_bot.runtime.snapshots import read_recent_decision_rows, write_snapshot
from trading_bot.scout import build_scout_candidates
from trading_bot.strategy.trailing_stop import next_trailing_stop

app = typer.Typer(help="Paper-trading CLI for stocks and ETFs.")


def now_in_zone(timezone: str):
    return runtime_session.now_in_zone(timezone)


def should_eod_exit(now: datetime, settings):
    return runtime_session.should_eod_exit(now, settings)


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(
        None,
        "--config-path",
        help="Path to the YAML config file. Falls back to CONFIG_PATH env var, then config.yaml.",
    ),
) -> None:
    import os

    if config_path is None:
        env_path = os.environ.get("CONFIG_PATH")
        if env_path:
            config_path = Path(env_path)
    ctx.obj = load_settings(config_path)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check local app readiness without fetching market data."""
    typer.echo(_format_doctor(ctx.obj))


@app.command()
def scan(
    ctx: typer.Context,
    symbols: list[str] = typer.Option(
        ...,
        "--symbols",
        help="Symbols to scan for trade candidates.",
    ),
    why: bool = typer.Option(
        False,
        "--why",
        help="Show the gate values behind each scan decision.",
    ),
    summary: bool = typer.Option(
        False,
        "--summary",
        help="Print one summary line after scan results.",
    ),
) -> None:
    """Scan the configured universe for trade candidates."""
    from trading_bot.runtime.orchestrator import run_scan

    parsed_symbols = _parse_symbols(symbols)
    scan_result = run_scan(parsed_symbols, ctx.obj, include_details=why)
    for result in scan_result["lines"]:
        typer.echo(result)
    if summary:
        typer.echo(_format_scan_summary(scan_result["summary"]))


@app.command(name="scan-universe")
def scan_universe(
    ctx: typer.Context,
    why: bool = typer.Option(
        False,
        "--why",
        help="Show the gate values behind each scan decision.",
    ),
    summary: bool = typer.Option(
        False,
        "--summary",
        help="Print one summary line after scan results.",
    ),
    notify: bool = typer.Option(
        False,
        "--notify",
        help="Send Discord alerts for fresh GREEN candidates.",
    ),
    universe_path: Path | None = typer.Option(
        None,
        "--universe-path",
        help="Optional path to a newline or comma separated universe file.",
    ),
) -> None:
    """Scan symbols from the saved universe file."""
    path = universe_path or Path(ctx.obj.app.universe_path)
    symbols = (
        _read_ranked_universe_symbols(ctx.obj)
        if universe_path is None
        else _read_universe_symbols(path)
    )
    if not symbols:
        typer.echo(f"universe=empty path={path}")
        typer.echo("alerts=0")
        return
    _run_scan_command(ctx, symbols, why=why, summary=summary, notify=notify)


@app.command(name="build-universe")
def build_universe(ctx: typer.Context) -> None:
    """Build the saved universe from Yahoo small-cap screeners."""
    result = _build_universe_file(ctx.obj)
    for line in result["lines"]:
        typer.echo(line)


@app.command(name="alert-signals")
def alert_signals(ctx: typer.Context) -> None:
    """Send alerts for the latest fresh GREEN scan candidates."""
    snapshot = _load_json_snapshot(Path(ctx.obj.app.scan_results_path))
    candidates = _approved_alert_candidates(snapshot.get("candidates", []))
    if not candidates:
        typer.echo("alerts=0")
        return
    notify(ctx.obj, "info", "BUY Signal", _format_signal_alert(candidates))
    typer.echo(f"alerts={len(candidates)}")


@app.command(name="burn-in-report")
def burn_in_report(
    ctx: typer.Context,
    log_dir: Path = typer.Option(
        None,
        "--log-dir",
        help="Path to burn-in log directory. Defaults to config log_dir.",
    ),
    db_path: Path = typer.Option(
        None,
        "--db-path",
        help="Path to burn-in SQLite database. Defaults to config state_db_path.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output report as JSON instead of formatted text.",
    ),
) -> None:
    """Generate a comprehensive burn-in analytics report.

    Parses decision-log.jsonl and the SQLite portfolio database to produce
    a full analysis including trade performance, signal quality, risk metrics,
    counter-thesis effectiveness, and actionable recommendations.
    """
    from trading_bot.reports.burn_in_analytics import compute_burn_in_report, format_report

    log_path = log_dir or (Path(ctx.obj.app.log_dir) / "decision-log.jsonl")
    db = db_path or Path(ctx.obj.app.state_db_path)

    report = compute_burn_in_report(log_path, db, settings=ctx.obj)

    if json_output:
        typer.echo(json.dumps(report, default=str, indent=2))
    else:
        typer.echo(format_report(report))


@app.command(name="run-ops")
def run_ops(
    ctx: typer.Context,
    cycles: int = typer.Option(1, "--cycles", min=1, help="Number of ops cycles to run."),
    interval_seconds: int = typer.Option(
        0,
        "--interval-seconds",
        min=0,
        help="Sleep between cycles. Use 0 for a single immediate pass.",
    ),
    build_universe: bool = typer.Option(
        True,
        "--build-universe/--no-build-universe",
        help="Refresh the saved universe before each scan cycle.",
    ),
    why: bool = typer.Option(False, "--why", help="Show scan gate values in the ops scan."),
) -> None:
    """Run scout, scan, notify, and position-management in one local loop."""
    from trading_bot.runtime.orchestrator import run_scan

    for cycle in range(1, cycles + 1):
        typer.echo(f"cycle={cycle} build_universe={'yes' if build_universe else 'no'}")
        symbols: list[str] = []
        if build_universe:
            universe_result = _build_universe_file(ctx.obj)
            for line in universe_result["lines"]:
                typer.echo(line)
            symbols = universe_result["symbols"]
        if not symbols:
            symbols = _read_ranked_universe_symbols(ctx.obj)

        scan_result = run_scan(symbols, ctx.obj, include_details=why)
        for line in scan_result["lines"]:
            typer.echo(line)
        typer.echo(_format_scan_summary(scan_result["summary"]))

        candidates = _approved_alert_candidates(scan_result["candidates"])
        if candidates:
            notify(ctx.obj, "info", "BUY Signal", _format_signal_alert(candidates))
            typer.echo(f"alerts={len(candidates)}")
        else:
            typer.echo("alerts=0")

        manage_result = _run_manage_positions_once(ctx)
        if manage_result["exit_events"]:
            notify(ctx.obj, "warning", "Exit Triggered", _format_exit_alert(manage_result["exit_events"]))
        typer.echo(
            f"cycle={cycle} done symbols={len(symbols)} signals={len(candidates)} "
            f"exits={len(manage_result['exit_events'])}"
        )

        if cycle < cycles and interval_seconds > 0:
            time.sleep(interval_seconds)


@app.command(name="alert-exits")
def alert_exits(ctx: typer.Context) -> None:
    """Send alerts for recent sell fills from the manager."""
    rows = read_recent_decision_rows(Path(ctx.obj.app.log_dir) / "decision-log.jsonl", limit=20)
    events = [
        row
        for row in rows
        if row.get("command") == "manage-positions" and row.get("status") == "FILLED"
    ]
    if not events:
        typer.echo("alerts=0")
        return
    notify(ctx.obj, "warning", "Position Exit", _format_exit_alert(events))
    typer.echo(f"alerts={len(events)}")


@app.command(name="paper-trade")
def paper_trade(
    ctx: typer.Context,
    symbols: list[str] = typer.Option(
        ...,
        "--symbols",
        help="Symbols to trade in paper mode.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview paper trades without writing fills or portfolio state.",
    ),
) -> None:
    """Run the paper-trading loop."""
    from trading_bot.runtime.orchestrator import run_paper_trade

    parsed_symbols: list[str] = []
    for raw_value in symbols:
        parsed_symbols.extend(
            symbol.strip() for symbol in raw_value.split(",") if symbol.strip()
        )

    for result in run_paper_trade(parsed_symbols, ctx.obj, dry_run=dry_run):
        typer.echo(result)


@app.command()
def backtest(
    ctx: typer.Context,
    symbols: list[str] = typer.Option(
        ...,
        "--symbols",
        help="Symbols to replay in backtest mode.",
    ),
    start: str | None = typer.Option(
        None,
        "--start",
        help="Inclusive start date in YYYY-MM-DD format.",
    ),
    end: str | None = typer.Option(
        None,
        "--end",
        help="Inclusive end date in YYYY-MM-DD format.",
    ),
    walk_forward: bool = typer.Option(
        False,
        "--walk-forward",
        help="Run walk-forward analysis across sequential windows.",
    ),
    windows: int = typer.Option(
        10,
        "--windows",
        min=2,
        help="Number of sequential windows for walk-forward.",
    ),
    strategy: str = typer.Option(
        None,
        "--strategy",
        help="Strategy to use: v2.5, v3, or rl.",
    ),
    compare: bool = typer.Option(
        False,
        "--compare",
        help="Compare all available strategies.",
    ),
) -> None:
    """Replay historical data for a strategy.

    Use --walk-forward to split the date range into N sequential windows
    and run an independent backtest on each. Consistent performance across
    all windows = robust strategy. High variance = fragile / overfit.

    Use --strategy to select v2.5, v3, or rl. Use --compare to run all
    available strategies and show a side-by-side comparison.
    """
    parsed_symbols = _parse_symbols(symbols)

    if walk_forward:
        from trading_bot.backtest.runner import run_walk_forward
        summary = run_walk_forward(parsed_symbols, ctx.obj, start=start, end=end, windows=windows)
        typer.echo(
            " ".join([
                f"trades={summary['trades']}",
                f"wins={summary['wins']}",
                f"win_rate={summary['win_rate']:.2f}",
                f"net_pnl={summary['net_pnl']:.2f}",
                f"windows={len(summary.get('windows', []))}",
            ])
        )
        for w in summary.get("windows", []):
            typer.echo(
                f"  window={w['window']} start={w['start']} end={w['end']} "
                f"trades={w['trades']} wins={w['wins']} "
                f"win_rate={w['win_rate']:.2f} net_pnl={w['net_pnl']}"
            )
    elif compare:
        from trading_bot.backtest.runner import run_strategy_comparison
        comparison = run_strategy_comparison(parsed_symbols, ctx.obj, start=start, end=end)
        typer.echo("STRATEGY COMPARISON")
        typer.echo("=" * 60)
        for strat, result in comparison["results"].items():
            typer.echo(f"\n{strat.upper()}:")
            typer.echo(f"  trades={result['trades']} wins={result['wins']} losses={result['losses']}")
            typer.echo(f"  win_rate={result['win_rate']:.2f} net_pnl={result['net_pnl']:.2f}")
        typer.echo(f"\nBest P&L: {comparison['best_pnl_strategy']}")
        typer.echo(f"Best Win Rate: {comparison['best_winrate_strategy']}")
    elif strategy:
        if strategy == "rl":
            from trading_bot.backtest.runner import run_rl_backtest
            summary = run_rl_backtest(parsed_symbols, ctx.obj, start=start, end=end)
        else:
            from trading_bot.backtest.runner import run_backtest
            summary = run_backtest(parsed_symbols, ctx.obj, start=start, end=end)
        typer.echo(
            " ".join(
                [
                    f"trades={summary['trades']}",
                    f"wins={summary['wins']}",
                    f"win_rate={summary['win_rate']:.2f}",
                    f"net_pnl={summary['net_pnl']:.2f}",
                ]
            )
        )
    else:
        from trading_bot.backtest.runner import run_backtest
        summary = run_backtest(parsed_symbols, ctx.obj, start=start, end=end)
        typer.echo(
            " ".join(
                [
                    f"trades={summary['trades']}",
                    f"wins={summary['wins']}",
                    f"win_rate={summary['win_rate']:.2f}",
                    f"net_pnl={summary['net_pnl']:.2f}",
                ]
            )
        )


@app.command()
def dashboard(
    ctx: typer.Context,
    output: Path = typer.Option(
        Path("state/dashboard.html"),
        "--output",
        help="Path for static HTML dashboard.",
    ),
) -> None:
    """Build a static dashboard from local JSON snapshots."""
    from trading_bot.runtime.dashboard import build_dashboard

    path = build_dashboard(ctx.obj, output)
    typer.echo(f"dashboard={path}")


@app.command()
def serve(
    ctx: typer.Context,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. Defaults to localhost (127.0.0.1) for security.",
    ),
    port: int = typer.Option(
        8000,
        "--port",
        help="Port to serve the live dashboard on.",
    ),
) -> None:
    """Serve a live, auto-refreshing dashboard from local state files.

    Binds to localhost only (127.0.0.1) by default per the security
    hardening policy. The dashboard reads state JSON + the burn-in
    decision-log JSONL every refresh and never writes to them.

    Press Ctrl-C to stop.
    """
    from trading_bot.runtime.dashboard import serve_dashboard

    typer.echo(f"Serving live dashboard at http://{host}:{port} (Ctrl-C to stop)")
    typer.echo("Routes: / (HTML) | /api/state (JSON) | /healthz")
    serve_dashboard(ctx.obj, host=host, port=port, block=True)


@app.command(name="manage-positions")
def manage_positions(ctx: typer.Context) -> None:
    """Run one position-management check."""
    _run_manage_positions_once(ctx)


@app.command(name="run-manager")
def run_manager(
    ctx: typer.Context,
    interval: int = typer.Option(
        60,
        "--interval",
        help="Seconds between manage-positions runs. Use 0 for a tight loop.",
    ),
    max_failures: int = typer.Option(
        5,
        "--max-failures",
        help="Maximum consecutive failures before circuit breaker opens.",
    ),
) -> None:
    """Continuously run manage-positions on an interval until interrupted.

    Press Ctrl-C (SIGINT) to stop. The loop runs `_run_manage_positions_once`
    every `--interval` seconds. Each iteration is independent: it hydrates
    the latest portfolio state, exits stale or stale-data-skipped positions,
    and persists any sells to SQLite before sleeping.

    Circuit breaker: after `--max-failures` consecutive exceptions, the daemon
    exits with an error code. This prevents infinite crash loops.
    """
    typer.echo(f"run-manager started interval={max(interval, 0)}s max_failures={max_failures}")
    consecutive_failures = 0
    try:
        while True:
            try:
                _run_manage_positions_once(ctx)
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                logger.error(f"iteration_failed failures={consecutive_failures} error={e}")
                if consecutive_failures >= max_failures:
                    logger.critical("circuit_breaker_open")
                    typer.echo(f"circuit breaker open after {max_failures} failures, exiting")
                    raise typer.Exit(code=1)
                # Exponential backoff before retry
                backoff = min(2 ** (consecutive_failures - 1), 30)
                logger.info(f"backing_off seconds={backoff}")
                time.sleep(backoff)
                continue
            if interval > 0:
                time.sleep(interval)
            else:
                time.sleep(0.1)
    except KeyboardInterrupt:
        typer.echo("run-manager stopped")


@app.command(name="continuous")
def continuous(
    ctx: typer.Context,
    interval: int = typer.Option(
        300,
        "--interval",
        help="Seconds between full loop cycles (scan + trade + manage).",
    ),
    cycles: int = typer.Option(
        0,
        "--cycles",
        min=0,
        help="Maximum cycles to run. Use 0 for infinite.",
    ),
    build_universe: bool = typer.Option(
        True,
        "--build-universe/--no-build-universe",
        help="Refresh the universe before each scan cycle.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview trades without executing fills.",
    ),
    max_failures: int = typer.Option(
        10,
        "--max-failures",
        help="Consecutive failures before circuit breaker opens.",
    ),
    event_system: bool = typer.Option(
        False,
        "--event-system",
        help="Wire loop events through the event bus.",
    ),
) -> None:
    """Run the full paper-trading loop continuously.

    Each cycle performs:
    1. Build universe (optional)
    2. Scan watchlist for signals
    3. Execute paper trades on approved signals
    4. Manage existing positions (exits, stops, targets)
    5. Sleep for interval

    Press Ctrl-C (SIGINT) to stop gracefully. The loop tracks statistics
    and exits after --max-failures consecutive errors to prevent crash loops.
    """
    from trading_bot.runtime.continuous_loop import run_continuous_loop

    typer.echo(
        f"continuous_loop_start interval={interval}s cycles={'inf' if cycles == 0 else cycles} "
        f"build_universe={'yes' if build_universe else 'no'} dry_run={'yes' if dry_run else 'no'}"
    )

    try:
        stats = run_continuous_loop(
            settings=ctx.obj,
            interval_seconds=interval,
            max_cycles=cycles if cycles > 0 else None,
            build_universe=build_universe,
            dry_run=dry_run,
            max_failures=max_failures,
            use_event_system=event_system,
        )
        typer.echo(f"loop_stopped stats={json.dumps(stats.summary(), default=str)}")
    except KeyboardInterrupt:
        typer.echo("continuous_loop_stopped_by_user")


def _run_manage_positions_once(ctx: typer.Context) -> dict[str, object]:
    """Run one position-management check (EOD, stop, target, trail).

    Returns a dict with *positions*, *actions*, *lines*, and *exit_events*
    so callers (e.g. ``run-ops``) can alert without re-echoing.
    """
    from trading_bot.data import market_data
    from trading_bot.safety.kill_switch import check_kill_switch_before_trade

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.ensure_portfolio_state()

    # V2.5: Check kill switch
    allowed, reason = check_kill_switch_before_trade(ledger)
    if not allowed:
        typer.echo(f"KILL_SWITCH: {reason}")
        return {"positions": 0, "actions": 0, "lines": [], "exit_events": []}

    # V3.1: Circuit breaker — auto-halt on consecutive losses / max drawdown
    from trading_bot.safety.circuit_breaker import check_circuit_breakers

    cb_allowed, cb_reason = check_circuit_breakers(ledger, ctx.obj)
    if not cb_allowed:
        typer.echo(f"CIRCUIT_BREAKER: {cb_reason}")
        return {"positions": 0, "actions": 0, "lines": [], "exit_events": []}

    broker = _paper_broker_from_state(state, ctx.obj)
    log_path = Path(ctx.obj.app.log_dir) / "decision-log.jsonl"
    manage_now = now_in_zone(ctx.obj.app.timezone)
    eod_active = should_eod_exit(manage_now, ctx.obj.session)
    lines: list[str] = []
    actions = 0
    skipped_stale = 0
    exit_events: list[dict[str, object]] = []
    for ticker, position in sorted(state.positions.items()):
        # Use intraday bars for responsive trailing stop management
        frame = market_data.fetch_bars(
            ticker,
            ctx.obj.market_data.intraday_period,
            ctx.obj.market_data.intraday_interval,
        )
        last_timestamp = frame_last_timestamp(frame)
        last_price: float | None = None
        if not frame.empty and "close" in frame.columns:
            last_price = float(frame.iloc[-1]["close"])
        if is_stale(last_timestamp, manage_now, max_age_minutes=ctx.obj.market_data.max_data_age_minutes):
            skipped_stale += 1
            append_decision_event(
                log_path,
                {
                    "command": "manage-positions",
                    "ticker": ticker,
                    "status": "SKIP",
                    "reason": "stale market data",
                    "last_timestamp": last_timestamp.isoformat() if last_timestamp else None,
                    "managed_at": manage_now.isoformat(),
                    "max_age_hours": ctx.obj.market_data.max_data_age_hours,
                },
            )
            lines.append(
                f"{ticker} SKIP reason=stale-data last={'unknown' if last_price is None else f'{last_price:.2f}'}"
            )
            continue
        if last_price is None:
            skipped_stale += 1
            append_decision_event(
                log_path,
                {
                    "command": "manage-positions",
                    "ticker": ticker,
                    "status": "SKIP",
                    "reason": "missing market data",
                    "managed_at": manage_now.isoformat(),
                },
            )
            lines.append(f"{ticker} SKIP reason=stale-data last=unknown")
            continue
        if eod_active:
            state, event, line = _fill_sell_position(
                ticker, position, "eod", manage_now, last_price,
                broker, ledger, state, log_path,
            )
            append_decision_event(log_path, event)
            exit_events.append(event)
            actions += 1
            lines.append(line)
            continue
        if position.stop_loss is not None and last_price <= position.stop_loss:
            state, event, line = _fill_sell_position(
                ticker, position, "stop", manage_now, last_price,
                broker, ledger, state, log_path,
            )
            append_decision_event(log_path, event)
            exit_events.append(event)
            actions += 1
            lines.append(line)
            continue
        if position.profit_target is not None and last_price >= position.profit_target:
            state, event, line = _fill_sell_position(
                ticker, position, "target", manage_now, last_price,
                broker, ledger, state, log_path,
            )
            append_decision_event(log_path, event)
            exit_events.append(event)
            actions += 1
            lines.append(line)
            continue
        # V3: Counter-thesis exit — original BUY thesis broken, exit early.
        # Priority: EOD > stop > target > counter-thesis > trailing stop.
        counter_exit = _maybe_counter_thesis_exit(
            ticker, position, frame, last_price, ctx.obj, manage_now,
            broker, ledger, state, log_path,
        )
        if counter_exit is not None:
            state, event, line = counter_exit
            append_decision_event(log_path, event)
            exit_events.append(event)
            actions += 1
            lines.append(line)
            continue
        trail_update = _update_trailing_stop(position, frame, last_price, ctx.obj)
        if trail_update is not None:
            new_stop, method, new_highest_high, new_initial_risk = trail_update
            state.positions[ticker] = position.model_copy(
                update={
                    "stop_loss": new_stop,
                    "highest_high": new_highest_high,
                    "initial_risk": new_initial_risk,
                }
            )
            ledger.save_portfolio_state(state)
            ledger.record_equity_snapshot(state, timestamp=manage_now)
            append_decision_event(
                log_path,
                {
                    "command": "manage-positions",
                    "ticker": ticker,
                    "status": "TRAIL",
                    "method": method,
                    "old_stop": position.stop_loss,
                    "new_stop": new_stop,
                    "last_price": last_price,
                    "highest_high": new_highest_high,
                    "initial_risk": new_initial_risk,
                },
            )
            actions += 1
            lines.append(
                f"{ticker} TRAIL method={method} stop={new_stop:.2f} "
                f"last={last_price:.2f} high={new_highest_high:.2f}"
            )
            continue
        lines.append(
            f"{ticker} qty={position.quantity} "
            f"avg={position.average_cost:.2f} last={last_price:.2f}"
        )
    typer.echo(
        f"positions={len(state.positions)} actions={actions} skipped={skipped_stale}"
    )
    for line in lines:
        typer.echo(line)
    portfolio_view = _build_portfolio_view(state, ctx.obj)
    write_snapshot(
        ctx.obj.app.portfolio_summary_path,
        {
            "mode": "portfolio",
            "summary": {
                "cash": round(state.cash, 2),
                "equity": portfolio_view["equity"],
                "realized_pnl": round(state.realized_pnl, 2),
                "unrealized_pnl": portfolio_view["unrealized_pnl"],
                "exposure": portfolio_view["exposure"],
                "positions": len(state.positions),
            },
            "positions": portfolio_view["positions"],
        },
    )

    return {
        "positions": len(state.positions),
        "actions": actions,
        "lines": lines,
        "exit_events": exit_events,
    }


@app.command()
def report(
    ctx: typer.Context,
    json_path: Path | None = typer.Option(
        None,
        "--json-path",
        help="Optional path to export the summary as JSON.",
    ),
    csv_path: Path | None = typer.Option(
        None,
        "--csv-path",
        help="Optional path to export order rows as CSV.",
    ),
) -> None:
    """Print a performance summary."""
    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    orders = ledger.list_order_rows()
    portfolio_view = _build_portfolio_view(state, ctx.obj)
    summary = build_daily_summary(
        realized_pnl=state.realized_pnl,
        unrealized_pnl=portfolio_view["unrealized_pnl"],
        open_positions=len(state.positions),
    )
    summary["exposure"] = portfolio_view["exposure"]
    summary["orders"] = len(orders)

    if json_path is not None:
        export_json(summary, json_path)
    if csv_path is not None:
        export_csv(orders, csv_path)

    write_snapshot(
        ctx.obj.app.dashboard_summary_path,
        {
            "mode": "report",
            "summary": summary,
            "positions": portfolio_view["positions"],
            "rows": orders,
            "recent_decisions": read_recent_decision_rows(Path(ctx.obj.app.log_dir) / "decision-log.jsonl"),
        },
    )

    typer.echo(
        " ".join(
            [
                f"net_pnl={summary['net_pnl']:.2f}",
                f"realized_pnl={summary['realized_pnl']:.2f}",
                f"unrealized_pnl={summary['unrealized_pnl']:.2f}",
                f"open_positions={summary['open_positions']}",
                f"exposure={summary['exposure']:.2f}",
                f"orders={summary['orders']}",
            ]
        )
    )


@app.command()
def portfolio(ctx: typer.Context) -> None:
    """Inspect the current simulated portfolio."""
    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    portfolio_view = _build_portfolio_view(state, ctx.obj)
    portfolio_summary = {
        "cash": round(state.cash, 2),
        "equity": portfolio_view["equity"],
        "realized_pnl": round(state.realized_pnl, 2),
        "unrealized_pnl": portfolio_view["unrealized_pnl"],
        "exposure": portfolio_view["exposure"],
        "positions": len(state.positions),
    }
    write_snapshot(
        ctx.obj.app.portfolio_summary_path,
        {
            "mode": "portfolio",
            "summary": portfolio_summary,
            "positions": portfolio_view["positions"],
        },
    )
    typer.echo(
        " ".join(
            [
                f"cash={portfolio_summary['cash']:.2f}",
                f"equity={portfolio_view['equity']:.2f}",
                f"realized_pnl={state.realized_pnl:.2f}",
                f"unrealized_pnl={portfolio_view['unrealized_pnl']:.2f}",
                f"exposure={portfolio_view['exposure']:.2f}",
                f"positions={len(state.positions)}",
            ]
        )
    )
    for row in portfolio_view["positions"]:
        typer.echo(
            " ".join(
                [
                    row["ticker"],
                    f"qty={row['quantity']}",
                    f"avg={row['average_cost']:.2f}",
                    f"last={row['last_price']:.2f}",
                    f"mv={row['market_value']:.2f}",
                    f"upl={row['unrealized_pnl']:.2f}",
                    f"alloc={row['allocation']:.2f}",
                ]
            )
        )


@app.command(name="paper-audit")
def paper_audit(ctx: typer.Context) -> None:
    """Check local paper-mode state for obvious drift."""
    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    orders = ledger.list_order_rows()
    equity_history = ledger.list_equity_history(limit=1)
    snapshot = _load_json_snapshot(Path(ctx.obj.app.portfolio_summary_path))
    issues = _collect_paper_audit_issues(state, orders, equity_history, snapshot)

    typer.echo(
        " ".join(
            [
                f"paper_audit={'PASS' if not issues else 'FAIL'}",
                f"orders={len(orders)}",
                f"positions={len(state.positions)}",
                f"equity_snapshots={len(equity_history)}",
                f"snapshot={'yes' if snapshot else 'no'}",
            ]
        )
    )
    for issue in issues:
        typer.echo(f"- {issue}")
    if issues:
        raise typer.Exit(code=1)


def _build_portfolio_view(state: PortfolioState, settings) -> dict[str, object]:
    latest_prices = _fetch_latest_prices(sorted(state.positions), settings)
    position_rows: list[dict[str, object]] = []
    total_market_value = 0.0
    unrealized_pnl = 0.0

    for ticker, position in sorted(state.positions.items()):
        last_price = latest_prices.get(ticker, position.average_cost)
        market_value = compute_position_market_value(position.quantity, last_price)
        position_upl = compute_unrealized_pnl(
            position.quantity,
            position.average_cost,
            last_price,
        )
        total_market_value += market_value
        unrealized_pnl += position_upl
        position_rows.append(
            {
                "ticker": ticker,
                "quantity": position.quantity,
                "average_cost": position.average_cost,
                "last_price": last_price,
                "market_value": market_value,
                "unrealized_pnl": position_upl,
            }
        )

    equity = round(state.cash + total_market_value, 2)
    for row in position_rows:
        row["allocation"] = round(
            compute_exposure_ratio(row["market_value"], equity),
            2,
        )

    return {
        "equity": equity,
        "unrealized_pnl": round(unrealized_pnl, 2),
        "exposure": round(compute_exposure_ratio(total_market_value, equity), 2),
        "positions": position_rows,
    }


def _collect_paper_audit_issues(
    state: PortfolioState,
    orders: list[dict[str, object]],
    equity_history: list[dict[str, object]],
    snapshot: dict[str, object],
) -> list[str]:
    issues: list[str] = []
    if snapshot:
        summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
        positions = snapshot.get("positions") if isinstance(snapshot.get("positions"), list) else []
        if round(float(summary.get("cash", state.cash)), 2) != round(state.cash, 2):
            issues.append("portfolio snapshot cash does not match ledger state")
        if int(summary.get("positions", len(state.positions))) != len(state.positions):
            issues.append("portfolio snapshot position count does not match ledger state")

        snapshot_positions = {
            str(row.get("ticker")): row
            for row in positions
            if isinstance(row, dict) and str(row.get("ticker", "")).strip()
        }
        if set(snapshot_positions) != set(state.positions):
            issues.append("portfolio snapshot tickers do not match ledger state")
        for ticker, position in state.positions.items():
            row = snapshot_positions.get(ticker)
            if row is None:
                continue
            if int(row.get("quantity", position.quantity)) != position.quantity:
                issues.append(f"portfolio snapshot quantity mismatch for {ticker}")
            if round(float(row.get("average_cost", position.average_cost)), 2) != round(position.average_cost, 2):
                issues.append(f"portfolio snapshot average_cost mismatch for {ticker}")
    elif orders or state.positions:
        issues.append("portfolio snapshot missing; run `portfolio` to refresh local paper snapshot")

    if orders and not equity_history:
        issues.append("equity history missing despite recorded paper orders")
    elif equity_history:
        latest = equity_history[-1]
        if round(float(latest.get("cash", state.cash)), 2) != round(state.cash, 2):
            issues.append("latest equity snapshot cash does not match ledger state")
        if round(float(latest.get("equity", state.equity)), 2) != round(state.equity, 2):
            issues.append("latest equity snapshot equity does not match ledger state")
        if round(float(latest.get("realized_pnl", state.realized_pnl)), 2) != round(state.realized_pnl, 2):
            issues.append("latest equity snapshot realized_pnl does not match ledger state")
        if round(float(latest.get("unrealized_pnl", state.unrealized_pnl)), 2) != round(state.unrealized_pnl, 2):
            issues.append("latest equity snapshot unrealized_pnl does not match ledger state")

    return issues


def _run_scan_command(
    ctx: typer.Context,
    symbols: list[str],
    *,
    why: bool,
    summary: bool,
    notify: bool,
) -> None:
    from trading_bot.runtime.orchestrator import run_scan

    parsed_symbols = _parse_symbols(symbols)
    scan_result = run_scan(parsed_symbols, ctx.obj, include_details=why)
    for result in scan_result["lines"]:
        typer.echo(result)
    if summary:
        typer.echo(_format_scan_summary(scan_result["summary"]))
    if notify:
        candidates = _approved_alert_candidates(scan_result["candidates"])
        if not candidates:
            typer.echo("alerts=0")
            return
        notify(ctx.obj, "info", "BUY Signal", _format_signal_alert(candidates))
        typer.echo(f"alerts={len(candidates)}")


def _parse_symbols(values: list[str]) -> list[str]:
    parsed_symbols: list[str] = []
    for raw_value in values:
        parsed_symbols.extend(symbol.strip() for symbol in raw_value.split(",") if symbol.strip())
    return parsed_symbols


def _build_universe_file(settings) -> dict[str, object]:
    from trading_bot.data import market_data

    fetch_limit = max(settings.scout.max_universe_size, settings.scout.max_snapshot_candidates)
    rows = market_data.fetch_small_cap_candidates(
        limit=fetch_limit,
        screeners=settings.scout.screeners,
    )
    scout_result = build_scout_candidates(rows, settings.scout)
    included_symbols = scout_result["included_symbols"]
    lines = [
        " ".join(
            [
                str(candidate["rank"]),
                str(candidate["ticker"]),
                f"score={float(candidate['scout_score']):.2f}",
                f"price={float(candidate['price'] or 0.0):.2f}",
                f"market_cap={int(candidate['market_cap'] or 0)}",
                f"avg_dollar_volume={float(candidate['avg_dollar_volume']):.2f}",
                f"source_hits={int(candidate['source_hits'])}",
                f"reasons={'; '.join(candidate['reasons'])}",
            ]
        )
        for candidate in scout_result["candidates"]
        if candidate["included"]
    ]

    path = Path(settings.app.universe_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text("".join(f"{symbol}\n" for symbol in included_symbols), encoding="utf-8")
    tmp_path.replace(path)

    snapshot_limit = max(settings.scout.max_universe_size, settings.scout.max_snapshot_candidates)
    write_snapshot(
        settings.app.universe_candidates_path,
        {
            "mode": "universe",
            "summary": scout_result["summary"],
            "candidates": scout_result["candidates"][:snapshot_limit],
        },
    )
    lines.append(
        f"summary candidates={scout_result['summary']['candidates']} "
        f"included={scout_result['summary']['included']} "
        f"excluded={scout_result['summary']['excluded']} "
        f"errors={scout_result['summary']['errors']} path={path}"
    )
    return {"lines": lines, "symbols": included_symbols}


def _read_universe_symbols(path: Path) -> list[str]:
    if not path.exists():
        return []
    values: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        values.extend(symbol.strip() for symbol in line.split(",") if symbol.strip())
    return values


def _read_ranked_universe_symbols(settings) -> list[str]:
    snapshot = _load_json_snapshot(Path(settings.app.universe_candidates_path))
    candidates = snapshot.get("candidates", [])
    if isinstance(candidates, list):
        ranked = [
            row
            for row in candidates
            if isinstance(row, dict)
            and row.get("included") is True
            and str(row.get("ticker", "")).strip()
        ]
        if ranked:
            ranked.sort(key=lambda row: int(row.get("rank") or 999999))
            return [str(row["ticker"]).strip() for row in ranked]
    return _read_universe_symbols(Path(settings.app.universe_path))


def _load_json_snapshot(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _approved_alert_candidates(candidates: object) -> list[dict[str, object]]:
    rows = candidates if isinstance(candidates, list) else []
    return [
        row
        for row in rows
        if row.get("status") == "APPROVED"
        and row.get("quality") == "GREEN"
        and row.get("freshness") == "fresh"
    ]


def _format_signal_alert(candidates: list[dict[str, object]]) -> str:
    lines = ["BUY CANDIDATE"]
    for row in candidates:
        reasons = row.get("reasons", [])
        reason_text = "; ".join(reasons) if isinstance(reasons, list) else str(reasons)
        lines.append(
            " ".join(
                [
                    str(row.get("ticker", "")),
                    f"entry={float(row.get('entry', 0.0)):.2f}",
                    f"stop={float(row.get('stop', 0.0)):.2f}",
                    f"target={float(row.get('target', 0.0)):.2f}",
                    f"conf={float(row.get('confidence', 0.0)):.2f}",
                    f"reason={reason_text}",
                ]
            ).strip()
        )
    return "\n".join(lines)


def _format_exit_alert(events: list[dict[str, object]]) -> str:
    lines = ["SELL ALERT"]
    for row in events:
        quantity = int(row.get("quantity", 0))
        fill_price = float(row.get("fill_price", 0.0))
        cash = float(row.get("cash", 0.0))
        lines.append(
            " ".join(
                [
                    str(row.get("ticker", "")),
                    f"reason={row.get('reason', 'exit')}",
                    f"qty={quantity}",
                    f"price={fill_price:.2f}",
                    f"cash={cash:.2f}",
                ]
            ).strip()
        )
    return "\n".join(lines)


def _send_discord_alert(settings, content: str) -> None:
    runtime_alerts.send_discord_message(
        webhook_url=settings.alerts.discord_webhook_url,
        content=content,
        username=settings.alerts.discord_username,
    )


def notify(settings, level: str, title: str, message: str, details: dict | None = None) -> list[bool]:
    """Send notification via AlertNotifier (Slack/Discord/generic webhook).

    Returns list of success status for each configured notifier.
    """
    from trading_bot.monitoring.notifiers import AlertNotifier, AlertLevel

    level_map = {
        "info": AlertLevel.INFO,
        "warning": AlertLevel.WARNING,
        "critical": AlertLevel.CRITICAL,
    }
    notifier = AlertNotifier(settings)
    if not notifier.has_notifiers():
        return []
    return notifier.notify(
        level=level_map.get(level, AlertLevel.INFO),
        title=title,
        message=message,
        details=details,
    )


def _maybe_counter_thesis_exit(
    ticker: str,
    position,
    frame,
    last_price: float,
    settings,
    manage_now: datetime,
    broker,
    ledger,
    state,
    log_path,
):
    """Check counter-thesis for an open position; exit if thesis is broken.

    Returns ``(new_state, event, line)`` when the position is exited, or
    None when the position should be kept (feature disabled, no block, or
    data unavailable — a data outage never forces an exit).
    """
    if not settings.counter_thesis.enabled or not settings.counter_thesis.exit_on_block:
        return None
    from trading_bot.runtime.orchestrator import _evaluate_counter_thesis_for_position

    result = _evaluate_counter_thesis_for_position(ticker, position, frame, settings)
    if result is None or not result.block_trade:
        return None

    new_state, event, line = _fill_sell_position(
        ticker, position, "counter-thesis", manage_now, last_price,
        broker, ledger, state, log_path,
    )
    event["counter_thesis"] = result.to_dict()
    return new_state, event, line


def _fill_sell_position(
    ticker: str,
    position,
    reason: str,
    submitted_at: datetime,
    last_price: float,
    broker,
    ledger,
    state,
    log_path,
) -> tuple:
    """Submit a market SELL order, record the fill, and update portfolio state.

    Returns ``(new_state, event_dict, line_text)`` so the caller can append
    the event to its own exit_events / lines / actions counters.
    """
    fill = broker.submit_order(
        OrderRequest(
            ticker=ticker,
            side="SELL",
            order_type="market",
            quantity=position.quantity,
            submitted_at=submitted_at,
        ),
        market_price=last_price,
    )
    realized_pnl = (fill.fill_price - position.average_cost) * fill.quantity - fill.fees
    ledger.record_fill(fill, side="SELL", realized_pnl=realized_pnl)
    new_state = _portfolio_state_after_sell(
        previous_state=state,
        ticker=ticker,
        fill_price=fill.fill_price,
        fill_fees=fill.fees,
        broker=broker,
    )
    ledger.save_portfolio_state(new_state)
    ledger.record_equity_snapshot(new_state, timestamp=fill.filled_at)
    event = {
        "command": "manage-positions",
        "ticker": ticker,
        "status": "FILLED",
        "reason": reason,
        "quantity": fill.quantity,
        "fill_price": fill.fill_price,
        "cash": new_state.cash,
    }
    line = (
        f"{ticker} FILLED reason={reason} qty={fill.quantity} "
        f"price={fill.fill_price:.2f} cash={new_state.cash:.2f}"
    )

    strategy_tag = getattr(position, "strategy_tag", "")
    if strategy_tag:
        from trading_bot.strategy.strategy_tracker import record_exit as _rec_exit

        _rec_exit(
            log_path.parent,
            strategy_tag,
            ticker,
            position.average_cost,
            fill.fill_price,
            fill.quantity,
            fill.fees,
            realized_pnl,
            reason,
            submitted_at,
        )

    return new_state, event, line


def _paper_broker_from_state(state: PortfolioState, settings) -> PaperBroker:
    broker = PaperBroker(
        starting_cash=state.cash,
        fee_per_order=settings.paper.fee_per_order,
        slippage_bps=settings.paper.slippage_bps,
    )
    broker.positions = {
        ticker: position.quantity
        for ticker, position in state.positions.items()
        if position.quantity > 0
    }
    return broker


def _update_trailing_stop(
    position,
    frame,
    last_price: float,
    settings,
) -> tuple[float, str, float, float] | None:
    """Tighten `position.stop_loss` using the latest frame.

    Returns `(new_stop, method, new_highest_high, new_initial_risk)` when
    the stop ratchets up, otherwise `None` so the caller falls through to
    the standard open-position line. `new_initial_risk` is locked in on
    the first call (entry_price - stop_loss) and persisted for future
    runs so r-multiple math stays stable as the stop moves.

    CRITICAL: `highest_high` tracks the bar's true high, not the close.
    This ensures chandelier stops capture wicks/spikes, not just closes.
    """
    # Extract the bar's high for proper chandelier tracking
    bar_high = last_price  # fallback if no high column
    if "high" in frame.columns:
        bar_high = float(frame.iloc[-1]["high"])
    
    # Track true high of the move (including wicks), not just closes
    new_highest_high = max(position.highest_high or bar_high, bar_high)

    new_initial_risk = position.initial_risk
    if (
        new_initial_risk is None
        and position.stop_loss is not None
        and position.stop_loss < position.average_cost
    ):
        new_initial_risk = round(position.average_cost - position.stop_loss, 4)

    atr_value: float | None = None
    try:
        # Fetch daily frame for ATR calculation (chandelier should use daily volatility)
        from trading_bot.data import market_data
        daily_frame = market_data.fetch_bars(
            position.ticker,
            settings.market_data.daily_period,
            "1d",
        )
        atr_frame = add_atr(daily_frame, period=14, column_name="atr_14")
        atr_series = atr_frame["atr_14"].dropna()
        if not atr_series.empty:
            atr_value = float(atr_series.iloc[-1])
    except (KeyError, ValueError):
        atr_value = None

    candidate = position.model_copy(
        update={
            "highest_high": new_highest_high,
            "initial_risk": new_initial_risk or position.initial_risk,
        }
    )
    new_stop, method = next_trailing_stop(candidate, last_price, atr_value)
    if new_stop is None or method is None:
        return None
    if position.stop_loss is not None and new_stop <= position.stop_loss:
        return None

    return new_stop, method, new_highest_high, new_initial_risk or position.initial_risk


def _portfolio_state_after_sell(
    previous_state: PortfolioState,
    ticker: str,
    fill_price: float,
    fill_fees: float,
    broker: PaperBroker,
) -> PortfolioState:
    exited_position = previous_state.positions[ticker]
    positions = {
        symbol: previous_state.positions[symbol].model_copy(update={"quantity": quantity})
        for symbol, quantity in broker.positions.items()
        if quantity > 0 and symbol in previous_state.positions
    }
    realized_delta = (
        (fill_price - exited_position.average_cost) * exited_position.quantity
    ) - fill_fees
    equity = broker.cash + sum(
        position.quantity * position.average_cost for position in positions.values()
    )
    return PortfolioState(
        cash=round(broker.cash, 2),
        equity=round(equity, 2),
        positions=positions,
        realized_pnl=round(previous_state.realized_pnl + realized_delta, 2),
        unrealized_pnl=0.0,
    )


def _fetch_latest_prices(symbols: list[str], settings) -> dict[str, float]:
    if not symbols:
        return {}

    from trading_bot.data import market_data

    prices: dict[str, float] = {}
    for symbol in symbols:
        frame = _try_fetch_bars(symbol, settings, market_data)
        if frame is None:
            continue
        if frame.empty or "close" not in frame.columns:
            continue
        last_price = float(frame.iloc[-1]["close"])
        if math.isfinite(last_price):
            prices[symbol] = last_price
    return prices


def _try_fetch_bars(symbol: str, settings, market_data) -> "pd.DataFrame | None":
    """Try intraday bars first, falling back to daily bars."""
    try:
        frame = market_data.fetch_bars(
            symbol,
            settings.market_data.intraday_period,
            settings.market_data.intraday_interval,
        )
    except Exception:
        frame = None
    if frame is not None and not frame.empty:
        return frame
    try:
        frame = market_data.fetch_bars(symbol, "1mo", "1d")
    except Exception:
        return None
    return frame


def _format_scan_summary(summary: dict[str, object]) -> str:
    return " ".join(
        [
            "summary",
            f"symbols={summary['symbols']}",
            f"approved={summary['approved']}",
            f"green={summary['green']}",
            f"yellow={summary['yellow']}",
            f"rejected={summary['rejected']}",
            f"no_signal={summary['no_signal']}",
            f"errors={summary['errors']}",
        ]
    )


def _format_doctor(settings) -> str:
    snapshots = [
        settings.app.scan_results_path,
        settings.app.portfolio_summary_path,
        settings.app.dashboard_summary_path,
        settings.app.backtest_summary_path,
    ]
    ready_snapshots = sum(1 for path in snapshots if Path(path).exists())

    provider = getattr(settings.market_data, "provider", "yfinance")
    provider_ok = "ok"
    if provider == "alpaca":
        import os
        if not os.environ.get("APCA_API_KEY_ID") or not os.environ.get("APCA_API_SECRET_KEY"):
            provider_ok = "missing APCA_API_KEY_ID/APCA_API_SECRET_KEY"

    return " ".join(
        [
            "doctor",
            f"live_trading={str(settings.app.live_trading_enabled).lower()}",
            f"state_db={_exists_label(settings.app.state_db_path)}",
            f"log_dir={_exists_label(settings.app.log_dir)}",
            f"snapshots={ready_snapshots}/{len(snapshots)}",
            f"provider={provider}",
            f"provider_auth={provider_ok}",
        ]
    )


@app.command(name="performance")
def performance(
    ctx: typer.Context,
    days: int = typer.Option(30, "--days", help="Number of days to analyze"),
    daily: bool = typer.Option(False, "--daily", help="Show daily breakdown"),
) -> None:
    """Show detailed performance metrics and trade statistics."""
    from trading_bot.monitoring.performance import (
        calculate_daily_metrics,
        calculate_performance_metrics,
        format_performance_report,
    )

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))

    if daily:
        daily_data = calculate_daily_metrics(ledger, lookback_days=days)
        if not daily_data:
            typer.echo("No trades found for the specified period.")
            return

        typer.echo(f"Daily Performance (last {len(daily_data)} days with trades):")
        typer.echo("")
        for day in daily_data:
            typer.echo(
                f"{day['date']}: "
                f"trades={day['trades']} "
                f"wins={day['wins']} "
                f"losses={day['losses']} "
                f"pnl=${day['net_pnl']:,.2f} "
                f"win_rate={day['win_rate']:.1%}"
            )
    else:
        metrics = calculate_performance_metrics(ledger, days=days)
        report = format_performance_report(metrics)
        typer.echo(report)


@app.command(name="health")
def health(ctx: typer.Context) -> None:
    """Run system health check."""
    from trading_bot.monitoring.health import check_system_health, format_health_report

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    health_result = check_system_health(ctx.obj, ledger)

    report = format_health_report(health_result)
    typer.echo(report)

    if not health_result.is_healthy():
        notify(
            ctx.obj,
            "critical",
            "System UNHEALTHY",
            report,
            {k: v for k, (status, msg) in health_result.checks.items() if not status},
        )
        raise typer.Exit(code=1)


@app.command(name="alerts")
def alerts(ctx: typer.Context) -> None:
    """Check for active alert conditions."""
    from trading_bot.monitoring.health import check_alert_conditions
    from trading_bot.monitoring.notifiers import notify_alerts, AlertNotifier

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    active_alerts = check_alert_conditions(ledger)

    if not active_alerts:
        typer.echo("No active alerts. System operating normally.")
        return

    typer.echo(f"Active Alerts ({len(active_alerts)}):")
    typer.echo("")
    for alert in active_alerts:
        level_icon = "🔴" if alert["level"] == "critical" else "🟡"
        typer.echo(f"{level_icon} [{alert['type']}] {alert['message']}")

    # Send alerts via configured notifiers
    notifier = AlertNotifier(ctx.obj)
    notify_alerts(active_alerts, notifier=notifier)

    # Exit with error code if critical alerts
    if any(a["level"] == "critical" for a in active_alerts):
        raise typer.Exit(code=2)


@app.command(name="strategy-health")
def strategy_health(
    ctx: typer.Context,
    window: int = typer.Option(20, "--window", help="Rolling window for win rate."),
) -> None:
    """Show per-strategy performance and allocation status."""
    from trading_bot.strategy.strategy_tracker import strategy_summary

    rows = strategy_summary(Path(ctx.obj.app.log_dir), window=window)
    if not rows:
        typer.echo("No strategy results tracked yet.")
        return

    typer.echo(f"{'Strategy':<30} {'Exits':>6} {'Recent':>6} {'Wins':>5} {'Losses':>6} {'WinRate':>8} {'PnL':>10} {'Alloc':>6}")
    typer.echo("-" * 85)
    for r in rows:
        alloc_label = {1.0: "full", 0.5: "half", 0.0: "skip"}.get(r["allocation"], str(r["allocation"]))
        typer.echo(
            f"{r['strategy']:<30} {r['total_exits']:>6} {r['recent_exits']:>6} "
            f"{r['recent_wins']:>5} {r['recent_losses']:>6} "
            f"{r['recent_win_rate']:>7.1%} {r['recent_net_pnl']:>8.2f} {alloc_label:>6}"
        )


@app.command(name="drawdown")
def drawdown(ctx: typer.Context) -> None:
    """Show drawdown analysis from equity history."""
    from trading_bot.monitoring.drawdown import (
        compute_drawdown_from_ledger,
        format_drawdown_report,
    )

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    metrics = compute_drawdown_from_ledger(ledger, limit=500)
    typer.echo(format_drawdown_report(metrics))

    if metrics.max_drawdown_pct > ctx.obj.monitoring.max_drawdown_pct:
        typer.echo(
            f"\n⚠️  Max drawdown {metrics.max_drawdown_pct:.2f}% exceeds limit "
            f"{ctx.obj.monitoring.max_drawdown_pct:.2f}%"
        )
        raise typer.Exit(code=1)


@app.command(name="correlation")
def correlation(ctx: typer.Context) -> None:
    """Analyze pairwise correlation across open positions."""
    from trading_bot.data.market_data import fetch_bars
    from trading_bot.risk.correlation import (
        compute_portfolio_correlation,
        format_correlation_report,
    )

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    tickers = sorted(t for t, p in state.positions.items() if p.quantity > 0)

    if len(tickers) < 2:
        typer.echo("Need 2+ open positions to compute correlation.")
        return

    price_history: dict[str, list[float]] = {}
    for ticker in tickers:
        try:
            bars = fetch_bars(ticker, period="3mo", interval="1d", settings=ctx.obj.market_data)
            if not bars.empty:
                price_history[ticker] = [float(c) for c in bars["Close"].tolist()]
        except Exception as exc:
            typer.echo(f"Warning: could not fetch history for {ticker}: {exc}")

    result = compute_portfolio_correlation(
        state.positions,
        price_history,
        max_avg_correlation=ctx.obj.monitoring.max_avg_correlation,
    )
    typer.echo(format_correlation_report(result))

    if result.warning:
        raise typer.Exit(code=1)


@app.command(name="var")
def var(
    ctx: typer.Context,
    method: str = typer.Option("historical", "--method", help="historical or parametric"),
) -> None:
    """Calculate Value at Risk for current portfolio."""
    from trading_bot.data.market_data import fetch_bars
    from trading_bot.risk.var import (
        compute_historical_var,
        compute_parametric_var,
        format_var_report,
        compute_stress_test,
        format_stress_report,
    )

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    tickers = sorted(t for t, p in state.positions.items() if p.quantity > 0)

    if not tickers:
        typer.echo("No open positions for VaR calculation.")
        return

    latest_prices = _fetch_latest_prices(tickers, ctx.obj)
    price_history: dict[str, list[float]] = {}
    for ticker in tickers:
        try:
            bars = fetch_bars(ticker, period="1y", interval="1d", settings=ctx.obj.market_data)
            if not bars.empty:
                price_history[ticker] = [float(c) for c in bars["Close"].tolist()]
        except Exception as exc:
            typer.echo(f"Warning: could not fetch history for {ticker}: {exc}")

    position_values: dict[str, float] = {}
    for ticker, position in state.positions.items():
        if position.quantity > 0:
            price = latest_prices.get(ticker, position.average_cost)
            position_values[ticker] = position.quantity * price

    confidence = ctx.obj.monitoring.var_confidence

    if method == "parametric":
        var_result = compute_parametric_var(
            position_values, price_history, state.positions, confidence=confidence
        )
    else:
        var_result = compute_historical_var(
            position_values, price_history, state.positions, confidence=confidence
        )

    typer.echo(format_var_report(var_result))
    typer.echo("")

    stress_results = compute_stress_test(position_values, state.positions)
    typer.echo(format_stress_report(stress_results))


@app.command(name="risk-report")
def risk_report(ctx: typer.Context) -> None:
    """Comprehensive risk report: drawdown, VaR, correlation, stress tests."""
    from trading_bot.monitoring.drawdown import (
        compute_drawdown_from_ledger,
        format_drawdown_report,
    )
    from trading_bot.risk.correlation import (
        compute_portfolio_correlation,
        format_correlation_report,
    )
    from trading_bot.risk.var import (
        compute_historical_var,
        compute_parametric_var,
        compute_stress_test,
        format_stress_report,
        format_var_report,
    )
    from trading_bot.monitoring.realtime_pnl import (
        calculate_realtime_pnl,
        check_pnl_alerts,
    )
    from trading_bot.data.market_data import fetch_bars

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    settings = ctx.obj

    typer.echo("=" * 60)
    typer.echo("RISK REPORT")
    typer.echo("=" * 60)
    typer.echo("")

    # 1. Drawdown
    typer.echo("--- Drawdown ---")
    dd_metrics = compute_drawdown_from_ledger(ledger, limit=500)
    typer.echo(format_drawdown_report(dd_metrics))
    typer.echo("")

    # 2. VaR
    typer.echo("--- Value at Risk ---")
    tickers = sorted(t for t, p in state.positions.items() if p.quantity > 0)
    if tickers:
        latest_prices = _fetch_latest_prices(tickers, settings)
        price_history: dict[str, list[float]] = {}
        for ticker in tickers:
            try:
                bars = fetch_bars(ticker, period="1y", interval="1d", settings=settings.market_data)
                if not bars.empty:
                    price_history[ticker] = [float(c) for c in bars["Close"].tolist()]
            except Exception:
                pass

        position_values: dict[str, float] = {}
        for ticker, position in state.positions.items():
            if position.quantity > 0:
                price = latest_prices.get(ticker, position.average_cost)
                position_values[ticker] = position.quantity * price

        if price_history:
            var_h = compute_historical_var(
                position_values, price_history, state.positions,
                confidence=settings.monitoring.var_confidence,
            )
            typer.echo(format_var_report(var_h))

            var_p = compute_parametric_var(
                position_values, price_history, state.positions,
                confidence=settings.monitoring.var_confidence,
            )
            typer.echo("")
            typer.echo(format_var_report(var_p))
        typer.echo("")

        # 3. Stress Tests
        typer.echo("--- Stress Tests ---")
        stress_results = compute_stress_test(position_values, state.positions)
        typer.echo(format_stress_report(stress_results))
        typer.echo("")

        # 4. Correlation
        typer.echo("--- Correlation ---")
        if len(tickers) >= 2:
            corr_result = compute_portfolio_correlation(
                state.positions,
                price_history,
                max_avg_correlation=settings.monitoring.max_avg_correlation,
            )
            typer.echo(format_correlation_report(corr_result))
        else:
            typer.echo("Need 2+ open positions for correlation analysis.")
        typer.echo("")

        # 5. Real-time PnL alerts
        typer.echo("--- PnL Alerts ---")
        snapshot = calculate_realtime_pnl(ledger, latest_prices)
        pnl_alerts = check_pnl_alerts(snapshot)
        if pnl_alerts:
            for alert in pnl_alerts:
                level_icon = "🔴" if alert["level"] == "critical" else "🟡"
                typer.echo(f"{level_icon} [{alert['type']}] {alert['message']}")
        else:
            typer.echo("No active PnL alerts.")
    else:
        typer.echo("No open positions — skipping VaR, correlation, stress tests.")

    typer.echo("")
    typer.echo("=" * 60)
    typer.echo("Risk report complete.")
    typer.echo("=" * 60)


@app.command(name="kill-switch")
def kill_switch(
    ctx: typer.Context,
    status: bool = typer.Option(None, "--status", help="Show current status"),
    halt: bool = typer.Option(False, "--halt", help="Halt all trading"),
    resume: bool = typer.Option(False, "--resume", help="Resume trading"),
    reason: str = typer.Option("manual", "--reason", help="Reason for halt"),
) -> None:
    """Emergency kill switch to halt/resume all trading.

    Use --halt to immediately stop all trading activity.
    Use --resume to allow trading again.
    Use --status to check current state.
    """
    from trading_bot.safety.kill_switch import (
        halt_trading,
        is_trading_halted,
        resume_trading,
    )

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))

    # Default to showing status if no action specified
    if status or (not halt and not resume):
        state = is_trading_halted(ledger)
        if state.enabled:
            typer.echo("🔴 KILL SWITCH: TRADING HALTED")
            typer.echo(f"  Reason: {state.reason}")
            typer.echo(f"  Triggered at: {state.triggered_at}")
            typer.echo(f"  Triggered by: {state.triggered_by}")
            raise typer.Exit(code=1)
        else:
            typer.echo("🟢 KILL SWITCH: Trading active")
            raise typer.Exit(code=0)

    if halt:
        halt_trading(ledger, reason=reason, triggered_by="operator")
        typer.echo(f"🔴 TRADING HALTED: {reason}")
        typer.echo("All trading activity is now blocked.")
        typer.echo("Use --resume to allow trading again.")
        raise typer.Exit(code=0)

    if resume:
        resume_trading(ledger, resumed_by="operator")
        typer.echo("🟢 TRADING RESUMED")
        typer.echo("Trading activity is now allowed.")
        raise typer.Exit(code=0)


def _exists_label(path: str) -> str:
    return "ok" if Path(path).exists() else "missing"


def _robinhood_boundary(settings):
    from trading_bot.brokers.robinhood.boundary import RobinhoodBrokerBoundary

    return RobinhoodBrokerBoundary(settings)


# V3: Robinhood CLI commands (MCP snapshot-only; no direct auth/live path)
@app.command()
def robinhood_status(
    ctx: typer.Context,
) -> None:
    """Show MCP-backed Robinhood snapshot status."""
    settings = ctx.obj
    boundary = _robinhood_boundary(settings)
    status = boundary.get_status()
    
    typer.echo("📊 Robinhood Status")
    typer.echo("─" * 40)
    
    typer.echo(f"Enabled: {'✅ Yes' if settings.robinhood.enabled else '❌ No'}")
    typer.echo(f"Mode: {settings.robinhood.mode.upper()}")
    typer.echo(f"Max Position: ${settings.robinhood.max_position_value:,.2f}")
    typer.echo(f"Daily Loss Limit: ${settings.robinhood.daily_loss_limit:,.2f}")

    typer.echo("\n🔌 MCP Broker Boundary")
    typer.echo(f"Source: {status.source.upper()}")
    typer.echo(f"Connection: {'connected' if status.connected else 'disconnected'}")
    typer.echo(f"Freshness: {status.freshness}")
    typer.echo(f"Account: {status.account_number or '(none)'}")
    typer.echo(f"Synced At: {status.synced_at or '(never)'}")
    typer.echo(f"Read Only: {'yes' if status.capabilities.read_only else 'no'}")
    typer.echo(
        f"Shadow Preview: {'yes' if status.capabilities.shadow_preview else 'no'}"
    )
    typer.echo(f"Live Submit: {'yes' if status.capabilities.live_submit else 'no'}")
    typer.echo(f"Live Cancel: {'yes' if status.capabilities.live_cancel else 'no'}")
    if status.reason:
        typer.echo(f"Reason: {status.reason}")


@app.command()
def sync_account(
    ctx: typer.Context,
) -> None:
    """
    Sync account information from Robinhood.
    
    Displays buying power, equity, and cash from your Robinhood account.
    Compares with local paper portfolio (if different).
    """
    boundary = _robinhood_boundary(ctx.obj)
    status = boundary.get_status()

    if not status.connected:
        typer.echo("❌ Robinhood account snapshot unavailable.")
        typer.echo("   Sync must be performed by Codex/operator using Robinhood MCP.")
        if status.reason:
            typer.echo(f"   {status.reason}")
        raise typer.Exit(code=1)

    account = boundary.get_portfolio(status.account_number or "")
    if account is None:
        typer.echo("❌ Robinhood account snapshot unavailable.")
        raise typer.Exit(code=1)

    typer.echo("📊 Robinhood Account Snapshot")
    typer.echo("─" * 40)
    typer.echo(f"Account: {account.account_number}")
    typer.echo(f"Equity: ${account.equity:,.2f}")
    typer.echo(f"Buying Power: ${account.buying_power:,.2f}")
    typer.echo(f"Cash: ${account.cash:,.2f}")
    typer.echo(f"Synced At: {status.synced_at or '(unknown)'}")
    typer.echo(f"Freshness: {status.freshness}")


@app.command()
def sync_positions(
    ctx: typer.Context,
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Show diff without applying"),
) -> None:
    """
    Sync positions from Robinhood.
    
    By default, shows diff between Robinhood positions and local state.
    Use --apply to update local state to match Robinhood.
    """
    boundary = _robinhood_boundary(ctx.obj)
    status = boundary.get_status()

    if not dry_run:
        typer.echo("❌ Local apply is not supported for MCP-backed Robinhood snapshots.")
        typer.echo("   Use Codex/operator review before changing local broker state.")
        raise typer.Exit(code=1)

    if not status.connected:
        typer.echo("❌ Robinhood position snapshot unavailable.")
        typer.echo("   Sync must be performed by Codex/operator using Robinhood MCP.")
        if status.reason:
            typer.echo(f"   {status.reason}")
        raise typer.Exit(code=1)

    positions = boundary.get_positions_for(status.account_number or "")
    typer.echo("📊 Robinhood Position Snapshot")
    typer.echo("─" * 40)
    typer.echo(f"Account: {status.account_number or '(none)'}")
    typer.echo(f"Positions: {len(positions)}")
    for position in positions:
        typer.echo(
            f"  {position.symbol}: qty={position.quantity:.4f} "
            f"avg=${position.average_cost:,.2f} value=${position.market_value:,.2f}"
        )


@app.command(name="live-trading")
def live_trading_status(ctx: typer.Context) -> None:
    """Check live trading status and configuration."""
    settings = ctx.obj
    boundary = _robinhood_boundary(settings)
    status = boundary.get_status()

    typer.echo("🔴 Live Trading Status")
    typer.echo("=" * 50)

    typer.echo("Status: disabled for local CLI")
    typer.echo("Mode: operator-mediated MCP intents only")
    typer.echo("")
    typer.echo("Robinhood Boundary:")
    typer.echo(f"  Source: {status.source}")
    typer.echo(f"  Connected: {status.connected}")
    typer.echo(f"  Freshness: {status.freshness}")
    typer.echo(f"  Account: {status.account_number or '(none)'}")

    typer.echo("")
    typer.echo("Robinhood Configuration:")
    typer.echo(f"  Enabled: {settings.robinhood.enabled}")
    typer.echo(f"  Mode: {settings.robinhood.mode}")
    
    typer.echo("")
    typer.echo("Safety Limits:")
    typer.echo(f"  Max Position Value: ${settings.robinhood.max_position_value:,.2f}")
    typer.echo(f"  Daily Loss Limit: ${settings.robinhood.daily_loss_limit:,.2f}")
    typer.echo(f"  Max Position %: {settings.risk.max_ticker_allocation_pct:.1%}")
    typer.echo("")
    typer.echo("Local direct auth/order execution is not supported. "
               "Live Robinhood actions must be handled through an operator-reviewed MCP workflow.")


@app.command(name="reconcile-positions")
def reconcile_positions_cmd(
    ctx: typer.Context,
    tolerance: float = typer.Option(1.0, "--tolerance", help="Allowed difference %"),
) -> None:
    """Reconcile local positions with Robinhood MCP snapshots.

    Read-only: compares local ledger positions against the broker boundary's
    operator-synced position snapshot and reports discrepancies. Never mutates
    local state - corrections must be applied via the operator-reviewed MCP
    workflow.
    """
    from trading_bot.brokers.robinhood.reconciliation import reconcile_positions

    settings = ctx.obj
    boundary = _robinhood_boundary(settings)
    status = boundary.get_status()

    typer.echo("📊 Position Reconciliation")
    typer.echo("=" * 50)

    if not status.connected:
        typer.echo("❌ Robinhood position snapshot unavailable.")
        typer.echo("   Sync must be performed by Codex/operator using Robinhood MCP.")
        if status.reason:
            typer.echo(f"   {status.reason}")
        raise typer.Exit(code=1)

    ledger = PortfolioLedger(Path(settings.app.state_db_path))
    result = reconcile_positions(ledger, boundary, tolerance_pct=tolerance)

    typer.echo(f"Source: {result.broker_source}")
    typer.echo(f"Status: {'✅ MATCHED' if result.matches else '❌ DISCREPANCIES'}")
    typer.echo(f"Local Value:  ${result.local_total_value:,.2f}")
    typer.echo(f"Broker Value: ${result.broker_total_value:,.2f}")
    typer.echo(f"Difference:   {result.value_difference_pct:.2f}%")

    if result.discrepancies:
        typer.echo("")
        typer.echo("Discrepancies found:")
        for disc in result.discrepancies:
            icon = "🔴" if disc.severity == "critical" else "🟡"
            typer.echo(
                f"  {icon} {disc.symbol}: "
                f"Local={disc.local_quantity:.2f}, "
                f"Broker={disc.broker_quantity:.2f}"
            )
    if result.local_only:
        typer.echo(f"\nLocal Only: {', '.join(result.local_only)}")
    if result.broker_only:
        typer.echo(f"\nBroker Only: {', '.join(result.broker_only)}")


@app.command(name="counter-thesis")
def counter_thesis(
    ctx: typer.Context,
    symbols: list[str] = typer.Option(
        ...,
        "--symbols",
        help="Symbols to run counter-thesis analysis against.",
    ),
    why: bool = typer.Option(
        False,
        "--why",
        help="Print each finding in full.",
    ),
) -> None:
    """Run counter-thesis analysis against each symbol's BUY thesis.

    For every symbol a signal (the thesis) is generated first; then each
    counter-thesis check looks for the strongest evidence against it. A
    blocked trade would be vetoed by the risk manager in scan/paper-trade.
    """
    from trading_bot.runtime.orchestrator import (
        _build_signal_result,
        _evaluate_counter_thesis_for_signal,
    )

    parsed_symbols = _parse_symbols(symbols)
    for symbol in parsed_symbols:
        typer.echo(f"{symbol} analyzing...")
        try:
            signal, reason, _ = _build_signal_result(symbol, ctx.obj)
        except Exception as exc:
            typer.echo(f"{symbol} NO_THESIS error={exc}")
            continue
        if signal is None:
            typer.echo(f"{symbol} NO_THESIS reason={reason}")
            continue

        result = _evaluate_counter_thesis_for_signal(symbol, signal, ctx.obj)
        if result is None:
            typer.echo(f"{symbol} counter_thesis=disabled")
            continue

        typer.echo(
            f"{symbol} counter_thesis "
            f"severity={result.overall_severity} "
            f"findings={len(result.findings)} "
            f"confidence={result.confidence_multiplier:.2f} "
            f"block={'true' if result.block_trade else 'false'}"
        )
        if why:
            for finding in result.findings:
                typer.echo(
                    f"  - {finding.check_name}: [{finding.severity}] {finding.description}"
                )


@app.command(name="discover")
def discover_symbols(
    ctx: typer.Context,
    mode: str = typer.Option("breakout", "--mode", help="Discovery mode: breakout, mean-reversion, gap-up"),
    max_symbols: int = typer.Option(20, "--max", help="Maximum symbols to return"),
    export: bool = typer.Option(False, "--export", help="Export to burn-in symbols file"),
) -> None:
    """Dynamically discover trading candidates based on market conditions."""
    from trading_bot.strategy.dynamic_watchlist import DynamicWatchlist
    from trading_bot.data.market_data import fetch_bars

    typer.echo(f"Discovering symbols (mode: {mode})...")
    typer.echo("=" * 50)

    watchlist = DynamicWatchlist(max_symbols=max_symbols)

    def data_provider(symbol: str):
        try:
            return fetch_bars(symbol, interval="1d", period="1mo")
        except Exception:
            return None

    typer.echo("Scanning universe for setups...")
    update = watchlist.update(data_provider)

    if update.sectors_favored:
        typer.echo(f"Favored sectors: {', '.join(update.sectors_favored)}")

    typer.echo(f"Added: {len(update.added)} | Removed: {len(update.removed)} | Total: {len(update.current)}")
    typer.echo("")

    if update.current:
        for entry in update.current[:max_symbols]:
            typer.echo(f"  {entry.symbol}: {entry.reason} (score: {entry.score:.1f})")
    else:
        typer.echo("No symbols passed screening criteria.")

    if export:
        export_path = watchlist.export_for_burn_in()
        typer.echo(f"\nExported {len(watchlist.get_symbols())} symbols to {export_path}")


@app.command(name="sector-analysis")
def sector_analysis_cmd(ctx: typer.Context) -> None:
    """Analyze sector rotation and relative strength."""
    from trading_bot.strategy.sector_rotation import SECTOR_ETFS, analyze_sector_rotation
    from trading_bot.data.market_data import fetch_bars

    typer.echo("📊 Sector Rotation Analysis")
    typer.echo("=" * 50)

    # Fetch sector data
    sector_data = {}
    spy_data = None

    typer.echo("Fetching sector data...")
    for symbol in list(SECTOR_ETFS.keys())[:5]:  # Demo: just first 5
        try:
            frame = fetch_bars(symbol, interval="1d", period="3mo")
            if frame is not None:
                sector_data[symbol] = frame
                typer.echo(f"  ✓ {symbol}")
        except Exception as e:
            typer.echo(f"  ✗ {symbol}: {e}")

    # Fetch SPY for relative strength
    try:
        spy_data = fetch_bars("SPY", interval="1d", period="3mo")
        typer.echo("  ✓ SPY (benchmark)")
    except Exception:
        pass

    if len(sector_data) < 3:
        typer.echo("")
        typer.echo("❌ Insufficient data for analysis")
        raise typer.Exit(code=1)

    # Analyze
    analysis = analyze_sector_rotation(sector_data, spy_data)

    typer.echo("")
    typer.echo("Results:")
    typer.echo(f"  Risk Mode: {'ON (Growth)' if analysis.risk_on else 'OFF (Defensive)'}")
    typer.echo(f"  Rotation Detected: {'Yes' if analysis.rotation_detected else 'No'}")

    if analysis.sectors:
        typer.echo("")
        typer.echo("Sector Rankings:")
        for sector in sorted(analysis.sectors, key=lambda x: x.rank)[:5]:
            icon = "🟢" if sector.rank <= 3 else "🟡" if sector.rank <= 6 else "🔴"
            typer.echo(f"  {icon} #{sector.rank} {sector.name} ({sector.symbol})")
            typer.echo(f"      5d: {sector.price_change_5d:+.2f}% | "
                      f"20d: {sector.price_change_20d:+.2f}% | "
                      f"RS: {sector.relative_strength:+.2f}%")


@app.command(name="screen")
def screen_market(
    ctx: typer.Context,
    min_price: float = typer.Option(5.0, "--min-price"),
    max_price: float = typer.Option(500.0, "--max-price"),
    min_volume: int = typer.Option(1_000_000, "--min-volume"),
    top: int = typer.Option(10, "--top", help="Show top N results"),
) -> None:
    """Screen market for technical setups."""
    from trading_bot.strategy.market_screener import MarketScreener
    from trading_bot.data.market_data import fetch_bars

    typer.echo("📈 Market Screen")
    typer.echo("=" * 50)
    typer.echo(f"Criteria: ${min_price:.2f} - ${max_price:.2f}, "
               f"min volume {min_volume:,.0f}")
    typer.echo("")

    screener = MarketScreener(
        min_price=min_price,
        max_price=max_price,
        min_volume=min_volume,
    )

    # Screen universe
    universe = screener.DEFAULT_UNIVERSE[:20]  # Demo: first 20
    results = []

    typer.echo("Screening symbols...")
    for symbol in universe:
        try:
            frame = fetch_bars(symbol, interval="1d", period="1mo")
            if frame is not None and len(frame) >= 20:
                result = screener.screen_symbol(symbol, frame)
                results.append(result)
                if result.passed:
                    typer.echo(f"  ✓ {symbol}: Score {result.score:.1f}")
        except Exception:
            continue

    # Show top results
    passed = [r for r in results if r.passed]
    passed.sort(key=lambda x: x.score, reverse=True)

    typer.echo("")
    typer.echo(f"Results: {len(passed)}/{len(results)} passed screening")

    if passed:
        typer.echo("")
        typer.echo(f"Top {min(top, len(passed))} candidates:")
        for result in passed[:top]:
            typer.echo(f"  {result.symbol}: {result.score:.1f} pts")
            if result.reasons:
                typer.echo(f"    {result.reasons[0]}")


@app.command(name="rl-train")
def rl_train(
    ctx: typer.Context,
    symbols: str = typer.Option("AAPL", "--symbols", help="Comma-separated symbols to train on"),
    agent: str = typer.Option("PPO", "--agent", help="DRL agent type (PPO, A2C, SAC, TD3, DDPG)"),
    episodes: int = typer.Option(100, "--episodes", help="Number of training episodes"),
    timesteps: int = typer.Option(100000, "--timesteps", help="Total timesteps"),
    learning_rate: float = typer.Option(3e-4, "--learning-rate", help="Learning rate"),
    feature_set: str = typer.Option("standard", "--feature-set", help="Feature set (standard, extended)"),
    output_dir: str = typer.Option("trained_models", "--output-dir", help="Output directory"),
    verbose: int = typer.Option(1, "--verbose", help="Verbosity level"),
) -> None:
    """Train a DRL trading agent."""
    import sys
    sys.argv = [
        "train_rl.py",
        "--symbols", symbols,
        "--agent", agent,
        "--episodes", str(episodes),
        "--timesteps", str(timesteps),
        "--learning-rate", str(learning_rate),
        "--feature-set", feature_set,
        "--output-dir", output_dir,
        "--verbose", str(verbose),
    ]

    from scripts.train_rl import main as train_main
    exit_code = train_main()
    raise typer.Exit(code=exit_code)


@app.command(name="rl-eval")
def rl_eval(
    ctx: typer.Context,
    symbols: str = typer.Option("AAPL", "--symbols", help="Comma-separated symbols to evaluate"),
    episodes: int = typer.Option(10, "--episodes", help="Number of evaluation episodes"),
) -> None:
    """Evaluate a trained DRL trading agent."""
    import sys
    sys.argv = [
        "train_rl.py",
        "--evaluate",
        "--symbols", symbols,
        "--eval-episodes", str(episodes),
    ]

    from scripts.train_rl import main as train_main
    exit_code = train_main()
    raise typer.Exit(code=exit_code)
