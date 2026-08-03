import json
from datetime import datetime, timedelta, timezone
from typing import Any
import math
from pathlib import Path
import time
import logging

import pandas as pd
import typer
import yaml

logger = logging.getLogger(__name__)


def _eod_marker_filename(
    root: Path, iso_date: str, intervals: list[str] | tuple[str, ...]
) -> Path:
    """Build the EOD marker path for a date + interval set.

    Marker filenames embed the interval set so that a backfill for one
    interval (e.g. ``1d``) does not block a subsequent backfill for a
    different interval (e.g. ``1m``) on the same date. The format is::

        .last_eod_fetch_<YYYY-MM-DD>[_<interval1>_<interval2>...].marker

    Sort the intervals so the name is deterministic regardless of call order.
    When ``intervals`` is empty, the bare ``<date>.marker`` is returned
    (CLI never passes empty in practice; this is a guard).
    """
    suffix = "_".join(sorted(intervals))
    name = (
        f".last_eod_fetch_{iso_date}_{suffix}.marker"
        if suffix
        else f".last_eod_fetch_{iso_date}.marker"
    )
    return Path(root) / name


from trading_bot.config.loader import load_settings
from trading_bot.config.settings import Settings
from trading_bot.data.indicators import add_atr, add_rsi
from trading_bot.logging_config import configure_from_settings
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

from trading_bot.runtime import session as runtime_session
from trading_bot.runtime.decision_log import append_decision_event
from trading_bot.runtime.latency import data_age_minutes, frame_last_timestamp, is_stale
from trading_bot.runtime.position_exit import (
    fill_partial_take_profit_position as _shared_fill_partial_take_profit_position,
    fill_sell_position as _shared_fill_sell_position,

)
from trading_bot.runtime.position_management import evaluate_exit_priority
from trading_bot.runtime.snapshots import read_recent_decision_rows, write_snapshot
from trading_bot.runtime.universe import merge_universe_symbols
from trading_bot.scout import build_scout_candidates
from trading_bot.strategy.trailing_stop import next_trailing_stop

app = typer.Typer(help="Paper-trading CLI for stocks and ETFs.")


def _repo_script_path(script_name: str) -> Path:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"script not found: {script_path}")
    return script_path


def now_in_zone(timezone: str) -> datetime:
    return runtime_session.now_in_zone(timezone)


def should_eod_exit(now: datetime, settings: Settings) -> bool:
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

    # Remember whether the caller passed --config-path explicitly so the
    # doctor command (and any other future subcommand) can decide
    # whether to auto-route to burn-in-config.yaml or honor the
    # operator's choice. Without this, `doctor --burn-in` would
    # silently override --config-path even though the operator picked
    # a different config. Regression for the 5319ddb bug.
    #
    # Typer creates a child context per subcommand, so we stash this
    # on ``ctx.obj`` (the loaded settings) which is shared between
    # callback and subcommand.
    explicit_config_path = config_path is not None

    if config_path is None:
        env_path = os.environ.get("CONFIG_PATH")
        if env_path:
            config_path = Path(env_path)
    ctx.obj = load_settings(config_path)
    ctx.obj._explicit_config_path = explicit_config_path
    # Stash the resolved config path so subcommands (notably ``serve``)
    # can re-export it before launching subprocesses whose module-level
    # state re-loads settings from CONFIG_PATH.
    ctx.obj._config_path = config_path
    configure_from_settings(ctx.obj)


def resolve_dashboard_port(settings) -> int:
    """Return the dashboard port to probe for health checks.

    Precedence:
      1. ``DASHBOARD_PORT`` env var (if set and parseable as int) — the
         burn-in sidecar exports this so subprocesses it spawns (including
         its own ``doctor --burn-in`` self-check) hit the right port.
      2. ``state/burn_in/dashboard.port`` — the burner writes this when
         the sidecar starts so a manual operator running ``doctor
         --burn-in`` from outside the burner can discover the actual
         sidecar port without exporting env vars.
      3. ``settings.app.dashboard_port`` — the loader's value, which is
         already env-overridden at load time. This also catches the
         ``--port`` CLI override from ``serve``.
      4. ``8000`` — the historical default documented in AGENTS.md.
    """
    import os

    env_value = os.getenv("DASHBOARD_PORT")
    if env_value:
        try:
            return int(env_value.strip())
        except ValueError:
            pass

    state_dir = _doctor_state_dir(settings)
    if state_dir is not None:
        port_file = state_dir / "burn_in" / "dashboard.port"
        try:
            text = port_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            text = ""
        if text:
            try:
                return int(text)
            except ValueError:
                pass

    configured = getattr(getattr(settings, "app", None), "dashboard_port", None)
    if isinstance(configured, int):
        return configured
    return 8000


def _pin_snapshot_state_dir(env=None) -> Path | None:
    """Return ``$PIN_DIR/state`` when the burner is pinned to an active
    snapshot, otherwise ``None``.

    The burn-in launcher captures ``HEAD`` into ``$PIN_DIR`` and runs
    ``auto-burn-in.sh`` with cwd ``$PIN_DIR``; the resident burner
    writes its heartbeat / pid / port / scan files under
    ``$PIN_DIR/state/...``, not the live worktree. ``doctor --burn-in``
    and ``resolve_dashboard_port`` must read from that location so
    manual operators (and the burner's own self-check) see the
    burner's actual health state.

    "Active" means both canonical marker files exist:

      - ``$PIN_DIR/scripts/auto-burn-in.sh`` — distinguishes a real
        snapshot from a stray directory the operator may have pointed
        ``PIN_DIR`` at by accident.
      - ``$PIN_DIR/state/burn_in/burn_in.pid`` — the burner writes
        this on every loop iteration (see
        ``scripts/auto-burn-in.sh``). Missing ⇒ the burner has been
        stopped; we must not silently read stale state from a dead
        snapshot.

    Returns ``None`` when ``PIN_DIR`` is unset or the snapshot is
    missing its markers so the caller falls back to the live worktree.
    """
    import os

    source = env if env is not None else os.environ
    raw = source.get("PIN_DIR")
    if not raw:
        return None
    pin_dir = Path(raw)
    if not (pin_dir / "scripts" / "auto-burn-in.sh").exists():
        return None
    if not (pin_dir / "state" / "burn_in" / "burn_in.pid").exists():
        return None
    return pin_dir / "state"


def _doctor_state_dir(settings):
    pin_state = _pin_snapshot_state_dir()
    if pin_state is not None:
        return pin_state

    from pathlib import Path

    app = getattr(settings, "app", None)
    if app is None:
        return None
    explicit = getattr(app, "state_dir", None)
    if explicit:
        return Path(str(explicit))
    db_path = getattr(app, "state_db_path", None)
    if db_path:
        return Path(str(db_path)).parent
    return None


def _previous_trading_day(today: "date") -> "date":
    """Return the most recent weekday strictly before ``today``.

    Saturday -> Friday, Sunday -> Friday, Monday -> Friday, etc. Used by
    ``eod-fetch`` so the CLI default never asks massive.com for a Sunday
    or holiday-eve date (which returns 404 from S3).
    """
    from datetime import timedelta

    candidate = today - timedelta(days=1)
    while candidate.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        candidate -= timedelta(days=1)
    return candidate


@app.command()
def doctor(
    ctx: typer.Context,
    burn_in: bool = typer.Option(
        False,
        "--burn-in",
        help="Run the burn-in reliability health checks (network-free).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON (implies --burn-in).",
    ),
) -> None:
    """Check local app readiness without fetching market data."""
    if not burn_in and not json_output:
        typer.echo(_format_doctor(ctx.obj))
        return

    # `doctor --burn-in` is the burner's self-check entrypoint. The
    # burner itself always uses burn-in-config.yaml, so when --burn-in
    # is requested without an explicit --config-path override, re-load
    # settings from burn-in-config.yaml regardless of what the global
    # callback loaded. This avoids the divergence where
    # `./tradebot-local doctor --burn-in` (no --config-path) reads
    # config.yaml's legacy `state/scan_results.json` and reports false-
    # positive FAILs. (Regression: 2026-07-28.)
    #
    # Only auto-route when the caller did NOT pass --config-path
    # explicitly. The callback stores the original input on
    # ``ctx._explicit_config_path`` so an operator-selected custom
    # config is honored even with --burn-in. Regression for the 5319ddb
    # silent-override bug.
    if burn_in and getattr(ctx.obj, "_explicit_config_path", True) is False:
        from trading_bot.config.loader import load_settings as _reload
        ctx.obj = _reload(Path("burn-in-config.yaml"))

    from trading_bot.health.runner import run_health_checks
    from trading_bot.health.types import HealthReport

    pin_state = _pin_snapshot_state_dir()
    state_dir_setting = pin_state or Path(
        getattr(ctx.obj.app, "state_dir", None)
        or Path(ctx.obj.app.state_db_path).parent
    )
    db_path = Path(ctx.obj.app.state_db_path)
    dashboard_port = resolve_dashboard_port(ctx.obj)
    eod_watchdog_pid_file = state_dir_setting / "burn_in" / "eod_watchdog.pid"
    scan_results_path = (
        pin_state / "burn_in" / "scan_results.json"
        if pin_state is not None
        else Path(ctx.obj.app.scan_results_path)
    )

    report: HealthReport = run_health_checks(
        state_dir=state_dir_setting,
        db_path=db_path,
        dashboard_port=dashboard_port,
        eod_watchdog_pid_file=eod_watchdog_pid_file,
        scan_results_path=scan_results_path,
    )

    if json_output:
        typer.echo(json.dumps(report.to_dict()))
    else:
        for check in report.checks:
            typer.echo(f"[burn-in] {check.name:<28} {check.status:<5} {check.detail}")
        typer.echo(
            f"Summary: worst={report.worst_status()}  "
            f"PASS={sum(1 for c in report.checks if c.status=='PASS')}  "
            f"WARN={sum(1 for c in report.checks if c.status=='WARN')}  "
            f"FAIL={sum(1 for c in report.checks if c.status=='FAIL')}"
        )

    raise typer.Exit(
        code={"PASS": 0, "WARN": 1, "FAIL": 2}[report.worst_status()]
    )


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
    # Always merge watchlist into the symbol set
    symbols = _merge_symbols(symbols, _read_universe_symbols(Path(ctx.obj.app.watchlist_path)))
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
    from pathlib import Path

    from trading_bot.learning.experiments.runtime_canary import (
        begin_runtime_canary,
        finish_runtime_canary,
    )
    from trading_bot.portfolio.ledger import PortfolioLedger
    from trading_bot.runtime.orchestrator import run_paper_trade

    parsed_symbols: list[str] = []
    for raw_value in symbols:
        parsed_symbols.extend(
            symbol.strip() for symbol in raw_value.split(",") if symbol.strip()
        )

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    runtime_canary = begin_runtime_canary(ctx.obj, ledger)

    try:
        for result in run_paper_trade(
            parsed_symbols,
            ctx.obj,
            dry_run=dry_run,
            runtime_canary=runtime_canary,
        ):
            typer.echo(result)
    finally:
        finish_runtime_canary(runtime_canary)


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
        help="Strategy to use: v2.5 or v3.",
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

    Use --strategy to select v2.5 or v3. Use --compare to run all
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
        strategies = ["v2.5", "v3"]
        comparison = run_strategy_comparison(parsed_symbols, ctx.obj, start=start, end=end, strategies=strategies)
        typer.echo("STRATEGY COMPARISON")
        typer.echo("=" * 60)
        for strat, result in comparison["results"].items():
            typer.echo(f"\n{strat.upper()}:")
            typer.echo(f"  trades={result['trades']} wins={result['wins']} losses={result['losses']}")
            typer.echo(f"  win_rate={result['win_rate']:.2f} net_pnl={result['net_pnl']:.2f}")
            typer.echo(f"  {_format_backtest_diagnostics(result)}")
        typer.echo(f"\nBest P&L: {comparison['best_pnl_strategy']}")
        typer.echo(f"Best Win Rate: {comparison['best_winrate_strategy']}")
    elif strategy:
        from trading_bot.backtest.runner import run_backtest

        if strategy not in {"v2.5", "v3"}:
            raise typer.BadParameter("strategy must be v2.5 or v3", param_hint="--strategy")
        strategy_settings = ctx.obj.model_copy(deep=True)
        strategy_settings.app.signal_mode = "serial"
        strategy_settings.strategy.use_v3_signals = strategy == "v3"
        summary = run_backtest(
            parsed_symbols,
            strategy_settings,
            start=start,
            end=end,
        )
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
        help="Deprecated. The static HTML generator has been removed.",
    ),
) -> None:
    """Deprecated: the static HTML dashboard has been removed.

    Use ``serve`` to launch the canonical FastAPI dashboard instead.
    This command is kept as a no-op alias so existing automation does
    not break; it intentionally writes no file.
    """
    typer.echo(
        "dashboard command is deprecated; use `./tradebot-local serve` for "
        "the canonical FastAPI dashboard."
    )
    typer.echo(f"(ignored --output={output})")


@app.command()
def serve(
    ctx: typer.Context,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. Defaults to localhost (127.0.0.1) for security.",
    ),
    port: int = typer.Option(
        None,
        "--port",
        help="Port to serve the live dashboard on. Defaults to app.dashboard_port (8000).",
    ),
) -> None:
    """Serve the canonical FastAPI dashboard (cohort-aware, SSE-updated).

    The legacy runtime dashboard and the static HTML generator have been
    removed; this command now delegates to the rich dashboard at
    ui/dashboard/main.py. Binds to localhost only by default per the
    security hardening policy.

    Press Ctrl-C to stop.
    """
    import os

    import uvicorn

    effective_port = port if port is not None else ctx.obj.app.dashboard_port
    # Re-export the resolved config path via CONFIG_PATH so the Uvicorn
    # string-imported ``ui.dashboard.main`` module reads the same config
    # the CLI just loaded. Without this, the dashboard's eager
    # ``DashboardState()`` re-runs against config.yaml regardless of
    # --config-path or CONFIG_PATH (root cause: uvicorn spawns a fresh
    # module import without receiving the loader's selection).
    config_path = getattr(ctx.obj, "_config_path", None)
    if config_path is not None:
        resolved = Path(config_path).resolve()
        os.environ["CONFIG_PATH"] = str(resolved)

    typer.echo(
        f"Serving rich dashboard at http://{host}:{effective_port} (Ctrl-C to stop)"
    )
    typer.echo(
        "Routes: / (HTML) | /api/portfolio | /api/evaluation-windows | "
        "/api/trades | /api/health | /api/stream (SSE)"
    )
    uvicorn.run(
        "ui.dashboard.main:app",
        host=host,
        port=effective_port,
        log_level="info",
    )


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
        )
        typer.echo(f"loop_stopped stats={json.dumps(stats.summary(), default=str)}")
    except KeyboardInterrupt:
        typer.echo("continuous_loop_stopped_by_user")


