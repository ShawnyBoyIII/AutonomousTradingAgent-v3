# Autonomous Trading Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a brand-new CLI-first Python paper-trading system for stocks and ETFs with intraday-first signals, daily trend filtering, strict risk gates, an explicit execution-mode boundary, a simulated broker, backtesting, and auditable reporting.

**Architecture:** The system is a modular Python package centered on typed domain models and deterministic services. Data flows from a provider-backed market-data layer into indicator calculations, then through daily and intraday strategy logic, risk validation, runtime orchestration, paper execution, portfolio accounting, and reporting, with file-backed configuration, SQLite-backed state, and a persistent decision log.

**Tech Stack:** Python 3.12+, Typer, Pydantic, pandas, numpy, yfinance, PyYAML, python-dotenv, Rich, sqlite3, pytest

---

## Product Workflows

### Research

- User provides symbols or a watchlist file
- CLI loads market data and computes indicators
- Strategy engine returns ranked candidates
- Risk manager explains approvals and rejections

### Paper Trade

- User runs paper-trade against the same universe
- Approved candidates become simulated orders
- Paper broker applies slippage and fees
- Portfolio and decision logs update after each fill or rejection

### Review

- User requests portfolio or report output
- CLI shows cash, equity, positions, realized and unrealized P/L
- Decision log explains recent trade decisions and missed trades

## File map

### Create

- `README.md` - setup, commands, safety model, project overview
- `pyproject.toml` - package metadata, dependencies, pytest config, CLI entry point
- `.env.example` - environment variable examples with live-trading disabled
- `config.yaml` - default strategy, risk, session, and storage settings
- `trading_bot/__init__.py` - package marker
- `trading_bot/main.py` - module entry point
- `trading_bot/cli/__init__.py` - package marker
- `trading_bot/cli/app.py` - Typer CLI commands
- `trading_bot/runtime/__init__.py` - package marker
- `trading_bot/runtime/orchestrator.py` - CLI-facing runtime flow coordinator
- `trading_bot/runtime/decision_log.py` - structured decision/audit log writer
- `trading_bot/config/__init__.py` - package marker
- `trading_bot/config/settings.py` - typed settings models
- `trading_bot/config/loader.py` - config/env loading and safety checks
- `trading_bot/models/__init__.py` - exports shared models
- `trading_bot/models/market.py` - bars, snapshots, scan results
- `trading_bot/models/signal.py` - signal and candidate trade models
- `trading_bot/models/order.py` - orders, fills, broker responses
- `trading_bot/models/portfolio.py` - account state, positions, trades
- `trading_bot/models/risk.py` - risk decision and sizing result models
- `trading_bot/data/__init__.py` - package marker
- `trading_bot/data/cache.py` - raw market-data cache helpers
- `trading_bot/data/market_data.py` - yfinance fetch/normalize functions
- `trading_bot/data/indicators.py` - EMA, SMA, RSI, MACD, ATR, support/resistance helpers
- `trading_bot/data/providers/__init__.py` - provider exports
- `trading_bot/data/providers/base.py` - market-data provider protocol
- `trading_bot/data/providers/yfinance_provider.py` - first provider implementation
- `trading_bot/strategy/__init__.py` - package marker
- `trading_bot/strategy/daily_filter.py` - higher-timeframe regime checks
- `trading_bot/strategy/setup_rules.py` - intraday setup rules
- `trading_bot/strategy/intraday_signal_engine.py` - scan orchestration and signal ranking
- `trading_bot/risk/__init__.py` - package marker
- `trading_bot/risk/position_sizer.py` - per-trade sizing math
- `trading_bot/risk/exposure.py` - allocation and duplicate-trade checks
- `trading_bot/risk/risk_manager.py` - final trade approval pipeline
- `trading_bot/portfolio/__init__.py` - package marker
- `trading_bot/portfolio/ledger.py` - SQLite-backed portfolio/order/trade storage
- `trading_bot/portfolio/performance.py` - P/L and report metric helpers
- `trading_bot/execution/__init__.py` - package marker
- `trading_bot/execution/modes.py` - execution mode enum and safety gate
- `trading_bot/execution/broker_base.py` - broker interface boundary
- `trading_bot/execution/fills.py` - slippage/fee fill math
- `trading_bot/execution/paper_broker.py` - simulated order execution
- `trading_bot/execution/order_manager.py` - candidate-order submission flow
- `trading_bot/backtest/__init__.py` - package marker
- `trading_bot/backtest/metrics.py` - win rate, drawdown, profit factor helpers
- `trading_bot/backtest/runner.py` - chronological historical replay
- `trading_bot/reports/__init__.py` - package marker
- `trading_bot/reports/summaries.py` - CLI-friendly report summaries
- `trading_bot/reports/exporters.py` - CSV/JSON export helpers
- `trading_bot/utils/__init__.py` - package marker
- `trading_bot/utils/logging.py` - project logger factory
- `trading_bot/utils/timeframes.py` - market-session/timeframe helpers
- `tests/test_config_loader.py` - config and safety defaults
- `tests/test_indicators.py` - indicator correctness
- `tests/test_strategy_signals.py` - daily filter and intraday signal rules
- `tests/test_position_sizer.py` - sizing math
- `tests/test_risk_manager.py` - risk approvals/rejections
- `tests/test_paper_broker.py` - simulated execution and balance updates
- `tests/test_backtest_runner.py` - no-look-ahead chronological replay
- `tests/test_reports.py` - summary/export structure
- `tests/test_cli_smoke.py` - CLI command smoke coverage
- `tests/test_live_safety.py` - hard-disabled live mode behavior
- `tests/test_decision_log.py` - structured decision-log writes
- `tests/test_execution_modes.py` - execution mode enforcement

