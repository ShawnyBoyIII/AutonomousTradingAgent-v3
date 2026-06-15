# Autonomous Trading Agent

This repository contains a CLI-first paper trading scaffold for stocks and ETFs.

## Quick start

1. Create a virtual environment.
2. Install the project with `pip install -e .[dev]`.
3. Run `tradebot --help` to view the available commands.
4. Try `tradebot scan --symbols AAPL` or `tradebot portfolio` to confirm the CLI is wired up.

## Safety

This CLI is paper-only by default. Live trading is disabled, and commands such as `paper-trade`, `backtest`, `report`, and `portfolio` are safe placeholders until the trading workflow is expanded.
