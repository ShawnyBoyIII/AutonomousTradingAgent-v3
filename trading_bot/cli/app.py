from pathlib import Path

import typer

from trading_bot.config.loader import load_settings
from trading_bot.models.portfolio import PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.portfolio.performance import (
    compute_exposure_ratio,
    compute_position_market_value,
    compute_unrealized_pnl,
)
from trading_bot.reports.exporters import export_csv, export_json
from trading_bot.reports.summaries import build_daily_summary
from trading_bot.runtime.snapshots import read_recent_decision_rows, write_snapshot

app = typer.Typer(help="Paper-trading CLI for stocks and ETFs.")


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(
        None,
        "--config-path",
        help="Path to the YAML config file.",
    ),
) -> None:
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

    parsed_symbols: list[str] = []
    for raw_value in symbols:
        parsed_symbols.extend(
            symbol.strip() for symbol in raw_value.split(",") if symbol.strip()
        )

    scan_result = run_scan(parsed_symbols, ctx.obj, include_details=why)
    for result in scan_result["lines"]:
        typer.echo(result)
    if summary:
        typer.echo(_format_scan_summary(scan_result["summary"]))


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
) -> None:
    """Replay historical data for a strategy."""
    from trading_bot.backtest.runner import run_backtest

    parsed_symbols: list[str] = []
    for raw_value in symbols:
        parsed_symbols.extend(
            symbol.strip() for symbol in raw_value.split(",") if symbol.strip()
        )

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


@app.command(name="manage-positions")
def manage_positions(ctx: typer.Context) -> None:
    """Run one position-management check."""
    from trading_bot.data import market_data

    ledger = PortfolioLedger(Path(ctx.obj.app.state_db_path))
    state = ledger.ensure_portfolio_state()
    typer.echo(f"positions={len(state.positions)} actions=0")
    for ticker, position in sorted(state.positions.items()):
        frame = market_data.fetch_bars(
            ticker,
            ctx.obj.market_data.daily_period,
            "1d",
        )
        last_price = float(frame.iloc[-1]["close"])
        typer.echo(
            f"{ticker} qty={position.quantity} "
            f"avg={position.average_cost:.2f} last={last_price:.2f}"
        )


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


def _fetch_latest_prices(symbols: list[str], settings) -> dict[str, float]:
    if not symbols:
        return {}

    from trading_bot.data import market_data

    prices: dict[str, float] = {}
    for symbol in symbols:
        try:
            frame = market_data.fetch_bars(
                symbol,
                settings.market_data.daily_period,
                "1d",
            )
        except Exception:
            continue
        if frame.empty or "close" not in frame.columns:
            continue
        prices[symbol] = float(frame.iloc[-1]["close"])
    return prices


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
    return " ".join(
        [
            "doctor",
            f"live_trading={str(settings.app.live_trading_enabled).lower()}",
            f"state_db={_exists_label(settings.app.state_db_path)}",
            f"log_dir={_exists_label(settings.app.log_dir)}",
            f"snapshots={ready_snapshots}/{len(snapshots)}",
        ]
    )


def _exists_label(path: str) -> str:
    return "ok" if Path(path).exists() else "missing"