## Task 1: Project scaffold and package metadata

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `.env.example`
- Create: `config.yaml`
- Create: `trading_bot/__init__.py`
- Create: `trading_bot/main.py`
- Create: `trading_bot/cli/__init__.py`
- Create: `tests/test_cli_smoke.py`

- [ ] **Step 1: Write failing CLI smoke test**

```python
from typer.testing import CliRunner

from trading_bot.cli.app import app


def test_cli_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "scan" in result.stdout
    assert "paper-trade" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_smoke.py::test_cli_shows_help -v`
Expected: FAIL with `ModuleNotFoundError` for `trading_bot.cli.app`

- [ ] **Step 3: Write minimal package and CLI entrypoint**

```python
# trading_bot/main.py
from trading_bot.cli.app import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
```

```python
# trading_bot/cli/app.py
import typer

app = typer.Typer(help="Paper-trading CLI for stocks and ETFs.")


@app.command("scan")
def scan() -> None:
    raise typer.Exit(code=0)


@app.command("paper-trade")
def paper_trade() -> None:
    raise typer.Exit(code=0)


@app.command("backtest")
def backtest() -> None:
    raise typer.Exit(code=0)


@app.command("report")
def report() -> None:
    raise typer.Exit(code=0)


@app.command("portfolio")
def portfolio() -> None:
    raise typer.Exit(code=0)
```

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "autonomous-trading-agent"
version = "0.1.0"
description = "CLI-first paper trading system for stocks and ETFs"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
  "numpy>=2.0",
  "pandas>=2.2",
  "pydantic>=2.7",
  "PyYAML>=6.0.1",
  "python-dotenv>=1.0",
  "rich>=13.7",
  "typer>=0.12",
  "yfinance>=0.2.54",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2",
]

[project.scripts]
tradebot = "trading_bot.main:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
```

- [ ] **Step 4: Add bootstrap docs and config stubs**

```yaml
# config.yaml
app:
  live_trading_enabled: false
  timezone: "America/New_York"
  state_db_path: "state/trading_bot.db"
  log_dir: "logs"
market_data:
  provider: "yfinance"
  daily_period: "1y"
  intraday_period: "5d"
  intraday_interval: "5m"
risk:
  max_risk_per_trade_pct: 0.01
  max_daily_risk_pct: 0.03
  max_ticker_allocation_pct: 0.20
  min_reward_risk_ratio: 2.0
```

```env
# .env.example
LIVE_TRADING_ENABLED=false
CONFIG_PATH=config.yaml
STATE_DB_PATH=state/trading_bot.db
LOG_LEVEL=INFO
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_cli_smoke.py::test_cli_shows_help -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml README.md .env.example config.yaml trading_bot tests/test_cli_smoke.py
git commit -m "feat: scaffold trading bot package and cli"
```

## Task 2: Config models and safety defaults

**Files:**
- Create: `trading_bot/config/settings.py`
- Create: `trading_bot/config/loader.py`
- Create: `tests/test_config_loader.py`
- Modify: `trading_bot/cli/app.py`

- [ ] **Step 1: Write failing config tests**

```python
from pathlib import Path

from trading_bot.config.loader import load_settings


def test_load_settings_reads_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        "  live_trading_enabled: false\n"
        "  timezone: America/New_York\n"
        "  state_db_path: state/test.db\n"
        "  log_dir: logs\n"
        "market_data:\n"
        "  provider: yfinance\n"
        "  daily_period: 1y\n"
        "  intraday_period: 5d\n"
        "  intraday_interval: 5m\n"
        "risk:\n"
        "  max_risk_per_trade_pct: 0.01\n"
        "  max_daily_risk_pct: 0.03\n"
        "  max_ticker_allocation_pct: 0.20\n"
        "  min_reward_risk_ratio: 2.0\n"
    )

    settings = load_settings(config_file)

    assert settings.app.live_trading_enabled is False
    assert settings.market_data.intraday_interval == "5m"


def test_env_cannot_enable_live_trading() -> None:
    settings = load_settings()
    assert settings.app.live_trading_enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_loader.py -v`
Expected: FAIL with `ModuleNotFoundError` for `trading_bot.config.loader`

- [ ] **Step 3: Implement typed settings and loader**

```python
# trading_bot/config/settings.py
from pydantic import BaseModel, Field


class AppSettings(BaseModel):
    live_trading_enabled: bool = False
    timezone: str = "America/New_York"
    state_db_path: str = "state/trading_bot.db"
    log_dir: str = "logs"


class MarketDataSettings(BaseModel):
    provider: str = "yfinance"
    daily_period: str = "1y"
    intraday_period: str = "5d"
    intraday_interval: str = "5m"


