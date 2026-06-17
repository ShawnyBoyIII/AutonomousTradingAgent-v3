# Autonomous Trading Agent

This repository contains a CLI-first paper trading scaffold for stocks and ETFs.

## Quick start

1. Create a virtual environment.
2. Install the project with `pip install -e .[dev]`.
3. Run `./tradebot-local --help` to view the available commands.
4. Try `./tradebot-local scan --symbols AAPL`, `./tradebot-local paper-trade --symbols AAPL`, or `./tradebot-local portfolio`.

`./tradebot-local` is repo-safe path. It always uses this project's `.venv` and bypasses stale global `tradebot` installs on your PATH.

If you still want direct `tradebot`, make sure it resolves to this repo's virtualenv copy instead of an older global install:

```bash
cd /Users/shawndlima/Documents/AutonomousTradingAgent
source .venv/bin/activate
which python
which tradebot
```

Expected paths should point into `/Users/shawndlima/Documents/AutonomousTradingAgent/.venv/`.

If `tradebot` still points at `/opt/anaconda3/bin/tradebot`, use `./tradebot-local` or run with the venv binary explicitly:

```bash
/Users/shawndlima/Documents/AutonomousTradingAgent/.venv/bin/tradebot --help
```

If your environment already picked up NumPy 2 with older compiled packages, reinstall inside the venv:

```bash
cd /Users/shawndlima/Documents/AutonomousTradingAgent
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --force-reinstall "numpy<2" "pandas>=2.2" "pyarrow" "numexpr" "bottleneck"
python -m pip install -e .[dev]
```

## Safety

This CLI is paper-only by default. Live trading is disabled. `scan`, `paper-trade`, `backtest`, `report`, and `portfolio` are wired for local use.

Successful `scan`, `portfolio`, `report`, and `backtest` runs also refresh JSON snapshots under `state/` for local inspection and future UI work.