def _market_data_is_stale_for_manage(
    last_timestamp: datetime | None,
    manage_now: datetime,
    max_age_minutes: int,
) -> bool:
    """Decide if intraday data is too stale to manage positions on.

    Mirrors the after-hours awareness already used by the scan path
    (``_market_data_status``): when the market is closed, treat any bar
    from the last 24h as fresh (covers same-day EOD bars, yesterday's
    close, and recent intraday bars).  Bars older than 24h (weekends,
    holidays, multi-day gaps) are still rejected.  During market hours,
    fall through to the standard ``is_stale`` check.

    2026-07-09 fix: the prior version only allowed 12-24h old bars
    after-hours, leaving a 4-12h dead-zone where same-day EOD bars
    (typical after-hours check) were rejected.
    """
    from datetime import timedelta

    from trading_bot.runtime.orchestrator import _is_us_market_open

    if last_timestamp is None:
        return True
    age_min = data_age_minutes(last_timestamp, manage_now)
    if age_min is None:
        return True
    if not _is_us_market_open(manage_now) and age_min <= 1440:
        # After-hours: any bar from the last 24h is acceptable.
        return False
    return is_stale(last_timestamp, manage_now, max_age_minutes=max_age_minutes)


def _run_manage_positions_once(ctx: typer.Context) -> dict[str, object]:
    """Run one position-management check (EOD, stop, target, trail).

    Returns a dict with *positions*, *actions*, *lines*, and *exit_events*
    so callers (e.g. ``run-ops``) can alert without re-echoing.
    """
    from trading_bot.data import market_data
    from trading_bot.learning.experiments.runtime_canary import (
        begin_runtime_canary,
        finish_runtime_canary,
    )
    from trading_bot.safety.kill_switch import check_kill_switch_before_trade

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    runtime_canary = begin_runtime_canary(ctx.obj, ledger)

    try:
        # Idempotency guard: skip exits for tickers recently sold by a concurrent
        # process.  Two manage-positions processes can read the same stale state and
        # both try to sell the same ticker — this prevents duplicate fills.
        _EXIT_COOLDOWN_SECONDS = 120  # 2-minute window after a sell

        def _recently_existed(ticker: str) -> bool:
            ts = state.last_exited_at.get(ticker)
            if not ts:
                return False
            try:
                exited_at = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                return False
            if exited_at.tzinfo is None:
                exited_at = exited_at.replace(tzinfo=manage_now.tzinfo)
            return (manage_now - exited_at).total_seconds() < _EXIT_COOLDOWN_SECONDS

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
        state_changed = False
        exit_events: list[dict[str, object]] = []
        for ticker, position in sorted(state.positions.items()):
            # Use intraday bars for responsive trailing stop management
            try:
                frame = market_data.fetch_bars(
                    ticker,
                    ctx.obj.market_data.intraday_period,
                    ctx.obj.market_data.intraday_interval,
                    settings=ctx.obj.market_data,
                )
            except Exception as exc:
                skipped_stale += 1
                append_decision_event(
                    log_path,
                    {
                        "command": "manage-positions",
                        "ticker": ticker,
                        "status": "SKIP",
                        "reason": "market data fetch failed",
                        "error": str(exc),
                        "managed_at": manage_now.isoformat(),
                    },
                )
                lines.append(f"{ticker} SKIP reason=market-data-fetch-failed")
                continue
            last_timestamp = frame_last_timestamp(frame)
            last_price: float | None = None
            if not frame.empty and "close" in frame.columns:
                last_price = float(frame.iloc[-1]["close"])
            if _market_data_is_stale_for_manage(
                last_timestamp, manage_now, ctx.obj.market_data.max_data_age_minutes
            ):
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
                        "max_age_minutes": ctx.obj.market_data.max_data_age_minutes,
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
            # Idempotency: skip if another process already sold this ticker
            if _recently_existed(ticker):
                lines.append(f"{ticker} SKIP recently-exited-cooldown")
                continue
            min_stop_pct = ctx.obj.risk.min_stop_distance_pct
            if min_stop_pct > 0 and position.stop_loss is not None:
                min_stop = round(position.average_cost * (1.0 - min_stop_pct / 100.0), 4)
                # Only widen stops that are still protective (below entry); a
                # stop already ratcheted up by trailing should not be undone.
                if (
                    position.stop_loss > min_stop
                    and position.stop_loss < position.average_cost
                ):
                    state.positions[ticker] = position.model_copy(update={"stop_loss": min_stop})
                    position = state.positions[ticker]
                    state_changed = True

            def _counter_thesis_check():
                if (
                    not ctx.obj.counter_thesis.enabled
                    or not ctx.obj.counter_thesis.exit_on_block
                ):
                    return None
                from trading_bot.runtime.orchestrator import (
                    _evaluate_counter_thesis_for_position,
                )

                result = _evaluate_counter_thesis_for_position(
                    ticker, position, frame, ctx.obj
                )
                return result if result is not None and result.block_trade else None

            decision = evaluate_exit_priority(
                position=position,
                current_price=last_price,
                settings=ctx.obj,
                now=manage_now,
                eod_active=eod_active,
                counter_thesis_check=_counter_thesis_check,
                trailing_stop_check=lambda: _update_trailing_stop(
                    position, frame, last_price, ctx.obj
                ),
            )
            if decision.partial:
                state, event, line = _shared_fill_partial_take_profit_position(
                    ticker=ticker,
                    position=position,
                    submitted_at=manage_now,
                    last_price=last_price,
                    broker=broker,
                    ledger=ledger,
                    state=state,
                    log_path=log_path,
                    fraction=ctx.obj.paper.partial_take_profit_fraction,
                    settings=ctx.obj,
                    runtime_canary=runtime_canary,
                )
                append_decision_event(log_path, event)
                exit_events.append(event)
                actions += 1
                lines.append(line)
                continue
            if decision.reason == "trailing_stop":
                trail_update = decision.payload
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
            if decision.should_exit:
                outward_reason = {
                    "eod_exit": "eod",
                    "stop_loss": "stop",
                    "profit_target": "target",
                    "counter_thesis": "counter-thesis",
                }.get(decision.reason, decision.reason)
                state, event, line = _fill_sell_position(
                    ticker,
                    position,
                    outward_reason,
                    manage_now,
                    last_price,
                    broker,
                    ledger,
                    state,
                    log_path,
                    frame,
                    ctx.obj,
                    runtime_canary=runtime_canary,
                    exit_reason=decision.reason,
                )
                if decision.reason == "counter_thesis":
                    event["counter_thesis"] = decision.payload.to_dict()
                append_decision_event(log_path, event)
                exit_events.append(event)
                actions += 1
                lines.append(line)
                continue
            lines.append(
                f"{ticker} qty={position.quantity} "
                f"avg={position.average_cost:.2f} last={last_price:.2f}"
            )
        if state_changed:
            ledger.save_portfolio_state(state)
            ledger.record_equity_snapshot(state, timestamp=manage_now)
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
    finally:
        finish_runtime_canary(runtime_canary)

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


@app.command()
def deposit(
    ctx: typer.Context,
    amount: float = typer.Option(
        ...,
        "--amount",
        help="Cash to add to the ledger (use a negative value to withdraw).",
    ),
    note: str = typer.Option(
        "",
        "--note",
        help="Optional note recorded with the deposit in the decision log.",
    ),
) -> None:
    """Add (or withdraw) cash from the simulated ledger for trading."""
    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.deposit(amount)
    log_path = Path(ctx.obj.app.log_dir) / "decision-log.jsonl"
    append_decision_event(
        log_path,
        {
            "command": "deposit",
            "amount": round(amount, 2),
            "cash_after": round(state.cash, 2),
            "equity_after": round(state.equity, 2),
            "note": note,
            "at": now_in_zone(ctx.obj.app.timezone).isoformat(),
        },
    )
    typer.echo(
        f"deposit={amount:+.2f} cash={state.cash:.2f} equity={state.equity:.2f}"
    )


@app.command(name="paper-audit")
def paper_audit(ctx: typer.Context) -> None:
    """Check local paper-mode state for obvious drift."""
    from datetime import datetime as dt, timezone

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    orders = ledger.list_order_rows()
    equity_history = ledger.list_recent_equity_history(limit=1)
    snapshot_path = Path(ctx.obj.app.portfolio_summary_path)
    snapshot = _load_json_snapshot(snapshot_path)
    snapshot_generated_at = _parse_snapshot_timestamp(snapshot.get("generated_at") if snapshot else None)
    now = dt.now(timezone.utc)
    issues = _collect_paper_audit_issues(
        state,
        orders,
        equity_history,
        snapshot,
        snapshot_generated_at=snapshot_generated_at,
        now=now,
    )

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


@app.command(name="trade-attribution")
def trade_attribution(ctx: typer.Context) -> None:
    """Show P&L breakdown by ticker: entries, exits, and strategy tags."""
    from datetime import datetime as dt
    import json as json_module

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    orders = ledger.list_order_rows()
    decision_log_path = Path(ctx.obj.app.log_dir) / "decision-log.jsonl"

    last_buy: dict[str, dict] = {}
    sells: list[dict] = []
    for o in orders:
        if o["side"] == "BUY":
            last_buy[o["ticker"]] = o
        else:
            sells.append(o)

    print(f'{"Ticker":8} {"Strategy":24} {"Buy@":>9} {"Sell@":>9} {"Qty":>5} {"P&L":>10} {"Ret%":>7} {"Held":>8}')
    print("-" * 90)
    total_pnl = 0.0
    win_pnl = 0.0
    loss_pnl = 0.0
    wins = 0
    losses = 0
    strategy_pnl: dict[str, float] = {}
    strategy_count: dict[str, int] = {}
    prior_buys: dict[str, list[dict]] = {}
    for o in orders:
        if o["side"] == "BUY":
            prior_buys.setdefault(o["ticker"], []).append(o)
    for s in sorted(sells, key=lambda x: x.get("filled_at", "")):
        t = s["ticker"]
        pnl = float(s.get("pnl", 0.0) or 0.0)
        b = None
        s_at = s.get("filled_at", "")
        if t in prior_buys:
            for i, candidate in enumerate(reversed(prior_buys[t])):
                if candidate.get("filled_at", "") < s_at:
                    b = candidate
                    actual_idx = len(prior_buys[t]) - 1 - i
                    prior_buys[t].pop(actual_idx)
                    break
        buy_price = b["fill_price"] if b else 0.0
        sell_price = s["fill_price"]
        ret = (sell_price - buy_price) / max(buy_price, 0.01) * 100 if buy_price > 0 else 0.0
        held = "N/A"
        b_at = b.get("filled_at", "") if b else ""
        if b_at and s_at:
            try:
                d = dt.fromisoformat(s_at) - dt.fromisoformat(b_at)
                held = f"{int(d.total_seconds()//60)}m"
            except (ValueError, TypeError):
                pass
        tag = s.get("strategy_tag", "") or (b.get("strategy_tag", "") if b else "") or "unknown"
        print(f"{t:8} {tag[:24]:24} {buy_price:9.2f} {sell_price:9.2f} {s['quantity']:5} {pnl:10.2f} {ret:7.2f}% {held:>8}")
        total_pnl += pnl
        if pnl > 0:
            wins += 1
            win_pnl += pnl
        else:
            losses += 1
            loss_pnl += abs(pnl)
        strategy_pnl[tag] = strategy_pnl.get(tag, 0.0) + pnl
        strategy_count[tag] = strategy_count.get(tag, 0) + 1
    print("-" * 90)
    print(f'{"":8} {"TOTAL":24} {"":9} {"":9} {"":5} {total_pnl:10.2f}')
    wl = wins + losses
    if wl > 0:
        print(f"\nWins: {wins}, Losses: {losses}, Win rate: {wins/wl*100:.0f}%")
        pf = "inf" if loss_pnl == 0 else f"{win_pnl / loss_pnl:.2f}"
        print(f"Profit factor: {pf}")
    if strategy_pnl:
        print(f"\n{'Strategy':30} {'Trades':>7} {'P&L':>10}")
        print("-" * 50)
        for tag in sorted(strategy_pnl, key=lambda k: strategy_pnl[k]):
            print(f"{tag[:30]:30} {strategy_count[tag]:7} {strategy_pnl[tag]:10.2f}")


@app.command(name="paper-report")
def paper_report(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a JSON payload alongside the human-readable report.",
    ),
    since: str = typer.Option(
        None,
        "--since",
        help="UTC ISO datetime; include only rows on or after this timestamp.",
    ),
    until: str = typer.Option(
        None,
        "--until",
        help="UTC ISO datetime; include only rows on or before this timestamp.",
    ),
) -> None:
    """Multi-dimensional P&L report: overall, per strategy, per hour, per ticker."""
    from trading_bot.analytics import format_paper_performance_report, summarize_paper_performance

    since_dt = _parse_utc_datetime(since)
    until_dt = _parse_utc_datetime(until)

    db_path = Path(ctx.obj.app.state_db_path)
    if not db_path.exists():
        typer.echo(f"DB not found at {db_path}")
        raise typer.Exit(code=1)

    report = summarize_paper_performance(
        db_path=db_path,
        since=since_dt,
        until=until_dt,
        naive_timezone=ctx.obj.app.timezone,
    )
    typer.echo(format_paper_performance_report(report))
    if json_output:
        import json

        typer.echo(
            json.dumps(
                {
                    "total_trades": report.total_trades,
                    "winning_trades": report.winning_trades,
                    "losing_trades": report.losing_trades,
                    "realized_pnl": report.realized_pnl,
                    "gross_wins": report.gross_wins,
                    "gross_losses": report.gross_losses,
                    "profit_factor": (
                        report.profit_factor
                        if report.profit_factor != float("inf")
                        else None
                    ),
                    "win_rate": report.win_rate,
                    "evaluation_window": {
                        "start": (
                            report.evaluation_window.start.isoformat()
                            if report.evaluation_window.start
                            else None
                        ),
                        "end": (
                            report.evaluation_window.end.isoformat()
                            if report.evaluation_window.end
                            else None
                        ),
                    },
                    "by_strategy": [
                        {
                            "label": r.label,
                            "trades": r.trades,
                            "wins": r.wins,
                            "losses": r.losses,
                            "net_pnl": r.net_pnl,
                            "profit_factor": (
                                r.profit_factor
                                if r.profit_factor != float("inf")
                                else None
                            ),
                        }
                        for r in report.by_strategy
                    ],
                    "by_hour": [
                        {
                            "label": r.label,
                            "trades": r.trades,
                            "net_pnl": r.net_pnl,
                            "wins": r.wins,
                            "losses": r.losses,
                        }
                        for r in report.by_hour
                    ],
                    "by_ticker": [
                        {
                            "label": r.label,
                            "trades": r.trades,
                            "net_pnl": r.net_pnl,
                            "profit_factor": (
                                r.profit_factor
                                if r.profit_factor != float("inf")
                                else None
                            ),
                        }
                        for r in report.by_ticker
                    ],
                },
                default=str,
            )
        )


def _graduation_recommend(report) -> str:
    """Map a report to one of the AGENTS.md decision buckets.

    Uses AGENTS.md's "100 closed trades" check; before that threshold
    the recommendation is "keep accumulating evidence" so operators do
    not stop the run on small samples.
    """
    if report.total_trades < 100:
        return (
            f"KEEP ACCUMULATING: only {report.total_trades}/100 closed trades. "
            "Do not stop the burn-in before AGENTS.md's 100-trade gate."
        )
    pf = report.profit_factor
    if pf >= 1.3:
        return f"GRADUATE TO LIVE CONSIDERATION: PF={pf:.2f} >= 1.3 over {report.total_trades} closed trades."
    if pf >= 0.8:
        return f"CONTINUE PAPER TUNING: PF={pf:.2f} in 0.8-1.3 over {report.total_trades} closed trades."
    return f"ADVISORY: PF={pf:.2f} < 0.8 over {report.total_trades} closed trades. Review decision-log.jsonl."


def _parse_utc_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