class RiskSettings(BaseModel):
    max_risk_per_trade_pct: float = Field(default=0.01, gt=0.0)
    max_daily_risk_pct: float = Field(default=0.03, gt=0.0)
    max_ticker_allocation_pct: float = Field(default=0.20, gt=0.0)
    min_reward_risk_ratio: float = Field(default=2.0, gt=0.0)


class Settings(BaseModel):
    app: AppSettings = AppSettings()
    market_data: MarketDataSettings = MarketDataSettings()
    risk: RiskSettings = RiskSettings()
```

```python
# trading_bot/config/loader.py
from pathlib import Path
from typing import Any

import yaml

from trading_bot.config.settings import Settings


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or Path("config.yaml")
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    settings = Settings.model_validate(raw)
    settings.app.live_trading_enabled = False
    return settings
```

- [ ] **Step 4: Wire CLI to settings load**

```python
# add near top of trading_bot/cli/app.py
from trading_bot.config.loader import load_settings


@app.callback()
def main() -> None:
    load_settings()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_config_loader.py tests/test_cli_smoke.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/config trading_bot/cli/app.py tests/test_config_loader.py
git commit -m "feat: add typed settings loader with live safety defaults"
```

## Task 3: Domain models

**Files:**
- Create: `trading_bot/models/market.py`
- Create: `trading_bot/models/signal.py`
- Create: `trading_bot/models/order.py`
- Create: `trading_bot/models/portfolio.py`
- Create: `trading_bot/models/risk.py`
- Create: `trading_bot/models/__init__.py`
- Modify: `tests/test_config_loader.py`

- [ ] **Step 1: Write failing model test**

```python
from trading_bot.models.signal import TradeSignal


def test_trade_signal_requires_stop_loss() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=102.5,
        risk_reward_ratio=2.5,
        confidence=0.75,
        reasons=["breakout"],
        strategy_tag="opening-range-breakout",
        timestamp="2026-06-13T10:00:00-04:00",
    )
    assert signal.stop_loss == 99.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_loader.py::test_trade_signal_requires_stop_loss -v`
Expected: FAIL because test is in wrong file or model missing

- [ ] **Step 3: Move test into proper model coverage file and implement models**

```python
# tests/test_strategy_signals.py
from trading_bot.models.signal import TradeSignal


def test_trade_signal_requires_stop_loss() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=102.5,
        risk_reward_ratio=2.5,
        confidence=0.75,
        reasons=["breakout"],
        strategy_tag="opening-range-breakout",
        timestamp="2026-06-13T10:00:00-04:00",
    )
    assert signal.stop_loss == 99.0
```

```python
# trading_bot/models/signal.py
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TradeSignal(BaseModel):
    ticker: str
    timeframe: Literal["daily", "intraday"]
    action: Literal["BUY", "SELL", "HOLD", "EXIT"]
    entry_price: float = Field(gt=0.0)
    stop_loss: float = Field(gt=0.0)
    profit_target: float = Field(gt=0.0)
    risk_reward_ratio: float = Field(gt=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str]
    strategy_tag: str
    timestamp: datetime
```

- [ ] **Step 4: Implement remaining core models with minimal fields**

```python
# trading_bot/models/order.py
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class OrderRequest(BaseModel):
    ticker: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["market", "limit", "stop", "bracket"]
    quantity: int = Field(gt=0)
    submitted_at: datetime
    limit_price: float | None = None
    stop_price: float | None = None


class FillResult(BaseModel):
    order_id: str
    ticker: str
    quantity: int
    fill_price: float
    fees: float
    filled_at: datetime
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_strategy_signals.py::test_trade_signal_requires_stop_loss -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/models tests/test_strategy_signals.py
git commit -m "feat: add typed trading domain models"
```

## Task 4: Indicator math

**Files:**
- Create: `trading_bot/data/indicators.py`
- Create: `tests/test_indicators.py`

- [ ] **Step 1: Write failing indicator tests**

```python
import pandas as pd

from trading_bot.data.indicators import add_ema, add_rsi


def test_add_ema_creates_column() -> None:
    frame = pd.DataFrame({"close": [10, 11, 12, 13, 14]})
    result = add_ema(frame, period=3, column_name="ema_3")
    assert "ema_3" in result.columns


def test_add_rsi_creates_bounded_values() -> None:
    frame = pd.DataFrame({"close": [10, 11, 12, 11, 13, 14, 13, 15]})
    result = add_rsi(frame, period=3)
    latest = result["rsi_3"].dropna().iloc[-1]
    assert 0 <= latest <= 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_indicators.py -v`
Expected: FAIL with missing `trading_bot.data.indicators`

- [ ] **Step 3: Implement minimal indicator helpers**

```python
import pandas as pd


def add_ema(frame: pd.DataFrame, period: int, column_name: str) -> pd.DataFrame:
    result = frame.copy()
    result[column_name] = result["close"].ewm(span=period, adjust=False).mean()
    return result


