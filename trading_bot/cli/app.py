from pathlib import Path

import typer

from trading_bot.config.loader import load_settings
from trading_bot.runtime.orchestrator import run_scan

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
def scan(
    symbols: list[str] = typer.Option(
        ...,
        "--symbols",
        help="Symbols to scan for trade candidates.",
    ),
) -> None:
    """Scan the configured universe for trade candidates."""
    run_scan(symbols)


@app.command(name="paper-trade")
def paper_trade() -> None:
    """Run the paper-trading loop."""
    return None


@app.command()
def backtest() -> None:
    """Replay historical data for a strategy."""
    return None


@app.command()
def report() -> None:
    """Print a performance summary."""
    return None


@app.command()
def portfolio() -> None:
    """Inspect the current simulated portfolio."""
    return None