@app.command(name="graduation-check")
def graduation_check(
    ctx: typer.Context,
    since: str | None = typer.Option(
        None,
        "--since",
        help="UTC ISO datetime; overrides paper.graduation_since.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="UTC ISO datetime; include only rows on or before this timestamp.",
    ),
) -> None:
    """Run AGENTS.md's 100-trade graduation gate against current paper DB.

    Reports overall PF/win-rate and prints a single recommendation that
    mirrors the AGENTS.md decision flow:
      PF > 1.3  → graduate to live trading consideration
      0.8–1.3  → continue paper tuning
      < 0.8     → advisory alert; review decision-log.jsonl
    Below 100 closed trades the recommendation is to keep accumulating
    evidence regardless of PF, so a small sample does not trigger a
    premature stop.
    """
    from trading_bot.analytics import (
        format_paper_performance_report,
        summarize_paper_performance,
    )

    db_path = Path(ctx.obj.app.state_db_path)
    if not db_path.exists():
        typer.echo(f"DB not found at {db_path}")
        raise typer.Exit(code=1)

    since_dt = _parse_utc_datetime(
        since if since is not None else ctx.obj.paper.graduation_since
    )
    until_dt = _parse_utc_datetime(until)
    report = summarize_paper_performance(
        db_path=db_path,
        since=since_dt,
        until=until_dt,
        naive_timezone=ctx.obj.app.timezone,
    )
    typer.echo(format_paper_performance_report(report))
    typer.echo("")
    typer.echo(_graduation_recommend(report))

    pf = report.profit_factor
    exit_code = 0 if pf >= 1.3 and report.total_trades >= 100 else 1
    if pf < 0.8 and report.total_trades >= 100:
        exit_code = 2
    raise typer.Exit(code=exit_code)


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


def _parse_snapshot_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _ledger_exposure(state: PortfolioState) -> float:
    return sum(
        position.quantity * position.average_cost for position in state.positions.values()
    )


def _collect_paper_audit_issues(
    state: PortfolioState,
    orders: list[dict[str, object]],
    equity_history: list[dict[str, object]],
    snapshot: dict[str, object],
    snapshot_generated_at: datetime | None = None,
    now: datetime | None = None,
) -> list[str]:
    issues: list[str] = []
    if snapshot_generated_at is not None and snapshot_generated_at.tzinfo is None:
        snapshot_generated_at = snapshot_generated_at.replace(tzinfo=timezone.utc)
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
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

        if "realized_pnl" in summary and round(
            float(summary.get("realized_pnl", state.realized_pnl)), 2
        ) != round(state.realized_pnl, 2):
            issues.append("portfolio snapshot realized_pnl does not match ledger state")
        if "unrealized_pnl" in summary and round(
            float(summary.get("unrealized_pnl", state.unrealized_pnl)), 2
        ) != round(state.unrealized_pnl, 2):
            issues.append("portfolio snapshot unrealized_pnl does not match ledger state")
        if "exposure" in summary:
            ledger_exposure = round(_ledger_exposure(state), 2)
            snapshot_exposure = round(float(summary.get("exposure", ledger_exposure)), 2)
            if snapshot_exposure != ledger_exposure:
                issues.append("portfolio snapshot exposure does not match ledger state")

        if (
            snapshot_generated_at is not None
            and now is not None
            and now - snapshot_generated_at > timedelta(hours=24)
        ):
            issues.append(
                f"portfolio snapshot is stale "
                f"({(now - snapshot_generated_at).total_seconds() / 3600:.1f}h old)"
            )
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
        else:
            notify(ctx.obj, "info", "BUY Signal", _format_signal_alert(candidates))
            typer.echo(f"alerts={len(candidates)}")


def _parse_symbols(values: list[str]) -> list[str]:
    parsed_symbols: list[str] = []
    for raw_value in values:
        parsed_symbols.extend(symbol.strip() for symbol in raw_value.split(",") if symbol.strip())
    return parsed_symbols