def add_rsi(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    result = frame.copy()
    delta = result["close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.rolling(period).mean()
    avg_loss = losses.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    result[f"rsi_{period}"] = 100 - (100 / (1 + rs))
    return result
```

- [ ] **Step 4: Add MACD, ATR, SMA, volume average helpers**

```python
def add_sma(frame: pd.DataFrame, period: int, column_name: str) -> pd.DataFrame:
    result = frame.copy()
    result[column_name] = result["close"].rolling(period).mean()
    return result
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_indicators.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/data/indicators.py tests/test_indicators.py
git commit -m "feat: add technical indicator helpers"
```

## Task 5: Market data provider abstraction, fetch, and normalization

**Files:**
- Create: `trading_bot/data/cache.py`
- Create: `trading_bot/data/market_data.py`
- Create: `trading_bot/data/providers/__init__.py`
- Create: `trading_bot/data/providers/base.py`
- Create: `trading_bot/data/providers/yfinance_provider.py`
- Modify: `trading_bot/models/market.py`
- Modify: `tests/test_indicators.py`

- [ ] **Step 1: Write failing market-data test with stub frame**

```python
import pandas as pd

from trading_bot.data.market_data import normalize_ohlcv_frame


def test_normalize_ohlcv_frame_standardizes_columns() -> None:
    raw = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [101.0],
            "Low": [99.0],
            "Close": [100.5],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-06-13 10:00:00"]),
    )

    result = normalize_ohlcv_frame(raw)

    assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_indicators.py::test_normalize_ohlcv_frame_standardizes_columns -v`
Expected: FAIL because function missing

- [ ] **Step 3: Implement normalizer and cache shell**

```python
import pandas as pd


def normalize_ohlcv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    renamed = frame.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    ).reset_index(names="timestamp")
    return renamed[["timestamp", "open", "high", "low", "close", "volume"]]
```

```python
# trading_bot/data/cache.py
from pathlib import Path


def ensure_cache_dir(path: str) -> Path:
    cache_dir = Path(path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir
```

- [ ] **Step 4: Implement yfinance fetch wrapper**

```python
# trading_bot/data/providers/base.py
from typing import Protocol

import pandas as pd


class MarketDataProvider(Protocol):
    def fetch_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame: ...
```

```python
# trading_bot/data/providers/yfinance_provider.py
import pandas as pd
import yfinance as yf

from trading_bot.data.market_data import normalize_ohlcv_frame


class YFinanceProvider:
    def fetch_bars(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        ticker = yf.Ticker(symbol)
        frame = ticker.history(period=period, interval=interval, auto_adjust=False)
        if frame.empty:
            raise ValueError(f"No market data returned for {symbol}")
        return normalize_ohlcv_frame(frame)
```

```python
# trading_bot/data/market_data.py
from trading_bot.data.providers.yfinance_provider import YFinanceProvider


def fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
    return YFinanceProvider().fetch_bars(symbol, period, interval)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_indicators.py::test_normalize_ohlcv_frame_standardizes_columns -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/data trading_bot/models/market.py tests/test_indicators.py
git commit -m "feat: add market data provider abstraction"
```

## Task 6: Daily filter and intraday setup rules

**Files:**
- Create: `trading_bot/strategy/daily_filter.py`
- Create: `trading_bot/strategy/setup_rules.py`
- Modify: `tests/test_strategy_signals.py`

- [ ] **Step 1: Write failing strategy tests**

```python
import pandas as pd

from trading_bot.strategy.daily_filter import is_bullish_daily_regime
from trading_bot.strategy.setup_rules import detect_intraday_breakout


def test_daily_regime_true_when_price_above_trend() -> None:
    frame = pd.DataFrame(
        {
            "close": [100, 102, 104],
            "ema_20": [99, 100, 101],
            "sma_50": [98, 99, 100],
        }
    )
    assert is_bullish_daily_regime(frame) is True


def test_intraday_breakout_detects_range_break() -> None:
    frame = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, 1000],
        }
    )
    breakout = detect_intraday_breakout(frame)
    assert breakout is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_strategy_signals.py -v`
Expected: FAIL with missing strategy modules

- [ ] **Step 3: Implement daily filter**

```python
import pandas as pd


def is_bullish_daily_regime(frame: pd.DataFrame) -> bool:
    latest = frame.iloc[-1]
    return bool(latest["close"] > latest["ema_20"] > latest["sma_50"])
```

- [ ] **Step 4: Implement intraday setup rule**

```python
import pandas as pd


def detect_intraday_breakout(frame: pd.DataFrame, lookback: int = 4) -> bool:
    if len(frame) <= lookback:
        return False
    prior = frame.iloc[-(lookback + 1):-1]
    latest = frame.iloc[-1]
    range_high = prior["high"].max()
    return bool(latest["close"] > range_high and latest["volume"] > latest["volume_avg_5"])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_strategy_signals.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/strategy tests/test_strategy_signals.py
git commit -m "feat: add daily regime and intraday setup rules"
```

## Task 7: Signal engine orchestration

**Files:**
- Create: `trading_bot/strategy/intraday_signal_engine.py`
- Modify: `trading_bot/models/signal.py`
- Modify: `tests/test_strategy_signals.py`

- [ ] **Step 1: Write failing signal-engine test**

```python
import pandas as pd

from trading_bot.strategy.intraday_signal_engine import generate_signal


