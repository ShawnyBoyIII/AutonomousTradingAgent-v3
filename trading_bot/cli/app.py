from datetime import datetime
from pathlib import Path

import typer

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
from trading_bot.runtime.decision_log import append_decision_event
from trading_bot.runtime.snapshots import read_recent_decision_rows, write_snapshot
from trading_bot.strategy.trailing_stop import next_trailing_stop

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
    broker = _paper_broker_from_state(state)
    log_path = Path(ctx.obj.app.log_dir) / "decision-log.jsonl"
    lines: list[str] = []
    actions = 0
    for ticker, position in sorted(state.positions.items()):
        frame = market_data.fetch_bars(
            ticker,
            ctx.obj.market_data.daily_period,
            "1d",
        )
        last_price = float(frame.iloc[-1]["close"])
        if position.stop_loss is not None and last_price <= position.stop_loss:
            fill = broker.submit_order(
                OrderRequest(
                    ticker=ticker,
                    side="SELL",
                    order_type="market",
                    quantity=position.quantity,
                    submitted_at=datetime.now(),
                ),
                market_price=last_price,
            )
            ledger.record_fill(fill, side="SELL")
            state = _portfolio_state_after_sell(
                previous_state=state,
                ticker=ticker,
                fill_price=fill.fill_price,
                fill_fees=fill.fees,
                broker=broker,
            )
            ledger.save_portfolio_state(state)
            append_decision_event(
                log_path,
                {
                    "command": "manage-positions",
                    "ticker": ticker,
                    "status": "FILLED",
                    "reason": "stop",
                    "quantity": fill.quantity,
                    "fill_price": fill.fill_price,
                    "cash": state.cash,
                },
            )
            actions += 1
            lines.append(
                f"{ticker} FILLED reason=stop qty={fill.quantity} "
                f"price={fill.fill_price:.2f} cash={state.cash:.2f}"
            )
            continue
        if position.profit_target is not None and last_price >= position.profit_target:
            fill = broker.submit_order(
                OrderRequest(
                    ticker=ticker,
                    side="SELL",
                    order_type="market",
                    quantity=position.quantity,
                    submitted_at=datetime.now(),
                ),
                market_price=last_price,
            )
            ledger.record_fill(fill, side="SELL")
            state = _portfolio_state_after_sell(
                previous_state=state,
                ticker=ticker,
                fill_price=fill.fill_price,
                fill_fees=fill.fees,
                broker=broker,
            )
            ledger.save_portfolio_state(state)
            append_decision_event(
                log_path,
                {
                    "command": "manage-positions",
                    "ticker": ticker,
                    "status": "FILLED",
                    "reason": "target",
                    "quantity": fill.quantity,
                    "fill_price": fill.fill_price,
                    "cash": state.cash,
                },
            )
            actions += 1
            lines.append(
                f"{ticker} FILLED reason=target qty={fill.quantity} "
                f"price={fill.fill_price:.2f} cash={state.cash:.2f}"
            )
            continue
        trail_update = _update_trailing_stop(position, frame, last_price)
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
    typer.echo(f"positions={len(state.positions)} actions={actions}")
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


def _paper_broker_from_state(state: PortfolioState) -> PaperBroker:
    broker = PaperBroker(starting_cash=state.cash, fee_per_order=1.0, slippage_bps=0)
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
) -> tuple[float, str, float, float] | None:
    """Tighten `position.stop_loss` using the latest frame.

    Returns `(new_stop, method, new_highest_high, new_initial_risk)` when
    the stop ratchets up, otherwise `None` so the caller falls through to
    the standard open-position line. `new_initial_risk` is locked in on
    the first call (entry_price - stop_loss) and persisted for future
    runs so r-multiple math stays stable as the stop moves.
    """
    new_highest_high = max(position.highest_high or last_price, last_price)

    new_initial_risk = position.initial_risk
    if (
        new_initial_risk is None
        and position.stop_loss is not None
        and position.stop_loss < position.average_cost
    ):
        new_initial_risk = round(position.average_cost - position.stop_loss, 4)

    atr_value: float | None = None
    try:
        atr_frame = add_atr(frame, period=14, column_name="atr_14")
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