def _merge_symbols(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw_symbol in group:
            symbol = raw_symbol.upper().strip()
            if symbol and symbol not in seen:
                seen.add(symbol)
                merged.append(symbol)
    return merged


def _apply_advisory_symbol_overrides(symbols: list[str], settings, limit: int | None = None) -> list[str]:
    from trading_bot.advisory import apply_scout_override

    return apply_scout_override(symbols, settings, limit=limit)


def _apply_advisory_candidate_overrides(
    candidates: list[dict[str, object]],
    symbols: list[str],
    settings,
) -> list[dict[str, object]]:
    final_symbols = _apply_advisory_symbol_overrides(
        symbols,
        settings,
        limit=settings.scout.max_universe_size,
    )
    symbol_set = set(final_symbols)
    by_ticker = {
        str(candidate.get("ticker", "")).upper().strip(): dict(candidate)
        for candidate in candidates
        if str(candidate.get("ticker", "")).strip()
    }
    for rank, ticker in enumerate(final_symbols, start=1):
        candidate = by_ticker.get(ticker, {"ticker": ticker, "scout_score": 0.0, "source_hits": 0, "source_names": [], "market_cap": None, "price": None, "avg_dollar_volume": 0.0, "volume_ratio": None, "reasons": []})
        reasons = list(candidate.get("reasons", [])) if isinstance(candidate.get("reasons"), list) else []
        if ticker not in by_ticker:
            reasons.append("advisory promoted symbol")
        candidate.update({"included": True, "rank": rank, "reasons": reasons})
        by_ticker[ticker] = candidate
    for ticker, candidate in by_ticker.items():
        if ticker not in symbol_set:
            candidate["included"] = False
            candidate["rank"] = None
    ordered = sorted(
        by_ticker.values(),
        key=lambda candidate: (
            not bool(candidate.get("included")),
            int(candidate.get("rank") or 999999),
            -float(candidate.get("scout_score", 0.0) or 0.0),
            str(candidate.get("ticker", "")),
        ),
    )
    return ordered


def _format_backtest_diagnostics(result: dict) -> str:
    return (
        f"avg_win={result.get('avg_win', 0.0):.2f} "
        f"avg_loss={result.get('avg_loss', 0.0):.2f} "
        f"expectancy={result.get('expectancy', 0.0):.2f} "
        f"profit_factor={result.get('profit_factor', 0.0):.2f} "
        f"pnl_per_trade={result.get('pnl_per_trade', 0.0):.2f}"
    )


def _build_universe_file(settings) -> dict[str, object]:
    from trading_bot.data import market_data
    from trading_bot.advisory import load_scout_override

    fetch_limit = max(settings.scout.max_universe_size, settings.scout.max_snapshot_candidates)
    rows = market_data.fetch_small_cap_candidates(
        limit=fetch_limit,
        screeners=settings.scout.screeners,
    )
    scout_result = build_scout_candidates(rows, settings.scout, advisory_override=load_scout_override(settings))
    path = Path(settings.app.universe_path)
    previous_symbols = _read_universe_symbols(path)
    static_symbols: list[str] = []
    if settings.scout.static_core_path:
        static_path = Path(settings.scout.static_core_path)
        if static_path.exists():
            static_symbols = _read_universe_symbols(static_path)
    watchlist_symbols = _read_universe_symbols(Path(settings.app.watchlist_path))
    included_symbols, preserved_previous = merge_universe_symbols(
        static_symbols,
        watchlist_symbols,
        scout_result.included_symbols,
        previous_symbols if settings.scout.preserve_previous_on_underflow else [],
        max_size=settings.scout.max_universe_size,
        min_size=settings.scout.min_universe_size,
    )
    lines = [
        " ".join(
            [
                str(candidate.rank),
                str(candidate.ticker),
                f"score={float(candidate.scout_score):.2f}",
                f"price={float(candidate.price or 0.0):.2f}",
                f"market_cap={int(candidate.market_cap or 0)}",
                f"avg_dollar_volume={float(candidate.avg_dollar_volume):.2f}",
                f"source_hits={int(candidate.source_hits)}",
                f"reasons={'; '.join(candidate.reasons)}",
            ]
        )
        for candidate in scout_result.candidates
        if candidate.included
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text("".join(f"{symbol}\n" for symbol in included_symbols), encoding="utf-8")
    tmp_path.replace(path)

    snapshot_limit = max(settings.scout.max_universe_size, settings.scout.max_snapshot_candidates)
    scout_dump = scout_result.model_dump()
    write_snapshot(
        settings.app.universe_candidates_path,
        {
            "mode": "universe",
            "summary": scout_dump["summary"],
            "candidates": scout_dump["candidates"][:snapshot_limit],
        },
    )
    lines.append(
        f"summary candidates={scout_result.summary.candidates} "
        f"included={scout_result.summary.included} "
        f"excluded={scout_result.summary.excluded} "
        f"errors={scout_result.summary.errors} path={path}"
    )
    lines.append(
        f"universe merged={len(included_symbols)} "
        f"static={len(static_symbols)} watchlist={len(watchlist_symbols)} "
        f"previous={len(previous_symbols)} preserved_previous={preserved_previous}"
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
    from trading_bot.models.scout import UniverseCandidatesSnapshot

    snapshot = _load_json_snapshot(Path(settings.app.universe_candidates_path))
    if snapshot:
        parsed = UniverseCandidatesSnapshot.model_validate(snapshot)
        ranked = [
            candidate
            for candidate in parsed.candidates
            if candidate.included and candidate.ticker.strip()
        ]
        if ranked:
            ranked.sort(key=lambda candidate: candidate.rank or 999999)
            return _apply_advisory_symbol_overrides(
                [candidate.ticker.strip() for candidate in ranked],
                settings,
                limit=settings.scout.max_universe_size,
            )
    return _apply_advisory_symbol_overrides(
        _read_universe_symbols(Path(settings.app.universe_path)),
        settings,
        limit=settings.scout.max_universe_size,
    )


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
    bars=None,
    settings=None,
    runtime_canary=None,
    exit_reason: str | None = None,
) -> tuple:
    """Submit a market SELL order, record the fill, and update portfolio state."""
    exit_rsi = None
    exit_atr = None
    hold_duration = None
    exit_strategy = None

    if bars is not None and not bars.empty:
        try:
            bar_copy = bars.copy()
            bar_copy = add_rsi(bar_copy, period=14)
            rsi_val = bar_copy["rsi_14"].iloc[-1] if "rsi_14" in bar_copy.columns else None
            if rsi_val is not None and not (isinstance(rsi_val, float) and pd.isna(rsi_val)):
                exit_rsi = float(rsi_val)
        except (KeyError, ValueError):
            exit_rsi = None
        try:
            bar_copy = bars.copy()
            bar_copy = add_atr(bar_copy, period=14)
            atr_val = bar_copy["atr_14"].iloc[-1] if "atr_14" in bar_copy.columns else None
            if atr_val is not None and not (isinstance(atr_val, float) and pd.isna(atr_val)):
                exit_atr = float(atr_val)
        except (KeyError, ValueError):
            exit_atr = None

    if position.entry_at is not None:
        entry_at = position.entry_at
        if entry_at.tzinfo is None:
            entry_at = entry_at.replace(tzinfo=submitted_at.tzinfo or timezone.utc)
        hold_duration = (submitted_at - entry_at).total_seconds() / 60.0

    exit_strategy = getattr(position, "strategy_tag", None)

    return _shared_fill_sell_position(
        ticker=ticker,
        position=position,
        reason=reason,
        submitted_at=submitted_at,
        last_price=last_price,
        broker=broker,
        ledger=ledger,
        state=state,
        log_path=log_path,
        exit_rsi=exit_rsi,
        exit_atr=exit_atr,
        hold_duration_minutes=hold_duration,
        exit_strategy=exit_strategy,
        exit_reason=exit_reason or reason,
        settings=settings,
        runtime_canary=runtime_canary,
    )


def _paper_broker_from_state(state: PortfolioState, settings) -> PaperBroker:
    broker = PaperBroker(
        starting_cash=state.cash,
        fee_per_order=settings.paper.fee_per_order,
        slippage_bps=settings.paper.slippage_bps,
        dynamic_slippage_enabled=settings.paper.dynamic_slippage_enabled,
        dynamic_slippage_notional_bps_per_10k=settings.paper.dynamic_slippage_notional_bps_per_10k,
        dynamic_slippage_low_price_boost_bps=settings.paper.dynamic_slippage_low_price_boost_bps,
        dynamic_slippage_max_extra_bps=settings.paper.dynamic_slippage_max_extra_bps,
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
            settings=settings.market_data,
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
            settings=settings.market_data,
        )
    except Exception:
        frame = None
    if frame is not None and not frame.empty:
        return frame
    try:
        frame = market_data.fetch_bars(symbol, "1mo", "1d", settings=settings.market_data)
    except Exception:
        return None
    return frame


def _format_scan_summary(summary: dict[str, object]) -> str:
    parts = [
        "summary",
        f"symbols={summary['symbols']}",
        f"approved={summary['approved']}",
        f"green={summary['green']}",
        f"yellow={summary['yellow']}",
        f"rejected={summary['rejected']}",
        f"no_signal={summary['no_signal']}",
        f"errors={summary['errors']}",
    ]
    if "supermodel_support" in summary:
        parts.extend(
            [
                f"supermodel_support={summary['supermodel_support']}",
                f"supermodel_caution={summary['supermodel_caution']}",
                f"supermodel_block={summary['supermodel_block']}",
                f"supermodel_no_signal={summary['supermodel_no_signal']}",
            ]
        )
    return " ".join(parts)


def _format_doctor(settings) -> str:
    from trading_bot.data.providers.registry import provider_readiness

    snapshots = [
        settings.app.scan_results_path,
        settings.app.portfolio_summary_path,
        settings.app.dashboard_summary_path,
        settings.app.backtest_summary_path,
    ]
    ready_snapshots = sum(1 for path in snapshots if Path(path).exists())

    providers = list(getattr(settings.market_data, "provider_stack", []) or ["yfinance"])
    provider = ",".join(providers)
    provider_statuses: list[str] = []
    for provider_name in providers:
        readiness = provider_readiness(provider_name)
        provider_statuses.append(f"{provider_name}:{readiness.reason}")
    provider_auth = ",".join(provider_statuses)

    return " ".join(
        [
            "doctor",
            f"live_trading={str(settings.app.live_trading_enabled).lower()}",
            f"state_db={_exists_label(settings.app.state_db_path)}",
            f"log_dir={_exists_label(settings.app.log_dir)}",
            f"snapshots={ready_snapshots}/{len(snapshots)}",
            f"provider={provider}",
            f"provider_auth={provider_auth}",
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


@app.command(name="tune")
def tune(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview overrides without writing them."),
) -> None:
    """Generate safe burn-in tuning overrides from recent paper results."""
    from trading_bot.learning.experiments.store import ExperimentStore
    from trading_bot.learning.tuning_overrides import (
        propose_tuning_overrides,
        write_tuning_overrides,
    )

    if not dry_run:
        experiment_store = ExperimentStore(
            root=Path(ctx.obj.app.state_db_path).parent / "tuning_experiments"
        )
        if experiment_store.load_current() is not None:
            typer.echo(
                "Tuning experiment is active; run `tune-experiment evaluate` "
                "or `rollback` instead."
            )
            raise typer.Exit(code=2)

    proposal = propose_tuning_overrides(
        Path(ctx.obj.app.log_dir),
        ctx.obj,
        Path(ctx.obj.app.scan_results_path),
    )
    rendered = yaml.safe_dump(proposal, sort_keys=False).strip()
    if dry_run:
        typer.echo("DRY RUN")
        typer.echo(rendered)
        return

    output_path = Path(ctx.obj.app.tuning_overrides_path)
    write_tuning_overrides(output_path, proposal)
    typer.echo(f"Wrote tuning overrides to {output_path}")
    typer.echo(rendered)


def _format_tune_experiment_status(payload: dict) -> str:
    """Render ``controller.status()`` for human consumption.

    Operators need a single, copy-pasteable block per state. The empty
    state stays quiet ("No active experiment") so dashboards and CI logs
    do not flag it as an anomaly.
    """
    if not payload.get("active"):
        return "tune_experiment active=false\nNo active experiment."
    lines = [
        f"tune_experiment active=true id={payload.get('experiment_id', '?')}",
        f"status={payload.get('status', '?')}",
        f"change={payload.get('change', {}).get('section', '?')}.{payload.get('change', {}).get('field', '?')} "
        f"baseline={payload.get('change', {}).get('baseline', '?')} "
        f"candidate={payload.get('change', {}).get('candidate', '?')}",
        f"canary_closed_trades={payload.get('canary_closed_trades', 0)}",
    ]
    return "\n".join(lines)


@app.command(name="tune-experiment")
def tune_experiment(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="propose | status | evaluate | rollback"),
    reason: str | None = typer.Option(None, "--reason", help="Operator note for rollback."),
    json_output: bool = typer.Option(False, "--json", help="JSON output."),
) -> None:
    """Drive the tuning experiment controller."""
    from trading_bot.learning.experiments.controller import ExperimentController
    from trading_bot.learning.experiments.replay import StoredBarLoader
    from trading_bot.learning.experiments.store import ExperimentStore

    settings = ctx.obj
    store = ExperimentStore(
        root=Path(settings.app.state_db_path).parent / "tuning_experiments"
    )
    bar_loader = (
        StoredBarLoader(
            root=Path(settings.eod_data_store.store_root),
            manifest_db=Path(settings.eod_data_store.manifest_db),
        )
        if settings.eod_data_store.enabled
        else None
    )
    overrides_path = Path(
        getattr(settings.app, "tuning_overrides_path", None)
        or (Path(settings.app.state_db_path).parent / "tuning_overrides.yaml")
    )
    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=bar_loader,
        overrides_path=overrides_path,
    )

    if action == "status":
        payload = controller.status()
        if json_output:
            typer.echo(json.dumps(payload, indent=2, default=str))
            return
        typer.echo(_format_tune_experiment_status(payload))
        return

    if action == "propose":
        state = controller.propose()
        if state is None:
            typer.echo(
                "No experiment started (active experiment already exists, "
                "or no proposed change)."
            )
            raise typer.Exit(code=0)
        typer.echo(f"Proposed experiment {state.experiment_id}")
        typer.echo(state.change.model_dump_json(indent=2))
        return

    if action == "evaluate":
        state = controller.evaluate()
        if state is None:
            typer.echo("No active experiment to evaluate.")
            raise typer.Exit(code=2)
        typer.echo(_format_tune_experiment_status(controller.status()))
        return

    if action == "rollback":
        state = controller.rollback(reason=reason)
        if state is None:
            typer.echo("No active experiment to roll back.")
            raise typer.Exit(code=2)
        typer.echo(f"Rolled back experiment {state.experiment_id}")
        return

    raise typer.BadParameter(f"unknown action {action!r}")


@app.command(name="pattern-mine")
def pattern_mine(
    ctx: typer.Context,
    lookback_days: int = typer.Option(
        90,
        "--lookback-days",
        help="Number of days to look back for pattern mining.",
    ),
) -> None:
    """Run the pattern mining pass over historical EOD data."""
    import logging
    from trading_bot.patterns.miner import mine_patterns
    from trading_bot.patterns.digest import generate_digest

    settings = ctx.obj
    cfg = settings.eod_data_store

    if not cfg.enabled:
        typer.echo("eod_data_store disabled in config, cannot mine patterns.")
        raise typer.Exit(code=1)

    store_root = Path(cfg.store_root)
    manifest_db = Path(cfg.manifest_db)

    typer.echo(f"Mining patterns over last {lookback_days} days...")

    patterns = mine_patterns(
        store_root=store_root,
        manifest_db=manifest_db,
        lookback_days=lookback_days
    )

    if not patterns:
        typer.echo("No patterns found. Check if EOD data exists.")
        raise typer.Exit(code=1)

    output_dir = Path("state/patterns")
    generate_digest(patterns=patterns, output_dir=output_dir)

    typer.echo(f"Found {len(patterns)} patterns. Digests written to {output_dir}")

@app.command(name="eod-fetch")
def eod_fetch(
    ctx: typer.Context,
    as_of_date: str = typer.Option(
        None,
        "--date",
        help="Trading date YYYY-MM-DD. Default: yesterday (ET).",
    ),
    backfill_days: int = typer.Option(
        0,
        "--backfill-days",
        help="If >0, also fetch the previous N days (in addition to --date).",
    ),
    intervals: str = typer.Option(
        None,
        "--intervals",
        help=(
            "Comma-separated, e.g. '1d,1m,quotes,trades'. Default: from "
            "eod_data_store.intervals. Valid: '1d' (day-aggregates), "
            "'1m' (minute-aggregates), 'quotes', 'trades'."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print plan without making network calls or writes.",
    ),
) -> None:
    """Fetch massive.com S3 flat-files for the configured universe and write
    them into the long-term data store (``state/data_store/``).

    Runs end-of-day after massive.com publishes the previous session's bars
    (≈11:00 AM ET). Idempotent — re-runs for the same date are no-ops.
    """
    from datetime import date as _date, timedelta

    from trading_bot.data.data_store import DataStoreManifest
    from trading_bot.data.eod_runner import run_eod_fetch

    if as_of_date is None:
        as_of_date = _previous_trading_day(_date.today()).isoformat()

    settings = ctx.obj
    cfg = settings.eod_data_store
    if not cfg.enabled:
        typer.echo("eod_data_store disabled in config")
        return

    if as_of_date is None:
        from zoneinfo import ZoneInfo

        as_of_date = _previous_trading_day(
            datetime.now(ZoneInfo("America/New_York")).date()
        ).isoformat()
    target = _date.fromisoformat(as_of_date)

    universe_path = Path(settings.app.universe_path)
    manifest_db = Path(cfg.manifest_db)
    root = Path(cfg.store_root)

    chosen_intervals = (
        [s.strip() for s in intervals.split(",") if s.strip()]
        if intervals
        else list(cfg.intervals)
    )
    marker = _eod_marker_filename(root, target.isoformat(), chosen_intervals)

    if dry_run:
        typer.echo(
            f"DRY RUN: would fetch date={target} intervals={chosen_intervals} "
            f"universe={universe_path} store_root={root}"
        )
        return

    written = 0
    days = [target - timedelta(days=i) for i in range(backfill_days + 1)]
    for day in days:
        day_marker = _eod_marker_filename(root, day.isoformat(), chosen_intervals)
        if day_marker.exists():
            typer.echo(f"eod-fetch={day.isoformat()} skipped (marker exists)")
            continue
        manifest = DataStoreManifest(db_path=manifest_db)
        n = run_eod_fetch(
            settings=settings,
            universe_path=universe_path,
            manifest_db=manifest_db,
            as_of_date=day,
            marker_file=day_marker,
            intervals=chosen_intervals,
        )
        written += n
        typer.echo(f"eod-fetch={day.isoformat()} partitions={n}")

    typer.echo(f"eod-fetch total_partitions={written}")


@app.command()
def drawdown(ctx: typer.Context) -> None:
    """Show drawdown analysis from equity history."""
    from trading_bot.monitoring.drawdown import (
        compute_drawdown_from_ledger,
        format_drawdown_report,
    )

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    boundary = ctx.obj.paper.equity_evaluation_since or ctx.obj.paper.graduation_since
    metrics = compute_drawdown_from_ledger(
        ledger,
        limit=None,
        since=boundary,
        naive_timezone=ctx.obj.app.timezone,
    )
    if boundary is not None:
        typer.echo(f"Equity evaluation since {boundary.isoformat()}")
    if not metrics.sufficient_evidence:
        if metrics.sample_size == 0:
            typer.echo(format_drawdown_report(metrics))
            return
        typer.echo(
            "Insufficient cohort evidence (<2 snapshots); drawdown reporting skipped."
        )
        return
    typer.echo(format_drawdown_report(metrics))

    if metrics.max_drawdown_pct > ctx.obj.monitoring.max_drawdown_pct:
        typer.echo(
            f"\n⚠️  Max drawdown {metrics.max_drawdown_pct:.2f}% exceeds limit "
            f"{ctx.obj.monitoring.max_drawdown_pct:.2f}%"
        )
        raise typer.Exit(code=1)


@app.command(name="advisory-learn")
def advisory_learn(
    ctx: typer.Context,
    daily_report: bool = typer.Option(
        False,
        "--daily-report",
        help="Also write a markdown daily report.",
    ),
) -> None:
    """Run the advisory learner once.

    Reads recent decision-log events, derives a main_midcap + cheap_stocks
    recommendation report, and writes the latest report + scout override
    YAML. Opt-in via ``advisory.enabled``; no-ops cleanly when disabled.
    """
    from trading_bot.advisory import run_advisory_learner

    settings = ctx.obj
    if not settings.advisory.enabled:
        typer.echo("advisory=disabled")
        return

    summary = run_advisory_learner(settings, write_daily_report=daily_report)
    typer.echo(
        f"observations_added={summary.observations_added} "
        f"main={summary.main_recommendations} "
        f"cheap={summary.cheap_recommendations} "
        f"promote={len(summary.promoted_symbols)} "
        f"avoid={len(summary.avoided_symbols)}"
    )


@app.command(name="advisory-report")
def advisory_report(ctx: typer.Context) -> None:
    """Print the most recent advisory learner report."""
    from trading_bot.advisory import load_latest_advisory_report
    from trading_bot.advisory.reporting import format_advisory_report

    settings = ctx.obj
    report = load_latest_advisory_report(settings)
    if not report:
        typer.echo("No advisory report available yet — run advisory-learn first.")
        return
    typer.echo(format_advisory_report(report))


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
    equity_boundary = ctx.obj.paper.equity_evaluation_since or ctx.obj.paper.graduation_since
    if equity_boundary is not None:
        typer.echo(f"Cohort equity boundary: {equity_boundary.isoformat()}")
    dd_metrics = compute_drawdown_from_ledger(
        ledger,
        limit=None,
        since=equity_boundary,
        naive_timezone=ctx.obj.app.timezone,
    )
    if not dd_metrics.sufficient_evidence:
        if dd_metrics.sample_size == 0:
            typer.echo(format_drawdown_report(dd_metrics))
        else:
            typer.echo(
                "Insufficient cohort evidence (<2 snapshots); drawdown reporting skipped."
            )
    else:
        typer.echo(format_drawdown_report(dd_metrics))
    typer.echo("")

    # 2. VaR
    typer.echo("--- Value at Risk ---")
    tickers = sorted(t for t, p in state.positions.items() if p.quantity > 0)
    latest_prices: dict[str, float] = {}
    if tickers:
        latest_prices = _fetch_latest_prices(tickers, settings)
        price_history: dict[str, list[float]] = {}
        for ticker in tickers:
            try:
                bars = fetch_bars(ticker, period="1y", interval="1d", settings=settings.market_data)
                if not bars.empty:
                    close_column = "close" if "close" in bars.columns else ("Close" if "Close" in bars.columns else None)
                    if close_column is not None:
                        price_history[ticker] = [float(c) for c in bars[close_column].tolist()]
            except Exception as e:
                logger.debug("Error in CLI: %s", e)

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

    else:
        typer.echo("No open positions — skipping VaR, correlation, stress tests.")

    # 5. Real-time PnL / trade quality
    typer.echo("--- PnL Alerts ---")
    snapshot = calculate_realtime_pnl(ledger, latest_prices)
    typer.echo(
        " ".join(
            [
                f"closed_trades={snapshot.closed_trades}",
                f"win_rate={snapshot.win_rate_pct:.1f}%",
                f"profit_factor={snapshot.profit_factor:.2f}",
            ]
        )
    )
    pnl_alerts = check_pnl_alerts(snapshot)
    if pnl_alerts:
        for alert in pnl_alerts:
            level_icon = "🔴" if alert["level"] == "critical" else "🟡"
            typer.echo(f"{level_icon} [{alert['type']}] {alert['message']}")
    else:
        typer.echo("No active PnL alerts.")
    if snapshot.strategy_pnl:
        typer.echo("Top Strategy Attribution:")
        for strategy_tag, pnl in sorted(snapshot.strategy_pnl.items(), key=lambda item: item[1], reverse=True)[:5]:
            typer.echo(f"  {strategy_tag}: {pnl:+.2f}")

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


def _robinhood_boundary(settings: Settings) -> "RobinhoodBrokerBoundary":
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


@app.command(name="v3-debug")
def v3_debug(
    ctx: typer.Context,
    symbols: str = typer.Option(
        "",
        "--symbols",
        help="Comma-separated symbols to debug the V3 strategy selector against.",
    ),
) -> None:
    """Debug why the V3 strategy selector approves or rejects each symbol.

    Shows regime detection, setup search results, confluence scores,
    confidence thresholds, and the final decision for each symbol.
    """
    from trading_bot.config.settings import Settings
    from trading_bot.data import market_data
    from trading_bot.runtime.orchestrator import _drop_trailing_zero_volume_bars
    from trading_bot.strategy.strategy_selector import StrategySelector
    from trading_bot.strategy.market_regime import detect_market_regime, should_trade_regime, get_recommended_strategy
    from trading_bot.strategy.setup_rules import identify_intraday_setup, is_valid_mean_reversion_setup
    from trading_bot.strategy.mean_reversion import identify_mean_reversion_setup
    from trading_bot.data.indicators import add_ema, add_sma, add_atr, add_rsi, add_bollinger_bands, add_vwap

    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        typer.echo("Usage: v3-debug --symbols AAPL,MSFT,EBS")
        raise typer.Exit(code=1)

    settings = ctx.obj
    for sym in symbol_list:
        print(f"\n{'='*60}")
        print(f"  {sym}")
        print(f"{'='*60}")

        daily, daily_ok = market_data.fetch_and_validate_bars(sym, settings.market_data.daily_period, "1d", settings.market_data)
        if not daily_ok.valid:
            print(f"  DAILY FAIL: {daily_ok.reason}")
            continue

        intraday, intra_ok = market_data.fetch_and_validate_bars(sym, settings.market_data.intraday_period, settings.market_data.intraday_interval, settings.market_data)
        if not intra_ok.valid:
            print(f"  INTRADAY FAIL: {intra_ok.reason}")
            continue

        daily = daily.copy()
        daily = add_ema(daily, period=20, column_name="ema_20")
        daily = add_sma(daily, period=50, column_name="sma_50")
        daily = add_atr(daily, period=settings.risk.atr_period)
        daily = add_bollinger_bands(daily, period=20)

        regime, metrics = detect_market_regime(daily)
        rec_strat = get_recommended_strategy(regime)
        can_trade = should_trade_regime(regime, settings.strategy.risk_tolerance)
        print(f"  Regime: {regime.value} (ADX={metrics.adx:.1f}, vol_pct={metrics.volatility_percentile:.0%})")
        print(f"  Recommended: {rec_strat} | Trade OK: {can_trade}")

        intraday = _drop_trailing_zero_volume_bars(intraday).copy()
        intraday["volume_avg_5"] = intraday["volume"].rolling(5).mean()
        intraday = add_atr(intraday, period=settings.risk.atr_period)
        intraday = add_rsi(intraday, period=14)
        intraday = add_bollinger_bands(intraday, period=20)
        intraday = add_vwap(intraday)

        last = intraday.iloc[-1]
        close = float(last.get("close", 0))
        rsi = float(last.get("rsi_14", 0))
        bb_l = float(last.get("bb_lower", 0))
        bb_u = float(last.get("bb_upper", 0))
        vol = float(last.get("volume", 0))
        avg_vol = float(last.get("volume_avg_5", 0))
        bb_pct = (close - bb_l) / (bb_u - bb_l) * 100 if bb_u > bb_l else 0
        print(f"  Last bar: close={close:.2f} RSI={rsi:.1f} %B={bb_pct:.1f} vol={vol:.0f}/{avg_vol:.0f}")

        trend_setup = identify_intraday_setup(intraday)
        mr_setup = identify_mean_reversion_setup(intraday)
        mr_frame = is_valid_mean_reversion_setup(intraday)
        print(f"  Trend setup: {trend_setup or 'NONE'}")
        print(f"  MR setup:   {mr_setup or 'NONE'}")
        print(f"  MR frame:   {mr_frame}")

        if can_trade:
            selector = StrategySelector(risk_tolerance=settings.strategy.risk_tolerance)
            selector.min_confidence = settings.strategy.min_confidence
            selector.atr_stop_multiplier = settings.risk.atr_stop_multiplier
            selector.min_stop_distance_pct = settings.risk.min_stop_distance_pct
            selection = selector.select_strategy(sym, daily, intraday)

            print(f"  Decision: should_trade={selection.should_trade}")
            print(f"  Strategy: {selection.strategy_type} | Setup: {selection.setup_name}")
            if selection.signal_score:
                s = selection.signal_score
                print(f"  Score: {s.total_score:.1f}/12 conf={s.confidence} (need {settings.strategy.min_confidence})")
                print(f"    technical={s.technical_score} volume={s.volume_score} trend={s.trend_score}")
                print(f"    momentum={s.momentum_score} regime_align={s.regime_alignment} factors={s.factor_score}")
            print(f"  Reason: {selection.reason}")
            if selection.should_trade:
                print(f"  Entry={selection.entry_price} stop={selection.stop_loss} target={selection.profit_target}")
        else:
            print(f"  BLOCKED: regime not tradeable at tolerance={settings.strategy.risk_tolerance}")


@app.command(name="discover")
def discover_symbols(
    ctx: typer.Context,
    mode: str = typer.Option("breakout", "--mode", help="Discovery mode: breakout, mean-reversion, gap-up"),
    max_symbols: int = typer.Option(20, "--max", help="Maximum symbols to return"),
    export: bool = typer.Option(False, "--export", help="Export to configured universe file"),
) -> None:
    """Dynamically discover trading candidates based on market conditions.

    --mode dispatches to a dedicated screener:
      - breakout:        screen_for_breakout_setups (near 20-day high)
      - mean-reversion:  screen_for_mean_reversion (oversold setups)
      - gap-up:          find_gap_up_symbols via quick_update_gappers

    The default fallback for unknown modes is the generic DynamicWatchlist
    update path. The burner runs --mode breakout so this command line
    semantics are observable.
    """
    from datetime import datetime, timezone
    from trading_bot.strategy.dynamic_watchlist import (
        DynamicWatchlist,
        WatchlistEntry,
    )
    from trading_bot.data.market_data import fetch_bars

    valid_modes = {"breakout", "mean-reversion", "gap-up"}
    if mode not in valid_modes:
        typer.echo(
            f"Unknown --mode {mode!r}; expected one of {sorted(valid_modes)}. "
            f"Falling back to DynamicWatchlist.update()."
        )

    typer.echo(f"Discovering symbols (mode: {mode})...")
    typer.echo("=" * 50)

    watchlist = DynamicWatchlist(max_symbols=max_symbols, scout_settings=ctx.obj.scout)

    # Build symbols_data by reading the configured universe candidates.
    universe_path = Path(ctx.obj.app.universe_path)
    candidate_symbols: list[str] = []
    if universe_path.exists():
        candidate_symbols = [
            line.strip() for line in universe_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    def data_provider(symbol: str) -> pd.DataFrame | None:
        try:
            return fetch_bars(symbol, interval="1d", period="1mo", settings=ctx.obj.market_data)
        except Exception:
            return None

    symbols_data: dict[str, pd.DataFrame] = {}
    for sym in candidate_symbols:
        frame = data_provider(sym)
        if frame is not None and not frame.empty:
            symbols_data[sym] = frame

    added_entries: list[WatchlistEntry] = []
    if mode == "breakout" and symbols_data:
        from trading_bot.strategy.market_screener import screen_for_breakout_setups

        typer.echo(f"Screening {len(symbols_data)} symbols for breakout setups...")
        results = screen_for_breakout_setups(symbols_data)
        for r in results[:max_symbols]:
            entry = WatchlistEntry(
                symbol=r.symbol,
                added_at=datetime.now(timezone.utc),
                reason="; ".join(r.reasons) if r.reasons else "breakout",
                score=float(r.score),
            )
            if not watchlist._entries or entry.symbol not in {e.symbol for e in watchlist._entries}:
                watchlist._entries.append(entry)
                added_entries.append(entry)
    elif mode == "mean-reversion" and symbols_data:
        from trading_bot.strategy.market_screener import screen_for_mean_reversion

        typer.echo(f"Screening {len(symbols_data)} symbols for mean-reversion setups...")
        results = screen_for_mean_reversion(symbols_data)
        for r in results[:max_symbols]:
            entry = WatchlistEntry(
                symbol=r.symbol,
                added_at=datetime.now(timezone.utc),
                reason="; ".join(r.reasons) if r.reasons else "mean-reversion",
                score=float(r.score),
            )
            if not watchlist._entries or entry.symbol not in {e.symbol for e in watchlist._entries}:
                watchlist._entries.append(entry)
                added_entries.append(entry)
    elif mode == "gap-up" and symbols_data:
        typer.echo(f"Screening {len(symbols_data)} symbols for pre-market gap-up setups...")
        added_entries = watchlist.quick_update_gappers(symbols_data)
    else:
        # Fallback: generic DynamicWatchlist update.
        typer.echo("Scanning universe for setups (fallback path)...")
        update = watchlist.update(data_provider)
        if update.sectors_favored:
            typer.echo(f"Favored sectors: {', '.join(update.sectors_favored)}")
        typer.echo(f"Added: {len(update.added)} | Removed: {len(update.removed)} | Total: {len(update.current)}")
        if export:
            export_path = watchlist.export_for_burn_in(ctx.obj.app.universe_path)
            overridden = _apply_advisory_symbol_overrides(
                _read_universe_symbols(Path(export_path)),
                ctx.obj,
                limit=max_symbols,
            )
            Path(export_path).write_text("\n".join(overridden), encoding="utf-8")
            typer.echo(f"\nExported {len(overridden)} symbols to {export_path}")
        return

    typer.echo(f"Added: {len(added_entries)} | Total: {len(watchlist._entries)}")
    typer.echo("")
    if watchlist._entries:
        for entry in watchlist._entries[:max_symbols]:
            typer.echo(f"  {entry.symbol}: {entry.reason} (score: {entry.score:.1f})")
    else:
        typer.echo("No symbols passed screening criteria.")
        typer.echo(
            "⚠️  0 candidates passed discovery — preserving existing universe. "
            "Treat discovery as failed."
        )

    if export:
        pre_export_count = len(watchlist._entries)
        export_path = watchlist.export_for_burn_in(ctx.obj.app.universe_path)
        overridden = _apply_advisory_symbol_overrides(
            _read_universe_symbols(Path(export_path)),
            ctx.obj,
            limit=max_symbols,
        )
        Path(export_path).write_text("\n".join(overridden), encoding="utf-8")
        if pre_export_count == 0 and overridden:
            typer.echo(
                f"\nExported {len(overridden)} symbols (preserved from prior universe) "
                f"to {export_path}"
            )
        else:
            typer.echo(f"\nExported {len(overridden)} symbols to {export_path}")
        # When --export is set and discovery produced 0 candidates, the
        # burner shell relies on the exit code to detect failure
        # (``if echo $output | grep -q "Exported"; then ...``). Without
        # a non-zero exit the shell silently treats the run as success.
        if pre_export_count == 0:
            raise typer.Exit(code=2)


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
            frame = fetch_bars(symbol, interval="1d", period="3mo", settings=ctx.obj.market_data)
            if frame is not None:
                sector_data[symbol] = frame
                typer.echo(f"  ✓ {symbol}")
        except Exception as e:
            typer.echo(f"  ✗ {symbol}: {e}")

    # Fetch SPY for relative strength
    try:
        spy_data = fetch_bars("SPY", interval="1d", period="3mo", settings=ctx.obj.market_data)
        typer.echo("  ✓ SPY (benchmark)")
    except Exception as e:
        logger.debug("Error in CLI: %s", e)

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
            frame = fetch_bars(symbol, interval="1d", period="1mo", settings=ctx.obj.market_data)
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


def db_history(
    ctx: typer.Context,
    ticker: str | None = typer.Option(None, "--ticker", help="Filter by ticker"),
    since: str | None = typer.Option(None, "--since", help="Start date (YYYY-MM-DD)"),
    limit: int = typer.Option(50, "--limit", help="Max rows to return"),
) -> None:
    """Query scan results and trades from the database."""
    from datetime import datetime

    from trading_bot.db.session import get_session, make_session_factory
    from trading_bot.db.repositories import get_scan_results, get_trades

    engine = None
    try:
        from trading_bot.db.session import init_db
        engine = init_db(ctx.obj)
        session_factory = make_session_factory(engine)
        session = get_session(session_factory)

        try:
            if ticker:
                typer.echo(f"SCAN RESULTS for {ticker.upper()}")
                typer.echo("=" * 60)
                results = get_scan_results(session, ticker=ticker.upper(), since=datetime.fromisoformat(since) if since else None, limit=limit)
                for r in results:
                    typer.echo(f"  {r.timestamp} {r.ticker} {r.action} conf={r.confidence:.3f} strategy={r.strategy_tag}")

                typer.echo(f"\nTRADES for {ticker.upper()}")
                typer.echo("=" * 60)
                trades = get_trades(
                    session,
                    ticker=ticker.upper(),
                    since=datetime.fromisoformat(since) if since else None,
                    limit=limit,
                )
                for t in trades:
                    pnl_str = f" pnl={t.pnl:.2f}" if t.pnl is not None else ""
                    typer.echo(f"  {t.filled_at} {t.ticker} {t.side} qty={t.quantity} @${t.entry_price:.2f}{pnl_str}")
            else:
                typer.echo("RECENT SCAN RESULTS")
                typer.echo("=" * 60)
                results = get_scan_results(session, since=datetime.fromisoformat(since) if since else None, limit=limit)
                for r in results:
                    typer.echo(f"  {r.timestamp} {r.ticker} {r.action} conf={r.confidence:.3f} strategy={r.strategy_tag}")

                typer.echo(f"\nRECENT TRADES")
                typer.echo("=" * 60)
                trades = get_trades(
                    session,
                    since=datetime.fromisoformat(since) if since else None,
                    limit=limit,
                )
                for t in trades:
                    pnl_str = f" pnl={t.pnl:.2f}" if t.pnl is not None else ""
                    typer.echo(f"  {t.filled_at} {t.ticker} {t.side} qty={t.quantity} @${t.entry_price:.2f}{pnl_str}")
        finally:
            session.close()
    finally:
        if engine:
            engine.dispose()


def _scan_row_reasons(row) -> list[str]:
    if not row.reasons:
        return []
    try:
        reasons = json.loads(row.reasons)
    except (TypeError, ValueError):
        return []
    if isinstance(reasons, list):
        return [str(reason) for reason in reasons if str(reason)]
    return []


def _scan_row_details(row) -> dict[str, Any] | None:
    if not row.details:
        return None
    try:
        details = json.loads(row.details)
    except (TypeError, ValueError):
        return None
    return details if isinstance(details, dict) else None


def _trade_stack_decision(trade) -> str | None:
    tag = getattr(trade, "strategy_tag", None)
    if not tag:
        return None
    for part in str(tag).split("|"):
        if part.startswith("stack:"):
            return part.split(":", 1)[1] or None
    return None


def _trade_consensus(trade) -> str | None:
    tag = getattr(trade, "strategy_tag", None)
    if not tag:
        return None
    for part in str(tag).split("|"):
        if part.startswith("consensus:"):
            return part.split(":", 1)[1] or None
    return None


@app.command(name="db-features")
def db_features(
    ctx: typer.Context,
    ticker: str | None = typer.Option(None, "--ticker", help="Filter by ticker"),
    since: str | None = typer.Option(None, "--since", help="Start date (YYYY-MM-DD)"),
    status: str | None = typer.Option(None, "--status", help="Filter by status (APPROVED, REJECTED, etc.)"),
    action: str | None = typer.Option(None, "--action", help="Filter by action (BUY, HOLD, SELL)"),
    regime: str | None = typer.Option(None, "--regime", help="Filter by market regime (trending_up, trending_down, mean_reversion, high_volatility, sideways)"),
    quality: str | None = typer.Option(None, "--quality", help="Filter by signal quality (GREEN, YELLOW, RED)"),
    strategy: str | None = typer.Option(None, "--strategy", help="Filter by strategy tag"),
    limit: int = typer.Option(100, "--limit", help="Max rows to return"),
    summary: bool = typer.Option(False, "--summary", help="Show aggregate summary statistics"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Query scan_features table from the database."""
    from datetime import datetime
    from collections import Counter

    from trading_bot.db.session import get_session, init_db, make_session_factory
    from trading_bot.db.repositories import get_scan_features

    engine = None
    try:
        from trading_bot.db.session import init_db
        engine = init_db(ctx.obj)
        session_factory = make_session_factory(engine)
        session = get_session(session_factory)

        try:
            features = get_scan_features(
                session,
                ticker=ticker.upper() if ticker else None,
                since=datetime.fromisoformat(since) if since else None,
                limit=limit,
                status=status,
                action=action,
                market_regime=regime,
                quality=quality,
                strategy_tag=strategy,
            )

            if json_output:
                import json as json_module
                rows = []
                for f in features:
                    rows.append({
                        "id": f.id,
                        "timestamp": f.timestamp.isoformat() if f.timestamp else None,
                        "ticker": f.ticker,
                        "status": f.status,
                        "action": f.action,
                        "confidence": f.confidence,
                        "quality": f.quality,
                        "freshness": f.freshness,
                        "market_age_minutes": f.market_age_minutes,
                        "market_regime": f.market_regime,
                        "strategy_tag": f.strategy_tag,
                        "consensus": f.consensus,
                        "v3_total_score": f.v3_total_score,
                        "supermodel_score": f.supermodel_score,
                        "mtf_aligned": f.mtf_aligned,
                        "entry_volume_ratio": f.entry_volume_ratio,
                        "entry_range_ratio": f.entry_range_ratio,
                        "adaptive_rr": f.adaptive_rr,
                    })
                typer.echo(json_module.dumps(rows, default=str, indent=2))
                return

            typer.echo(f"SCAN FEATURES ({len(features)} rows)")
            typer.echo("=" * 120)
            typer.echo(
                f"{'Timestamp':<22} {'Ticker':>6} {'Status':>10} {'Action':>6} "
                f"{'Conf':>6} {'Quality':>8} {'Regime':<20} {'Strategy':<20}"
            )
            typer.echo("-" * 120)
            for f in features:
                ts = f.timestamp.strftime("%Y-%m-%d %H:%M:%S") if f.timestamp else "N/A"
                conf_str = f"{f.confidence:.3f}" if f.confidence is not None else "N/A"
                typer.echo(
                    f"  {ts:<20} {f.ticker:>6} {f.status:>10} {f.action:>6} "
                    f"{conf_str:>6} {f.quality or '':>8} {f.market_regime or '':<20} {f.strategy_tag or '':<20}"
                )

            if summary and features:
                typer.echo(f"\nSUMMARY")
                typer.echo("=" * 120)

                status_counts = Counter(f.status for f in features)
                typer.echo(f"\nStatus distribution:")
                for s, c in sorted(status_counts.items()):
                    typer.echo(f"  {s}: {c}")

                action_counts = Counter(f.action for f in features)
                typer.echo(f"\nAction distribution:")
                for a, c in sorted(action_counts.items()):
                    typer.echo(f"  {a}: {c}")

                regime_counts = Counter(f.market_regime for f in features if f.market_regime)
                typer.echo(f"\nRegime distribution:")
                for r, c in sorted(regime_counts.items()):
                    typer.echo(f"  {r}: {c}")

                quality_counts = Counter(f.quality for f in features if f.quality)
                typer.echo(f"\nQuality distribution:")
                for q, c in sorted(quality_counts.items()):
                    typer.echo(f"  {q}: {c}")

                v3_scores = [f.v3_total_score for f in features if f.v3_total_score is not None]
                if v3_scores:
                    avg_v3 = sum(v3_scores) / len(v3_scores)
                    typer.echo(f"\nV3 Total Score: avg={avg_v3:.3f} min={min(v3_scores):.3f} max={max(v3_scores):.3f} n={len(v3_scores)}")

                supermodel_scores = [f.supermodel_score for f in features if f.supermodel_score is not None]
                if supermodel_scores:
                    avg_sm = sum(supermodel_scores) / len(supermodel_scores)
                    typer.echo(f"\nSupermodel Score: avg={avg_sm:.3f} min={min(supermodel_scores):.3f} max={max(supermodel_scores):.3f} n={len(supermodel_scores)}")

                strategy_counts = Counter(f.strategy_tag for f in features if f.strategy_tag)
                typer.echo(f"\nStrategy distribution:")
                for st, c in sorted(strategy_counts.items(), key=lambda x: -x[1])[:10]:
                    typer.echo(f"  {st}: {c}")

                typer.echo(f"\nTotal: {len(features)} scan features")
        finally:
            session.close()
    finally:
        if engine:
            engine.dispose()


@app.command()
def db_portfolio(
    ctx: typer.Context,
    since: str | None = typer.Option(None, "--since", help="Start date (YYYY-MM-DD)"),
    limit: int = typer.Option(20, "--limit", help="Max snapshots to return"),
) -> None:
    """Show portfolio snapshots from the database."""
    from datetime import datetime

    from trading_bot.db.session import get_session, make_session_factory
    from trading_bot.db.repositories import get_open_positions
    from trading_bot.db.repositories.portfolio_snapshots import get_snapshots

    engine = None
    try:
        from trading_bot.db.session import init_db
        engine = init_db(ctx.obj)
        session_factory = make_session_factory(engine)
        session = get_session(session_factory)

        try:
            typer.echo("PORTFOLIO SNAPSHOTS")
            typer.echo("=" * 80)
            typer.echo(f"{'Timestamp':<22} {'Equity':>12} {'Cash':>12} {'Unrealized':>12} {'Positions':>10}")
            typer.echo("-" * 80)

            snapshots = get_snapshots(session, since=datetime.fromisoformat(since) if since else None, limit=limit)
            for s in snapshots:
                typer.echo(f"  {s.timestamp:<20} ${s.equity:>11,.2f} ${s.cash:>11,.2f} ${s.unrealized_pnl:>11,.2f} {s.num_positions:>8}")

            typer.echo("\nOPEN POSITIONS")
            typer.echo("=" * 60)
            positions = get_open_positions(session)
            if positions:
                for p in positions:
                    typer.echo(f"  {p.ticker} qty={p.quantity} avg_cost=${p.average_cost:.2f}")
            else:
                typer.echo("  (no open positions)")
        finally:
            session.close()
    finally:
        if engine:
            engine.dispose()


@app.command()
def db_trades(
    ctx: typer.Context,
    ticker: str | None = typer.Option(None, "--ticker", help="Filter by ticker"),
    since: str | None = typer.Option(None, "--since", help="Start date (YYYY-MM-DD)"),
    limit: int = typer.Option(50, "--limit", help="Max trades to return"),
) -> None:
    """List trades from the database."""
    from datetime import datetime

    from trading_bot.db.session import get_session, make_session_factory
    from trading_bot.db.repositories import get_open_trades, get_trades

    engine = None
    try:
        from trading_bot.db.session import init_db
        engine = init_db(ctx.obj)
        session_factory = make_session_factory(engine)
        session = get_session(session_factory)

        try:
            if ticker:
                typer.echo(f"TRADES for {ticker.upper()}")
                typer.echo("=" * 80)
                typer.echo(f"{'Timestamp':<22} {'Side':>6} {'Qty':>5} {'Entry':>10} {'Exit':>10} {'P&L':>10} {'Status':>10}")
                typer.echo("-" * 80)
                trades = get_trades(session, ticker=ticker.upper(), since=datetime.fromisoformat(since) if since else None, limit=limit)
            else:
                typer.echo("ALL TRADES")
                typer.echo("=" * 80)
                typer.echo(f"{'Timestamp':<22} {'Ticker':>6} {'Side':>6} {'Qty':>5} {'Entry':>10} {'Exit':>10} {'P&L':>10} {'Status':>10}")
                typer.echo("-" * 80)
                trades = get_trades(session, since=datetime.fromisoformat(since) if since else None, limit=limit)

            for t in trades:
                exit_str = f"${t.exit_price:.2f}" if t.exit_price else "-"
                pnl_str = f"${t.pnl:.2f}" if t.pnl is not None else "-"
                ticker_str = t.ticker
                typer.echo(f"  {t.filled_at:<20} {ticker_str:>6} {t.side:>6} {t.quantity:>5} ${t.entry_price:>9,.2f} {exit_str:>10} {pnl_str:>10} {t.status:>10}")

            typer.echo(f"\nTotal: {len(trades)} trades")
        finally:
            session.close()
    finally:
        if engine:
            engine.dispose()


@app.command()
def swarm(
    ctx: typer.Context,
    preset: str = typer.Option(
        "investment_committee",
        "--preset",
        help="Swarm preset to use (investment_committee, quant_desk, risk_committee, etc.).",
    ),
    symbols: list[str] = typer.Option(
        ...,
        "--symbols",
        help="Symbols to analyze.",
    ),
    max_workers: int = typer.Option(
        3,
        "--max-workers",
        help="Maximum concurrent workers.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output results as JSON.",
    ),
) -> None:
    """Run a multi-agent swarm analysis."""
    import json as json_module

    from trading_bot.swarm.engine import SwarmEngine
    from trading_bot.swarm.workers import WORKER_CLASSES
    from trading_bot.data.market_data import fetch_and_validate_bars
    from trading_bot.portfolio.ledger import PortfolioLedger

    parsed_symbols = _parse_symbols(symbols)
    settings = ctx.obj

    typer.echo(f"Swarm Analysis: preset='{preset}', symbols={parsed_symbols}")
    typer.echo("=" * 80)

    engine = SwarmEngine(preset_name=preset, max_concurrent=max_workers)
    engine.setup_workers(WORKER_CLASSES)

    # Fetch market data for all symbols
    market_data = {}
    for ticker in parsed_symbols:
        daily_frame, valid = fetch_and_validate_bars(
            ticker,
            settings.market_data.daily_period,
            "1d",
            settings.market_data,
        )
        if valid.valid and daily_frame is not None and not daily_frame.empty:
            market_data[ticker] = daily_frame
        else:
            typer.echo(f"  ⚠ {ticker}: data validation failed - {valid.reason}")

    if not market_data:
        typer.echo("No valid market data fetched. Aborting.")
        return

    typer.echo(f"  ✓ Fetched data for {len(market_data)}/{len(parsed_symbols)} symbols")
    typer.echo()

    # Run swarm
    state = PortfolioLedger(Path(settings.app.state_db_path)).ensure_portfolio_state()
    summary = engine.run(
        symbols=parsed_symbols,
        market_data=market_data,
        portfolio_state=state.model_dump(),
        vote_log_path=Path(settings.app.log_dir) / "worker_votes.jsonl",
    )

    if json_output:
        typer.echo(json_module.dumps(summary, default=str, indent=2))
    else:
        # Print worker statuses
        typer.echo("Worker Results:")
        typer.echo("-" * 80)
        decisions = summary.get("decisions", {}) if isinstance(summary, dict) else summary.decisions
        for ticker, decision in decisions.items():
            if isinstance(decision, dict):
                typer.echo(f"\n{ticker}:")
                typer.echo(f"  Decision: {decision.get('action', 'N/A')} (confidence: {decision.get('confidence', 0):.2f})")
                typer.echo(f"  Votes: {decision.get('votes_for', 0)} for, {decision.get('votes_against', 0)} against, {decision.get('votes_abstain', 0)} abstain")
                typer.echo(f"  Rationale: {decision.get('key_rationale', 'N/A')}")
                risk_factors = decision.get('risk_factors', [])
                if risk_factors:
                    typer.echo(f"  Risks:")
                    for risk in risk_factors[:5]:
                        typer.echo(f"    - {risk}")
            else:
                typer.echo(f"\n{ticker}:")
                typer.echo(f"  Decision: {decision.action} (confidence: {decision.confidence:.2f})")
                typer.echo(f"  Votes: {decision.votes_for} for, {decision.votes_against} against, {decision.votes_abstain} abstain")
                typer.echo(f"  Rationale: {decision.key_rationale}")
                if decision.risk_factors:
                    typer.echo(f"  Risks:")
                    for risk in decision.risk_factors[:5]:
                        typer.echo(f"    - {risk}")
            typer.echo()

        exec_time = summary.get("execution_time_seconds", 0) if isinstance(summary, dict) else summary.execution_time_seconds
        completed = summary.get("completed_workers", 0) if isinstance(summary, dict) else summary.completed_workers
        total = summary.get("total_workers", 0) if isinstance(summary, dict) else summary.total_workers
        typer.echo(f"Execution time: {exec_time:.1f}s")
        typer.echo(f"Workers: {completed}/{total} completed")


@app.command()
def attribution(
    ctx: typer.Context,
    symbols: list[str] = typer.Option(
        ...,
        "--symbols",
        help="Symbols to run attribution on.",
    ),
    start: str = typer.Option(None, "--start", help="Start date (YYYY-MM-DD)."),
    end: str = typer.Option(None, "--end", help="End date (YYYY-MM-DD)."),
    benchmark: str = typer.Option("SPY", "--benchmark", help="Benchmark ticker for beta calculation."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Run post-backtest attribution analysis."""
    from trading_bot.backtest.runner import run_backtest
    from trading_bot.backtest.attribution import run_attribution
    from trading_bot.data import market_data

    parsed_symbols = [s.upper().strip() for s in symbols]
    settings = ctx.obj

    typer.echo(f"Backtest Attribution: symbols={parsed_symbols}")
    typer.echo("=" * 80)

    # Run backtest first
    typer.echo("\nRunning backtest...")
    result = run_backtest(parsed_symbols, settings, start=start, end=end)

    # Fetch benchmark data
    benchmark_data = None
    try:
        benchmark_data = market_data.fetch_bars(
            benchmark,
            settings.market_data.daily_period,
            "1d",
            start=start,
            end=end,
            settings=settings.market_data,
        )
    except Exception as e:
        typer.echo(f"  ⚠ Could not fetch benchmark data: {e}")

    # Run attribution
    typer.echo("\nRunning attribution analysis...")
    attribution = run_attribution(result, benchmark_data=benchmark_data)

    if json_output:
        import json as json_module
        typer.echo(json_module.dumps(attribution, default=str, indent=2))
    else:
        # Print trade-level attribution
        trade_attr = attribution.get("trade_level_attribution", {})
        typer.echo("\nTrade-Level Attribution:")
        typer.echo("-" * 80)
        typer.echo(f"Total Trades: {trade_attr.get('total_trades', 0)}")
        typer.echo(f"Total P&L: ${trade_attr.get('total_pnl', 0):,.2f}")
        typer.echo(f"Win Rate: {trade_attr.get('win_rate', 0):.1f}%")
        typer.echo(f"Top Contributor: {trade_attr.get('top_contributor', 'N/A')}")
        typer.echo(f"Worst Contributor: {trade_attr.get('worst_contributor', 'N/A')}")

        # Ticker contributions
        typer.echo("\nTicker Contributions:")
        typer.echo(f"{'Ticker':<10} {'Trades':>8} {'P&L':>12} {'Contrib%':>10} {'Win%':>8}")
        typer.echo("-" * 50)
        for tc in trade_attr.get("ticker_contributions", []):
            typer.echo(
                f"{tc['ticker']:<10} {tc['trades']:>8} "
                f"${tc['net_pnl']:>10,.2f} {tc['contribution_pct']:>9.1f}% "
                f"{tc['win_rate']:>7.1f}%"
            )

        # Winner/loser analysis
        wl = attribution.get("winner_loser_analysis", {})
        typer.echo("\nWinner/Loser Analysis:")
        typer.echo("-" * 80)
        typer.echo(f"Avg Win: ${wl.get('avg_win', 0):,.2f}")
        typer.echo(f"Avg Loss: ${wl.get('avg_loss', 0):,.2f}")
        typer.echo(f"Win/Loss Ratio: {wl.get('win_loss_ratio', 0):.2f}")
        typer.echo(f"Profit Factor: {wl.get('profit_factor', 0):.2f}")
        typer.echo(f"Expectancy: ${wl.get('expectancy', 0):,.2f}")

        # Beta regression
        beta = attribution.get("beta_regression", {})
        if beta and "beta" in beta:
            typer.echo("\nBeta Regression:")
            typer.echo("-" * 80)
            typer.echo(f"Beta: {beta.get('beta', 0):.3f}")
            typer.echo(f"Alpha: {beta.get('alpha', 0):.4f}")
            typer.echo(f"Sharpe Ratio: {beta.get('sharpe_ratio', 0):.2f}")
            typer.echo(f"Interpretation: {beta.get('interpretation', '')}")

        # Monte Carlo
        mc = attribution.get("monte_carlo", {})
        if mc and "probability_of_profit" in mc:
            typer.echo("\nMonte Carlo Simulation:")
            typer.echo("-" * 80)
            typer.echo(f"Simulations: {mc.get('num_simulations', 0):,}")
            typer.echo(f"Probability of Profit: {mc.get('probability_of_profit', 0):.1%}")
            typer.echo(f"Mean P&L: ${mc.get('simulated_mean_pnl', 0):,.2f}")
            typer.echo(f"Max Drawdown: ${mc.get('max_drawdown_simulation', 0):,.2f}")
            ci = mc.get("confidence_intervals", {})
            for key, val in ci.items():
                typer.echo(f"{key}: ${val.get('lower', 0):,.2f} to ${val.get('upper', 0):,.2f}")
            typer.echo(f"Interpretation: {mc.get('interpretation', '')}")


@app.command()
def alpha_bench(
    ctx: typer.Context,
    zoo: str = typer.Option(
        "all",
        "--zoo",
        help="Factor zoo to bench (qlib, kakushadze, gtja, academic, all).",
    ),
    symbols: list[str] = typer.Option(
        None,
        "--symbols",
        help="Symbols to fetch data for benching.",
    ),
    lookback: int = typer.Option(
        60,
        "--lookback",
        help="Lookback period for IC calculation (days).",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Run strict benching with OOS split and random control.",
    ),
    compare: list[str] = typer.Option(
        None,
        "--compare",
        help="Compare specific factors by name.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output results as JSON.",
    ),
) -> None:
    """Benchmark alpha factors from the factor zoo."""
    import json as json_module

    from trading_bot.factors import AlphaFactorRegistry, AlphaZoo
    from trading_bot.factors.bench import bench_alpha, bench_zoo, compare_alphas, bench_strict
    from trading_bot.data.market_data import fetch_and_validate_bars

    settings = ctx.obj
    parsed_symbols = _parse_symbols(symbols) if symbols else ["SPY"]
    parsed_compare = _parse_symbols(compare) if compare else None

    typer.echo(f"Alpha Factor Benching")
    typer.echo("=" * 80)
    typer.echo(f"Symbols: {parsed_symbols}")
    typer.echo(f"Lookback: {lookback} days")
    typer.echo()

    # Fetch market data
    all_frames: dict[str, pd.DataFrame] = {}
    for ticker in parsed_symbols:
        daily_frame, valid = fetch_and_validate_bars(
            ticker,
            settings.market_data.daily_period,
            "1d",
            settings.market_data,
        )
        if valid.valid and daily_frame is not None and not daily_frame.empty:
            all_frames[ticker] = daily_frame
        else:
            typer.echo(f"  ⚠ {ticker}: data validation failed - {valid.reason}")

    if not all_frames:
        typer.echo("No valid market data fetched. Aborting.")
        return

    # Use first symbol as primary
    primary_ticker = list(all_frames.keys())[0]
    frame = all_frames[primary_ticker]

    typer.echo(f"Using data for: {primary_ticker} ({len(frame)} bars)")
    typer.echo()

    results: dict[str, Any] = {}

    if parsed_compare:
        # Compare specific factors
        typer.echo("Comparing factors:")
        typer.echo("-" * 80)
        comparison = compare_alphas(
            parsed_compare,
            frame,
            lookback=lookback,
            sort_by="ic_ir",
        )
        results["comparison"] = comparison

        if not json_output:
            typer.echo(f"\nFactors compared: {comparison['factors_compared']}")
            typer.echo(f"Sort by: {comparison['sort_by']}")
            typer.echo(f"\n{'Factor':<40} {'IC IR':>8} {'IC Mean':>10} {'IC+ %':>8} {'Category':<20}")
            typer.echo("-" * 90)
            for r in comparison.get("results", []):
                typer.echo(
                    f"{r['factor_name']:<40} {r['ic_ir']:>8.4f} "
                    f"{r['ic_mean']:>10.4f} {r['ic_positive_ratio']:>7.1%} "
                    f"{r['category']:<20}"
                )

    elif zoo != "all":
        # Bench specific zoo
        try:
            zoo_enum = AlphaZoo(zoo)
        except ValueError:
            typer.echo(f"Invalid zoo: '{zoo}'. Use: qlib, kakushadze, gtja, academic")
            return

        typer.echo(f"Benching zoo: {zoo}")
        typer.echo("-" * 80)
        zoo_results = bench_zoo(zoo_enum, frame, lookback=lookback)
        results["zoo"] = zoo_results

        if not json_output and "aggregate" in zoo_results:
            agg = zoo_results["aggregate"]
            typer.echo(f"\nAggregate Statistics:")
            typer.echo(f"  Factors: {agg['n_factors']}")
            typer.echo(f"  Avg IC Mean: {agg['avg_ic_mean']:.4f}")
            typer.echo(f"  Avg IC IR: {agg['avg_ic_ir']:.4f}")
            typer.echo(f"  Avg IC+ Ratio: {agg['avg_ic_positive_ratio']:.1%}")
            typer.echo(f"  Best IC IR: {agg['best_ic_ir']:.4f}")
            typer.echo(f"  Worst IC IR: {agg['worst_ic_ir']:.4f}")

            if zoo_results.get("factors"):
                typer.echo(f"\n{'Factor':<40} {'IC IR':>8} {'IC Mean':>10} {'IC+ %':>8} {'Category':<20} {'Status':<10}")
                typer.echo("-" * 100)
                for f in zoo_results["factors"]:
                    typer.echo(
                        f"{f['factor_name']:<40} {f['ic_ir']:>8.4f} "
                        f"{f['ic_mean']:>10.4f} {f['ic_positive_ratio']:>7.1%} "
                        f"{f['category']:<20} {f['categorization']:<10}"
                    )

    else:
        # Bench all zoos
        typer.echo("Benching all factor zoos:")
        typer.echo("-" * 80)
        for zoo_name in AlphaZoo:
            typer.echo(f"\n--- {zoo_name.value.upper()} ---")
            zoo_results = bench_zoo(zoo_name, frame, lookback=lookback)
            results[f"zoo_{zoo_name.value}"] = zoo_results

            if not json_output and "aggregate" in zoo_results:
                agg = zoo_results["aggregate"]
                typer.echo(f"  Factors: {agg['n_factors']}, Avg IC IR: {agg['avg_ic_ir']:.4f}")
                for f in zoo_results.get("factors", [])[:5]:
                    typer.echo(
                        f"    {f['factor_name']:<35} IC IR: {f['ic_ir']:>7.4f}  ({f['categorization']})"
                    )

    if strict:
        # Run strict benching on top factors
        typer.echo("\n\nStrict Benching (OOS + Random Control):")
        typer.echo("=" * 80)
        if parsed_compare:
            for name in parsed_compare[:5]:
                factor = AlphaFactorRegistry.get(name)
                if factor:
                    strict_result = bench_strict(factor, frame, lookback=lookback)
                    results[f"strict_{name}"] = strict_result

                    if not json_output:
                        typer.echo(f"\n{factor}:")
                        is_result = strict_result.get("in_sample", {})
                        oos_result = strict_result.get("out_of_sample", {})
                        overfit = strict_result.get("overfitting_check", {})
                        typer.echo(f"  In-Sample IC IR: {is_result.get('ic_ir', 0):.4f}")
                        typer.echo(f"  Out-of-Sample IC IR: {oos_result.get('ic_ir', 0):.4f}")
                        typer.echo(f"  Overfitting: {overfit.get('verdict', 'N/A')} ({overfit.get('degradation_pct', 0):.1f}% degradation)")
        else:
            # Bench top factor from each zoo
            for zoo_name in AlphaZoo:
                factors = AlphaFactorRegistry.get_by_zoo(zoo_name)
                if factors:
                    factor = factors[0]
                    strict_result = bench_strict(factor, frame, lookback=lookback)
                    results[f"strict_{zoo_name.value}"] = strict_result

                    if not json_output:
                        typer.echo(f"\n{factor.zoo.value.upper()} - {factor}:")
                        is_result = strict_result.get("in_sample", {})
                        oos_result = strict_result.get("out_of_sample", {})
                        overfit = strict_result.get("overfitting_check", {})
                        typer.echo(f"  In-Sample IC IR: {is_result.get('ic_ir', 0):.4f}")
                        typer.echo(f"  Out-of-Sample IC IR: {oos_result.get('ic_ir', 0):.4f}")
                        typer.echo(f"  Overfitting: {overfit.get('verdict', 'N/A')} ({overfit.get('degradation_pct', 0):.1f}% degradation)")

    if json_output:
        typer.echo(json_module.dumps(results, default=str, indent=2))


@app.command()
def research_autopilot(
    ctx: typer.Context,
    action: str = typer.Option(
        "run",
        "--action",
        help="Action: create, run, stats, cycles, or bench-to-hypothesis.",
    ),
    title: str = typer.Option(
        "",
        "--title",
        help="Hypothesis title (required for create).",
    ),
    description: str = typer.Option(
        "",
        "--description",
        help="Hypothesis description (required for create).",
    ),
    category: str = typer.Option(
        "custom",
        "--category",
        help="Hypothesis category: factor_tweak, parameter_optimization, regime_dependent, cross_asset, risk_management, entry_exit, position_sizing, custom.",
    ),
    symbols: list[str] = typer.Option(
        [],
        "--symbols",
        help="Symbols to test.",
    ),
    start_date: str = typer.Option(
        "2024-01-01",
        "--start",
        help="Backtest start date.",
    ),
    end_date: str = typer.Option(
        "2025-06-01",
        "--end",
        help="Backtest end date.",
    ),
    max_cycles: int = typer.Option(
        10,
        "--max-cycles",
        help="Maximum research cycles to run.",
    ),
    benching_results: str = typer.Option(
        "",
        "--benching",
        help="JSON string of benching results for auto-generating hypotheses.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output results as JSON.",
    ),
) -> None:
    """Research autopilot: hypothesis → backtest → evaluate → learn loop."""
    import json as json_module

    from trading_bot.research.engine import ResearchEngine
    from trading_bot.research.models import (
        HypothesisCategory,
        HypothesisStatus,
    )
    from trading_bot.research.store import ResearchStore

    store = ResearchStore()
    engine = ResearchEngine(store)
    results: dict[str, Any] = {}

    if action == "create":
        if not title or not description:
            typer.echo("Error: --title and --description required for create")
            return

        try:
            cat = HypothesisCategory(category)
        except ValueError:
            typer.echo(f"Invalid category: {category}")
            return

        parsed_symbols = _parse_symbols(symbols) if symbols else []
        hypothesis = engine.create_hypothesis(
            title=title,
            description=description,
            category=cat,
            parameters={
                "symbols": parsed_symbols,
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        results["hypothesis"] = {
            "id": hypothesis.id,
            "title": hypothesis.title,
            "status": hypothesis.status.value,
            "category": hypothesis.category.value,
        }
        typer.echo(f"Created hypothesis: {hypothesis.id}")
        typer.echo(f"  Title: {hypothesis.title}")
        typer.echo(f"  Description: {hypothesis.description}")
        typer.echo(f"  Category: {hypothesis.category.value}")
        typer.echo(f"  Status: {hypothesis.status.value}")

    elif action == "run":
        typer.echo(
            "research-autopilot --action run is not implemented in this build. "
            "The candidate action called run_backtest() with the wrong argument "
            "order and mapped output keys that the active backtest does not "
            "provide, producing silent zero-metric cycles. The create / stats / "
            "cycles actions remain available."
        )
        return

        def _backtest_fn(hyp: Any) -> dict[str, Any]:
            params = hyp.parameters
            syms = _parse_symbols(params.get("symbols", []))
            if not syms:
                return {
                    "total_return": 0.0,
                    "win_rate": 0.0,
                    "sharpe_ratio": 0.0,
                    "max_drawdown": 0.0,
                    "total_trades": 0,
                    "profit_factor": 0.0,
                    "avg_trade_pnl": 0.0,
                }
            from trading_bot.backtest.runner import run_backtest
            bt_result = run_backtest(syms, params.get("start_date", start_date), params.get("end_date", end_date), ctx.obj)
            return {
                "total_return": bt_result.get("total_return", 0.0),
                "win_rate": bt_result.get("win_rate", 0.0),
                "sharpe_ratio": bt_result.get("sharpe_ratio", 0.0),
                "max_drawdown": bt_result.get("max_drawdown", 0.0),
                "total_trades": bt_result.get("total_trades", 0),
                "profit_factor": bt_result.get("profit_factor", 0.0),
                "avg_trade_pnl": bt_result.get("avg_trade_pnl", 0.0),
                "metrics": bt_result,
            }

        cycles = engine.run_pending_hypotheses(_backtest_fn, max_cycles=max_cycles)
        results["cycles"] = []
        for cycle in cycles:
            cycle_data = {
                "cycle_id": cycle.cycle_id,
                "hypothesis": cycle.hypothesis.title if cycle.hypothesis else None,
                "status": cycle.hypothesis.status.value if cycle.hypothesis else None,
                "evaluation": cycle.evaluation,
            }
            if cycle.experiment_result:
                cycle_data["experiment"] = {
                    "win_rate": cycle.experiment_result.win_rate,
                    "sharpe_ratio": cycle.experiment_result.sharpe_ratio,
                    "max_drawdown": cycle.experiment_result.max_drawdown,
                    "total_trades": cycle.experiment_result.total_trades,
                }
            results["cycles"].append(cycle_data)

        typer.echo(f"\nCompleted {len(cycles)} cycle(s):")
        for cycle in cycles:
            status = cycle.hypothesis.status.value if cycle.hypothesis else "unknown"
            typer.echo(f"  {cycle.hypothesis.title if cycle.hypothesis else 'N/A'} -> {status}")
            typer.echo(f"    {cycle.evaluation}")

    elif action == "stats":
        stats = engine.get_stats()
        results["stats"] = stats
        if json_output:
            typer.echo(json_module.dumps(results, default=str, indent=2))
        else:
            typer.echo("Research Statistics:")
            typer.echo(f"  Total hypotheses: {stats.get('total_hypotheses', 0)}")
            typer.echo(f"  Pending: {stats.get('pending_count', 0)}")
            typer.echo(f"  Running: {stats.get('running_count', 0)}")
            typer.echo(f"  Passed: {stats.get('passed_count', 0)}")
            typer.echo(f"  Failed: {stats.get('failed_count', 0)}")
            typer.echo(f"  Inconclusive: {stats.get('inconclusive_count', 0)}")
            typer.echo(f"  Total experiments: {stats.get('total_experiments', 0)}")
            typer.echo(f"  Total cycles: {stats.get('total_cycles', 0)}")
            typer.echo(f"  Avg win rate: {stats.get('avg_win_rate', 0):.1%}")
            typer.echo(f"  Avg Sharpe: {stats.get('avg_sharpe_ratio', 0):.2f}")

    elif action == "cycles":
        cycles = engine.list_cycles(limit=max_cycles)
        results["cycles"] = []
        for cycle in cycles:
            cycle_data = {
                "cycle_id": cycle.cycle_id,
                "hypothesis": cycle.hypothesis.title if cycle.hypothesis else None,
                "status": cycle.hypothesis.status.value if cycle.hypothesis else None,
                "evaluation": cycle.evaluation,
                "completed_at": cycle.completed_at.isoformat(),
            }
            if cycle.experiment_result:
                cycle_data["experiment"] = {
                    "win_rate": cycle.experiment_result.win_rate,
                    "sharpe_ratio": cycle.experiment_result.sharpe_ratio,
                    "max_drawdown": cycle.experiment_result.max_drawdown,
                    "total_trades": cycle.experiment_result.total_trades,
                }
            results["cycles"].append(cycle_data)

        if json_output:
            typer.echo(json_module.dumps(results, default=str, indent=2))
        else:
            typer.echo(f"Recent research cycles ({len(cycles)}):")
            for cycle in cycles:
                status = cycle.hypothesis.status.value if cycle.hypothesis else "unknown"
                typer.echo(f"  {cycle.cycle_id}: {cycle.hypothesis.title if cycle.hypothesis else 'N/A'} -> {status}")

    elif action == "bench-to-hypothesis":
        if not benching_results:
            typer.echo("Error: --benching JSON string required")
            return

        try:
            bench_data = json_module.loads(benching_results)
        except json_module.JSONDecodeError:
            typer.echo("Error: invalid JSON for --benching")
            return

        hypotheses = engine.auto_generate_hypotheses_from_benching(bench_data)
        results["hypotheses"] = [
            {
                "id": h.id,
                "title": h.title,
                "category": h.category.value,
                "status": h.status.value,
            }
            for h in hypotheses
        ]
        typer.echo(f"Generated {len(hypotheses)} hypotheses from benching results:")
        for h in hypotheses:
            typer.echo(f"  {h.id}: {h.title} ({h.category.value})")

    else:
        typer.echo(f"Unknown action: {action}. Use: create, run, stats, cycles, bench-to-hypothesis")

    if json_output and action not in ("stats", "cycles"):
        typer.echo(json_module.dumps(results, default=str, indent=2))


@app.command()
def memory(
    ctx: typer.Context,
    action: str = typer.Option(
        "store",
        "--action",
        help="Action: store, recall, search, stats, list, or clear.",
    ),
    title: str = typer.Option(
        "",
        "--title",
        help="Memory title (required for store).",
    ),
    content: str = typer.Option(
        "",
        "--content",
        help="Memory content (required for store).",
    ),
    memory_type: str = typer.Option(
        "custom",
        "--type",
        help="Memory type: research_finding, hypothesis_result, trading_insight, pattern_recognition, parameter_tuning, risk_observation, custom.",
    ),
    search: str = typer.Option(
        "",
        "--search",
        help="Search text for recall or search actions.",
    ),
    tags: str = typer.Option(
        "",
        "--tags",
        help="Comma-separated tags for filtering.",
    ),
    symbols: str = typer.Option(
        "",
        "--symbols",
        help="Comma-separated symbols for context.",
    ),
    max_results: int = typer.Option(
        10,
        "--max-results",
        help="Maximum results to return.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output results as JSON.",
    ),
) -> None:
    """Persistent memory: store, recall, and search trading insights."""
    import json as json_module

    from trading_bot.memory.models import MemoryType
    from trading_bot.memory.retriever import MemoryRetriever
    from trading_bot.memory.store import MemoryStore

    store = MemoryStore()
    retriever = MemoryRetriever(store)
    results: dict[str, Any] = {}

    if action == "store":
        if not title or not content:
            typer.echo("Error: --title and --content required for store")
            return

        try:
            mtype = MemoryType(memory_type)
        except ValueError:
            typer.echo(f"Invalid type: {memory_type}")
            return

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []

        if mtype == MemoryType.TRADING_INSIGHT:
            memory_entry = retriever.store_trading_insight(
                title=title,
                content=content,
                symbols=symbol_list,
                tags=tag_list,
            )
        else:
            from trading_bot.memory.models import MemoryEntry
            entry = MemoryEntry(
                memory_type=mtype,
                title=title,
                content=content,
                tags=tag_list,
            )
            row_id = store.save_memory(entry)
            entry.id = row_id
            memory_entry = entry

        results["memory"] = {
            "id": memory_entry.id,
            "title": memory_entry.title,
            "type": memory_entry.memory_type.value,
            "relevance": memory_entry.relevance_score,
        }
        typer.echo(f"Stored memory: {memory_entry.id}")
        typer.echo(f"  Title: {memory_entry.title}")
        typer.echo(f"  Type: {memory_entry.memory_type.value}")
        typer.echo(f"  Relevance: {memory_entry.relevance_score:.2f}")

    elif action == "recall":
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []
        memories = retriever.recall_for_context(
            context=search,
            symbols=symbol_list if symbol_list else None,
            max_results=max_results,
        )
        results["memories"] = [
            {
                "id": m.id,
                "title": m.title,
                "type": m.memory_type.value,
                "relevance": m.relevance_score,
                "content": m.content[:200],
                "tags": m.tags,
            }
            for m in memories
        ]
        typer.echo(f"Recalled {len(memories)} memory(ies):")
        for m in memories:
            typer.echo(f"  [{m.memory_type.value}] {m.title} (relevance: {m.relevance_score:.2f})")

    elif action == "search":
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        query_type = MemoryType(memory_type) if memory_type != "custom" else None

        from trading_bot.memory.models import MemoryQuery
        query = MemoryQuery(
            search_text=search,
            memory_type=query_type,
            tags=tag_list,
            limit=max_results,
            sort_by="relevance",
        )
        memories = store.query_memories(query)
        results["memories"] = [
            {
                "id": m.id,
                "title": m.title,
                "type": m.memory_type.value,
                "relevance": m.relevance_score,
                "content": m.content[:200],
                "tags": m.tags,
            }
            for m in memories
        ]
        typer.echo(f"Found {len(memories)} memory(ies):")
        for m in memories:
            typer.echo(f"  [{m.memory_type.value}] {m.title} (relevance: {m.relevance_score:.2f})")

    elif action == "stats":
        stats = retriever.get_stats()
        results["stats"] = {
            "total_memories": stats.total_memories,
            "by_type": stats.by_type,
            "recent_7d": stats.recent_count_7d,
            "recent_30d": stats.recent_count_30d,
            "avg_relevance": stats.avg_relevance,
            "tag_count": stats.tag_count,
        }
        if json_output:
            typer.echo(json_module.dumps(results, default=str, indent=2))
        else:
            typer.echo("Memory Statistics:")
            typer.echo(f"  Total memories: {stats.total_memories}")
            typer.echo(f"  By type: {json_module.dumps(stats.by_type)}")
            typer.echo(f"  Recent (7d): {stats.recent_count_7d}")
            typer.echo(f"  Recent (30d): {stats.recent_count_30d}")
            typer.echo(f"  Avg relevance: {stats.avg_relevance:.2f}")
            typer.echo(f"  Unique tags: {stats.tag_count}")

    elif action == "list":
        query_type = MemoryType(memory_type) if memory_type != "custom" else None
        memories = retriever.list_memories(memory_type=query_type, limit=max_results)
        results["memories"] = [
            {
                "id": m.id,
                "title": m.title,
                "type": m.memory_type.value,
                "relevance": m.relevance_score,
                "created_at": m.created_at.isoformat(),
            }
            for m in memories
        ]
        if json_output:
            typer.echo(json_module.dumps(results, default=str, indent=2))
        else:
            typer.echo(f"Recent memories ({len(memories)}):")
            for m in memories:
                typer.echo(f"  [{m.memory_type.value}] {m.title} ({m.created_at.strftime('%Y-%m-%d')})")

    elif action == "clear":
        count = store.clear_all()
        typer.echo(f"Cleared {count} memory(ies).")
        results["cleared"] = count

    else:
        typer.echo(f"Unknown action: {action}. Use: store, recall, search, stats, list, clear")

    if json_output and action not in ("stats",):
        typer.echo(json_module.dumps(results, default=str, indent=2))


@app.command()
def bench_weights(
    ctx: typer.Context,
    action: str = typer.Option(
        "update",
        "--action",
        help="Action: update, show, set, or reset.",
    ),
    benching_results: str = typer.Option(
        "",
        "--benching",
        help="JSON string of benching results for updating weights.",
    ),
    factor_name: str = typer.Option(
        "",
        "--factor",
        help="Factor name (required for set action).",
    ),
    weight: float = typer.Option(
        0.0,
        "--weight",
        help="Weight value (required for set action).",
    ),
    min_ic_ir: float = typer.Option(
        0.1,
        "--min-ic-ir",
        help="Minimum IC IR to consider a factor viable.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output results as JSON.",
    ),
) -> None:
    """Manage alpha factor benching weights for persistent scanner scoring."""
    import json as json_module

    from trading_bot.research.benching_weights import BenchingWeightsManager

    manager = BenchingWeightsManager()
    results: dict[str, Any] = {}

    if action == "update":
        if not benching_results:
            typer.echo("Error: --benching JSON string required")
            return

        try:
            bench_data = json_module.loads(benching_results)
        except json_module.JSONDecodeError:
            typer.echo("Error: invalid JSON for --benching")
            return

        updated = manager.update_from_benching(bench_data, min_ic_ir=min_ic_ir)
        stats = manager.get_stats()
        results["updated"] = updated
        results["stats"] = stats
        typer.echo(f"Updated {updated} benching weight(s)")
        typer.echo(f"  Total factors: {stats['total_factors']}")
        typer.echo(f"  Avg weight: {stats['avg_weight']:.4f}")
        typer.echo(f"  Max weight: {stats['max_weight']:.4f}")

    elif action == "show":
        stats = manager.get_stats()
        results["stats"] = stats
        if json_output:
            typer.echo(json_module.dumps(results, default=str, indent=2))
        else:
            typer.echo("Benching Weights:")
            typer.echo(f"  Total factors: {stats['total_factors']}")
            typer.echo(f"  Avg weight: {stats['avg_weight']:.4f}")
            typer.echo(f"  Max weight: {stats['max_weight']:.4f}")
            typer.echo(f"  Min weight: {stats['min_weight']:.4f}")
            if stats.get("weights"):
                typer.echo(f"\n{'Factor':<40} {'Weight':>8}")
                typer.echo("-" * 50)
                for name, w in stats["weights"].items():
                    typer.echo(f"{name:<40} {w:>8.4f}")

    elif action == "set":
        if not factor_name:
            typer.echo("Error: --factor required for set action")
            return
        manager.set_weight(factor_name, weight)
        typer.echo(f"Set weight for {factor_name}: {weight:.4f}")
        results["factor"] = factor_name
        results["weight"] = weight

    elif action == "reset":
        manager.reset()
        typer.echo("Reset all benching weights")
        results["reset"] = True

    else:
        typer.echo(f"Unknown action: {action}. Use: update, show, set, reset")

    if json_output and action not in ("show",):
        typer.echo(json_module.dumps(results, default=str, indent=2))


@app.command(name="cache-data")
def cache_data(
    ctx: typer.Context,
    symbols: str = typer.Option(
        "",
        "--symbols",
        help="Comma-separated symbols to cache (default: watchlist)",
    ),
    period: str = typer.Option(
        "1y",
        "--period",
        help="Data period (default: 1y)",
    ),
    interval: str = typer.Option(
        "1d",
        "--interval",
        help="Data interval (default: 1d)",
    ),
    watchlist_path: str = typer.Option(
        "state/watchlist.txt",
        "--watchlist-path",
        help="Path to watchlist file (default: state/watchlist.txt)",
    ),
    output_dir: str = typer.Option(
        "state/market_data_cache",
        "--output-dir",
        help="Directory to save cached data (default: state/market_data_cache)",
    ),
) -> None:
    """Download and cache historical market data for watchlist symbols.

    Pre-downloads data so RL training and backtesting can run without
    network calls. Data is saved as CSV files in the output directory.
    """
    from pathlib import Path

    from trading_bot.data import market_data
    from trading_bot.runtime.watchlist import read_watchlist

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Resolve symbols
    if symbols:
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        symbol_list = read_watchlist(watchlist_path)

    if not symbol_list:
        typer.echo("No symbols to cache.")
        raise typer.Exit(code=1)

    typer.echo("CACHE DATA")
    typer.echo("=" * 60)
    typer.echo(f"  Symbols: {len(symbol_list)}")
    typer.echo(f"  Period:  {period}")
    typer.echo(f"  Interval: {interval}")
    typer.echo(f"  Output:  {output_path}")
    typer.echo("")

    success = 0
    failed = 0

    for ticker in symbol_list:
        cache_file = output_path / f"{ticker}.csv"
        if cache_file.exists():
            typer.echo(f"  {ticker} SKIP (already cached)")
            success += 1
            continue

        try:
            typer.echo(f"  {ticker} downloading...", nl=False)
            df = market_data.fetch_bars(
                ticker,
                period=period,
                interval=interval,
                settings=ctx.obj.market_data,
            )

            if df.empty:
                typer.echo(f" FAILED (no data)")
                failed += 1
                continue

            df.to_csv(cache_file)
            typer.echo(f" OK ({len(df)} bars)")
            success += 1

        except Exception as e:
            typer.echo(f" FAILED ({e})")
            failed += 1

    typer.echo("")
    typer.echo(f"Done: {success} cached, {failed} failed")
    typer.echo(f"Data saved to: {output_path}")


@app.command(name="reset-portfolio")
def reset_portfolio(
    ctx: typer.Context,
    confirm: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
    backup: bool = typer.Option(
        True,
        "--backup/--no-backup",
        help="Create a timestamped backup of the database before resetting.",
    ),
) -> None:
    """Reset portfolio state, trade history, and equity tracking.

    Clears orders, trades, equity_history, and resets portfolio_state to
    the starting cash amount. This is a destructive operation — use with
    caution.
    """
    settings: Settings = ctx.obj
    db_path = Path(settings.app.state_db_path)

    if not db_path.exists():
        typer.echo(f"No database found at {db_path}")
        raise typer.Exit(code=1)

    typer.echo("PORTFOLIO RESET")
    typer.echo("=" * 60)
    typer.echo(f"  Database: {db_path}")
    typer.echo(f"  Backup:   {'yes' if backup else 'no'}")
    typer.echo("")

    if not confirm:
        typer.echo("WARNING: This will delete all trade history and reset")
        typer.echo("         portfolio state to starting cash.")
        typer.echo("")
        answer = typer.prompt("Type 'RESET' to confirm", default="", show_default=False)
        if answer != "RESET":
            typer.echo("Aborted.")
            raise typer.Exit(code=130)

    import shutil
    import sqlite3
    from datetime import datetime

    # Backup before doing anything
    if backup:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.with_name(f"{db_path.name}.reset_{ts}.bak")
        shutil.copy2(db_path, backup_path)
        typer.echo(f"  Backup created: {backup_path}")

    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        cur = conn.cursor()

        # Count rows before clearing
        tables_to_clear = ["orders", "trades", "equity_history"]
        counts = {}
        for t in tables_to_clear:
            try:
                # Use explicit if/elif blocks to appease static security analysis (Bandit)
                if t == "orders":
                    cur.execute("SELECT COUNT(*) FROM orders")
                elif t == "trades":
                    cur.execute("SELECT COUNT(*) FROM trades")
                elif t == "equity_history":
                    cur.execute("SELECT COUNT(*) FROM equity_history")
                else:
                    raise ValueError(f"Invalid table name: {t}")
                counts[t] = cur.fetchone()[0]
            except sqlite3.OperationalError:
                counts[t] = 0

        # Clear trade history
        for t in tables_to_clear:
            if counts[t] > 0:
                if t == "orders":
                    cur.execute("DELETE FROM orders")
                elif t == "trades":
                    cur.execute("DELETE FROM trades")
                elif t == "equity_history":
                    cur.execute("DELETE FROM equity_history")
                else:
                    raise ValueError(f"Invalid table name: {t}")
                typer.echo(f"  Cleared {t}: {counts[t]} rows")

        # Reset portfolio state to starting cash
        starting_cash = 100_000.0
        initial_state = PortfolioState(
            cash=starting_cash,
            equity=starting_cash,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
        )
        cur.execute(
            """
            INSERT INTO portfolio_state (id, payload)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
            """,
            (initial_state.model_dump_json(),),
        )
        typer.echo(f"  Reset portfolio_state: ${starting_cash:,.2f}")

        # Reset auto-increment counters
        try:
            cur.execute("DELETE FROM sqlite_sequence WHERE name IN ('equity_history', 'trades')")
        except sqlite3.OperationalError:
            pass

        conn.commit()
    finally:
        conn.close()

    typer.echo("")
    typer.echo("Portfolio reset complete.")