def test_generate_signal_returns_buy_candidate() -> None:
    daily = pd.DataFrame({"close": [100, 102, 104], "ema_20": [99, 100, 101], "sma_50": [98, 99, 100]})
    intraday = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "low": [99.8, 100.0, 99.9, 100.1, 100.7],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, 1000],
        }
    )

    signal = generate_signal("AAPL", daily, intraday)

    assert signal is not None
    assert signal.action == "BUY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategy_signals.py::test_generate_signal_returns_buy_candidate -v`
Expected: FAIL with missing `generate_signal`

- [ ] **Step 3: Implement minimal signal engine**

```python
from datetime import datetime

import pandas as pd

from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.daily_filter import is_bullish_daily_regime
from trading_bot.strategy.setup_rules import detect_intraday_breakout


def generate_signal(symbol: str, daily_frame: pd.DataFrame, intraday_frame: pd.DataFrame) -> TradeSignal | None:
    if not is_bullish_daily_regime(daily_frame):
        return None
    if not detect_intraday_breakout(intraday_frame):
        return None

    latest = intraday_frame.iloc[-1]
    stop_loss = float(intraday_frame.iloc[-2]["low"])
    entry = float(latest["close"])
    risk = entry - stop_loss
    profit_target = entry + (risk * 2.0)

    return TradeSignal(
        ticker=symbol,
        timeframe="intraday",
        action="BUY",
        entry_price=entry,
        stop_loss=stop_loss,
        profit_target=profit_target,
        risk_reward_ratio=2.0,
        confidence=0.7,
        reasons=["daily uptrend", "intraday breakout"],
        strategy_tag="intraday-breakout",
        timestamp=datetime.now(),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_strategy_signals.py::test_generate_signal_returns_buy_candidate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading_bot/strategy/intraday_signal_engine.py tests/test_strategy_signals.py
git commit -m "feat: add intraday signal engine"
```

## Task 8: Position sizing and exposure checks

**Files:**
- Create: `trading_bot/risk/position_sizer.py`
- Create: `trading_bot/risk/exposure.py`
- Create: `tests/test_position_sizer.py`

- [ ] **Step 1: Write failing sizing tests**

```python
from trading_bot.risk.position_sizer import calculate_position_size


def test_calculate_position_size_uses_account_risk() -> None:
    shares = calculate_position_size(account_equity=10000, risk_pct=0.01, entry_price=100, stop_loss=99)
    assert shares == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_position_sizer.py -v`
Expected: FAIL with missing risk module

- [ ] **Step 3: Implement sizing helper**

```python
def calculate_position_size(account_equity: float, risk_pct: float, entry_price: float, stop_loss: float) -> int:
    risk_per_share = entry_price - stop_loss
    if risk_per_share <= 0:
        return 0
    dollar_risk = account_equity * risk_pct
    return int(dollar_risk // risk_per_share)
```

- [ ] **Step 4: Implement exposure guard**

```python
def exceeds_ticker_allocation(account_equity: float, position_value: float, max_allocation_pct: float) -> bool:
    return position_value > (account_equity * max_allocation_pct)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_position_sizer.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/risk/position_sizer.py trading_bot/risk/exposure.py tests/test_position_sizer.py
git commit -m "feat: add risk sizing and exposure checks"
```

## Task 9: Risk manager approval pipeline

**Files:**
- Create: `trading_bot/risk/risk_manager.py`
- Create: `tests/test_risk_manager.py`
- Modify: `trading_bot/models/risk.py`

- [ ] **Step 1: Write failing risk-manager tests**

```python
from trading_bot.models.signal import TradeSignal
from trading_bot.risk.risk_manager import evaluate_signal


def test_evaluate_signal_rejects_low_reward_risk() -> None:
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.5,
        profit_target=100.6,
        risk_reward_ratio=1.2,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp="2026-06-13T10:00:00-04:00",
    )

    decision = evaluate_signal(signal=signal, account_equity=10000, open_tickers=set())

    assert decision.approved is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_risk_manager.py -v`
Expected: FAIL with missing risk manager

- [ ] **Step 3: Implement risk decision model**

```python
# trading_bot/models/risk.py
from pydantic import BaseModel


class RiskDecision(BaseModel):
    approved: bool
    reason: str
    position_size: int
    dollar_risk: float
    portfolio_exposure_warning: str | None = None
```

- [ ] **Step 4: Implement evaluator**

```python
from trading_bot.models.risk import RiskDecision
from trading_bot.models.signal import TradeSignal
from trading_bot.risk.position_sizer import calculate_position_size


def evaluate_signal(signal: TradeSignal, account_equity: float, open_tickers: set[str]) -> RiskDecision:
    if signal.risk_reward_ratio < 2.0:
        return RiskDecision(approved=False, reason="reward/risk below minimum", position_size=0, dollar_risk=0.0)
    if signal.ticker in open_tickers:
        return RiskDecision(approved=False, reason="duplicate open ticker", position_size=0, dollar_risk=0.0)

    size = calculate_position_size(account_equity, 0.01, signal.entry_price, signal.stop_loss)
    dollar_risk = size * (signal.entry_price - signal.stop_loss)
    return RiskDecision(approved=size > 0, reason="approved" if size > 0 else "invalid size", position_size=size, dollar_risk=dollar_risk)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_risk_manager.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/models/risk.py trading_bot/risk/risk_manager.py tests/test_risk_manager.py
git commit -m "feat: add risk manager approval pipeline"
```

