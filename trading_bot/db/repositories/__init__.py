"""Production repositories for the trading bot.

These modules back the canonical SQLite-backed persistence layer
(orders, trades, positions, scan results, scan features). They are
imported by the CLI runtime, the burner loop, the dashboard, and
the backtest runner.

Test-only repositories (events, market_data, model_predictions,
portfolio_snapshots) live alongside this package but are NOT re-exported
here. They back tables that no production code writes to or reads
from. Tests that exercise them should import directly from the
submodule path, e.g. ``from trading_bot.db.repositories.market_data
import upsert_market_bars``.
"""
from __future__ import annotations

from trading_bot.db.repositories.positions import (
    close_position,
    get_open_positions,
    upsert_position,
)
from trading_bot.db.repositories.scan_results import (
    get_scan_results,
    upsert_scan_result,
)
from trading_bot.db.repositories.scan_features import (
    get_scan_features,
    upsert_scan_feature,
)
from trading_bot.db.repositories.trades import (
    accumulate_partial_exit,
    get_open_trades,
    get_trades,
    update_trade_exit,
    upsert_trade,
)

__all__ = [
    "close_position",
    "get_open_positions",
    "upsert_position",
    "get_scan_results",
    "upsert_scan_result",
    "get_scan_features",
    "upsert_scan_feature",
    "accumulate_partial_exit",
    "get_open_trades",
    "get_trades",
    "update_trade_exit",
    "upsert_trade",
]
