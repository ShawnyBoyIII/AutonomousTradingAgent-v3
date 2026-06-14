import typer

app = typer.Typer(help="Paper-trading CLI for stocks and ETFs.")


@app.command()
def scan() -> None:
    """Scan the configured universe for trade candidates."""
    return None


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