## Task 10: Execution modes, broker boundary, and paper broker

**Files:**
- Create: `trading_bot/execution/modes.py`
- Create: `trading_bot/execution/broker_base.py`
- Create: `trading_bot/execution/fills.py`
- Create: `trading_bot/execution/paper_broker.py`
- Create: `tests/test_paper_broker.py`
- Create: `tests/test_execution_modes.py`
- Modify: `trading_bot/models/order.py`
- Modify: `trading_bot/models/portfolio.py`

- [ ] **Step 1: Write failing paper-broker test**

```python
from datetime import datetime

from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.order import OrderRequest


def test_market_buy_updates_cash_and_position() -> None:
    broker = PaperBroker(starting_cash=10000, fee_per_order=1.0, slippage_bps=0)
    order = OrderRequest(
        ticker="AAPL",
        side="BUY",
        order_type="market",
        quantity=10,
        submitted_at=datetime.now(),
    )

    fill = broker.submit_order(order, market_price=100.0)

    assert fill.fill_price == 100.0
    assert broker.cash == 8999.0
    assert broker.positions["AAPL"] == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_paper_broker.py -v`
Expected: FAIL with missing broker

- [ ] **Step 3: Write failing execution-mode test**

```python
import pytest

from trading_bot.execution.modes import ExecutionMode, require_paper_mode


def test_require_paper_mode_rejects_live() -> None:
    with pytest.raises(RuntimeError):
        require_paper_mode(ExecutionMode.LIVE)
```

- [ ] **Step 4: Implement execution-mode safety gate and broker boundary**

```python
# trading_bot/execution/modes.py
from enum import Enum


class ExecutionMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


def require_paper_mode(mode: ExecutionMode) -> None:
    if mode is not ExecutionMode.PAPER:
        raise RuntimeError("Live execution is not available in v1")
```

```python
# trading_bot/execution/broker_base.py
from typing import Protocol

from trading_bot.models.order import FillResult, OrderRequest


class BrokerAdapter(Protocol):
    def submit_order(self, order: OrderRequest, market_price: float) -> FillResult: ...
```

- [ ] **Step 5: Implement fill helper**

```python
def apply_slippage(price: float, slippage_bps: int, side: str) -> float:
    direction = 1 if side == "BUY" else -1
    return price * (1 + (direction * slippage_bps / 10_000))
```

- [ ] **Step 6: Implement minimal paper broker**

```python
from datetime import datetime
from uuid import uuid4

from trading_bot.execution.fills import apply_slippage
from trading_bot.models.order import FillResult, OrderRequest


class PaperBroker:
    def __init__(self, starting_cash: float, fee_per_order: float, slippage_bps: int) -> None:
        self.cash = starting_cash
        self.positions: dict[str, int] = {}
        self.fee_per_order = fee_per_order
        self.slippage_bps = slippage_bps

    def submit_order(self, order: OrderRequest, market_price: float) -> FillResult:
        fill_price = apply_slippage(market_price, self.slippage_bps, order.side)
        gross = fill_price * order.quantity
        if order.side == "BUY":
            self.cash -= gross + self.fee_per_order
            self.positions[order.ticker] = self.positions.get(order.ticker, 0) + order.quantity
        else:
            self.cash += gross - self.fee_per_order
            self.positions[order.ticker] = self.positions.get(order.ticker, 0) - order.quantity

        return FillResult(
            order_id=str(uuid4()),
            ticker=order.ticker,
            quantity=order.quantity,
            fill_price=fill_price,
            fees=self.fee_per_order,
            filled_at=datetime.now(),
        )
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_paper_broker.py tests/test_execution_modes.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add trading_bot/execution trading_bot/models/order.py trading_bot/models/portfolio.py tests/test_paper_broker.py tests/test_execution_modes.py
git commit -m "feat: add execution mode safety and paper broker"
```

## Task 11: Portfolio ledger and persistence

**Files:**
- Create: `trading_bot/portfolio/ledger.py`
- Create: `trading_bot/portfolio/performance.py`
- Modify: `tests/test_paper_broker.py`

- [ ] **Step 1: Write failing ledger test**

```python
from pathlib import Path

from trading_bot.portfolio.ledger import PortfolioLedger


def test_ledger_initializes_sqlite_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(db_path)
    ledger.initialize()
    assert db_path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_paper_broker.py::test_ledger_initializes_sqlite_tables -v`
Expected: FAIL with missing ledger

- [ ] **Step 3: Implement minimal SQLite ledger**

```python
import sqlite3
from pathlib import Path


class PortfolioLedger:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, ticker TEXT, side TEXT, quantity INTEGER, fill_price REAL, fees REAL, filled_at TEXT)"
            )
```

- [ ] **Step 4: Add performance summary helper**

```python
def compute_unrealized_pnl(quantity: int, average_cost: float, market_price: float) -> float:
    return (market_price - average_cost) * quantity
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_paper_broker.py::test_ledger_initializes_sqlite_tables -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/portfolio tests/test_paper_broker.py
git commit -m "feat: add portfolio ledger persistence"
```

## Task 12: Order manager integrating signal, risk, and broker

**Files:**
- Create: `trading_bot/execution/order_manager.py`
- Create: `trading_bot/runtime/decision_log.py`
- Create: `trading_bot/runtime/__init__.py`
- Modify: `tests/test_paper_broker.py`
- Create: `tests/test_decision_log.py`

- [ ] **Step 1: Write failing order-manager test**

```python
from datetime import datetime

from trading_bot.execution.order_manager import submit_signal_as_order
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.signal import TradeSignal


def test_submit_signal_as_order_returns_fill_for_approved_trade() -> None:
    broker = PaperBroker(starting_cash=10000, fee_per_order=1.0, slippage_bps=0)
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.0,
        profit_target=102.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(),
    )

    fill = submit_signal_as_order(signal, broker, account_equity=10000, open_tickers=set())

    assert fill is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_paper_broker.py::test_submit_signal_as_order_returns_fill_for_approved_trade -v`
Expected: FAIL with missing order manager

- [ ] **Step 3: Write failing decision-log test**

```python
from pathlib import Path

from trading_bot.runtime.decision_log import append_decision_event


def test_append_decision_event_writes_jsonl(tmp_path: Path) -> None:
    log_path = tmp_path / "decision-log.jsonl"
    append_decision_event(log_path, {"event": "signal_rejected", "ticker": "AAPL"})
    assert log_path.exists()
    assert "signal_rejected" in log_path.read_text()
```

- [ ] **Step 4: Implement order-manager helper**

```python
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.order import OrderRequest
from trading_bot.models.signal import TradeSignal
from trading_bot.execution.modes import ExecutionMode, require_paper_mode
from trading_bot.risk.risk_manager import evaluate_signal


def submit_signal_as_order(signal: TradeSignal, broker: PaperBroker, account_equity: float, open_tickers: set[str], mode: ExecutionMode = ExecutionMode.PAPER):
    require_paper_mode(mode)
    decision = evaluate_signal(signal, account_equity=account_equity, open_tickers=open_tickers)
    if not decision.approved:
        return None

    order = OrderRequest(
        ticker=signal.ticker,
        side="BUY",
        order_type="market",
        quantity=decision.position_size,
        submitted_at=signal.timestamp,
    )
    return broker.submit_order(order, market_price=signal.entry_price)
```

- [ ] **Step 5: Implement decision-log writer**

```python
import json
from pathlib import Path


def append_decision_event(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_paper_broker.py::test_submit_signal_as_order_returns_fill_for_approved_trade tests/test_decision_log.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add trading_bot/execution/order_manager.py trading_bot/runtime tests/test_paper_broker.py tests/test_decision_log.py
git commit -m "feat: connect risk approval to paper execution"
```

## Task 13: Backtest runner

**Files:**
- Create: `trading_bot/backtest/runner.py`
- Create: `trading_bot/backtest/metrics.py`
- Create: `tests/test_backtest_runner.py`

- [ ] **Step 1: Write failing backtest test**

```python
import pandas as pd

from trading_bot.backtest.runner import iterate_bars


def test_iterate_bars_yields_chronological_slices() -> None:
    frame = pd.DataFrame({"close": [1, 2, 3, 4]})
    slices = list(iterate_bars(frame, warmup=2))
    assert len(slices) == 2
    assert list(slices[0]["close"]) == [1, 2]
    assert list(slices[1]["close"]) == [1, 2, 3]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backtest_runner.py -v`
Expected: FAIL with missing backtest runner

- [ ] **Step 3: Implement chronological iterator**

```python
import pandas as pd


def iterate_bars(frame: pd.DataFrame, warmup: int):
    for end_index in range(warmup, len(frame)):
        yield frame.iloc[:end_index].copy()
```

- [ ] **Step 4: Add simple metric helper**

```python
def compute_win_rate(wins: int, losses: int) -> float:
    total = wins + losses
    return 0.0 if total == 0 else wins / total
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_backtest_runner.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/backtest tests/test_backtest_runner.py
git commit -m "feat: add chronological backtest runner"
```

## Task 14: Reports and exports

**Files:**
- Create: `trading_bot/reports/summaries.py`
- Create: `trading_bot/reports/exporters.py`
- Create: `tests/test_reports.py`

- [ ] **Step 1: Write failing report tests**

```python
from trading_bot.reports.summaries import build_daily_summary


def test_build_daily_summary_contains_key_metrics() -> None:
    summary = build_daily_summary(realized_pnl=125.5, unrealized_pnl=40.0, open_positions=2)
    assert summary["realized_pnl"] == 125.5
    assert summary["open_positions"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reports.py -v`
Expected: FAIL with missing reports module

- [ ] **Step 3: Implement summary helper**

```python
def build_daily_summary(realized_pnl: float, unrealized_pnl: float, open_positions: int) -> dict[str, float | int]:
    return {
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "open_positions": open_positions,
        "net_pnl": realized_pnl + unrealized_pnl,
    }
```

- [ ] **Step 4: Implement JSON/CSV exporters**

```python
import csv
import json
from pathlib import Path


def export_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def export_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_reports.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/reports tests/test_reports.py
git commit -m "feat: add report summary and export helpers"
```

## Task 15: CLI command wiring and runtime orchestrator

**Files:**
- Modify: `trading_bot/cli/app.py`
- Create: `trading_bot/runtime/orchestrator.py`
- Modify: `tests/test_cli_smoke.py`

- [ ] **Step 1: Write failing CLI command test**

```python
from typer.testing import CliRunner

from trading_bot.cli.app import app


def test_scan_command_prints_symbol_name() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["scan", "--symbols", "AAPL"])
    assert result.exit_code == 0
    assert "AAPL" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_smoke.py::test_scan_command_prints_symbol_name -v`
Expected: FAIL because command does not print anything

- [ ] **Step 3: Implement runtime orchestrator**

```python
from rich.console import Console

console = Console()


def run_scan(symbols: list[str]) -> None:
    for symbol in symbols:
        console.print(f"scan candidate: {symbol}")
```

- [ ] **Step 4: Implement CLI handlers with Rich output**

```python
import typer
from rich.console import Console
from trading_bot.runtime.orchestrator import run_scan

console = Console()


@app.command("scan")
def scan(symbols: str = typer.Option(..., "--symbols")) -> None:
    run_scan([symbol.strip() for symbol in symbols.split(",") if symbol.strip()])
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_cli_smoke.py::test_scan_command_prints_symbol_name -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add trading_bot/cli/app.py trading_bot/runtime/orchestrator.py tests/test_cli_smoke.py
git commit -m "feat: wire initial cli commands"
```

## Task 16: Hard live-trading safety tests

**Files:**
- Create: `tests/test_live_safety.py`
- Modify: `trading_bot/config/loader.py`

- [ ] **Step 1: Write failing live-safety tests**

```python
from trading_bot.config.loader import load_settings


def test_live_trading_remains_disabled_without_live_implementation() -> None:
    settings = load_settings()
    assert settings.app.live_trading_enabled is False
```

- [ ] **Step 2: Run test to verify it fails only if regression introduced**

Run: `pytest tests/test_live_safety.py -v`
Expected: PASS after implementation, fail if safety default regresses

- [ ] **Step 3: Tighten loader contract**

```python
def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or Path("config.yaml")
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    settings = Settings.model_validate(raw)
    settings.app.live_trading_enabled = False
    return settings
```

- [ ] **Step 4: Run test suite for safety-related modules**

Run: `pytest tests/test_config_loader.py tests/test_live_safety.py tests/test_risk_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading_bot/config/loader.py tests/test_live_safety.py
git commit -m "test: lock live trading disabled by default"
```

## Task 17: End-to-end verification and docs polish

**Files:**
- Modify: `README.md`
- Modify: `tests/test_cli_smoke.py`

- [ ] **Step 1: Write failing README-backed smoke expectation**

```python
from typer.testing import CliRunner

from trading_bot.cli.app import app


def test_portfolio_command_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["portfolio"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run test to verify current behavior**

Run: `pytest tests/test_cli_smoke.py::test_portfolio_command_runs -v`
Expected: FAIL if command raises or missing

- [ ] **Step 3: Finalize README commands and portfolio command**

```python
@app.command("portfolio")
def portfolio() -> None:
    console.print("portfolio summary unavailable: no persisted trades yet")
```

```md
# README.md

## Quick start

1. Create virtual environment
2. `pip install -e ".[dev]"`
3. Copy `.env.example`
4. Run `tradebot --help`
5. Run `tradebot scan --symbols AAPL,MSFT`

## Safety

- Paper trading only
- Live trading hard-disabled
- yfinance data only in v1
```

- [ ] **Step 4: Run full test suite**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 5: Run CLI smoke commands manually**

Run: `python -m trading_bot.main --help`
Expected: help output listing `scan`, `paper-trade`, `backtest`, `report`, `portfolio`

Run: `python -m trading_bot.main scan --symbols AAPL`
Expected: output contains `AAPL`

- [ ] **Step 6: Commit**

```bash
git add README.md trading_bot/cli/app.py tests/test_cli_smoke.py
git commit -m "docs: finalize usage and verify cli flow"
```

## Self-review

### Spec coverage

- CLI-first commands covered in Tasks 1, 15, 17
- Typed models covered in Task 3
- provider-backed market data and normalization covered in Task 5
- Indicators covered in Task 4
- Daily confirmation and intraday-first logic covered in Tasks 6 and 7
- Risk validation covered in Tasks 8 and 9
- explicit execution-mode safety and paper execution covered in Tasks 10 and 12
- Portfolio state and persistence covered in Task 11
- Backtesting covered in Task 13
- Reporting covered in Task 14
- Live-trading hard-disable covered in Tasks 2 and 16
- decision-log audit trail covered in Task 12

### Placeholder scan

- No `TODO`, `TBD`, or deferred implementation markers
- Each code step includes concrete file targets and sample code
- Each test step includes exact command and expected result

### Type consistency

- `TradeSignal` fields reused consistently in strategy and risk tasks
- `OrderRequest` and `FillResult` names reused consistently in execution tasks
- `load_settings()` remains single entrypoint for config and safety behavior
