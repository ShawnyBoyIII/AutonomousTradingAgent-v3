from __future__ import annotations

from trading_bot.db.repositories.market_data import (
    get_latest_bar_timestamp,
    get_market_bars,
    is_market_data_stale,
    upsert_market_bars,
)
from trading_bot.db.repositories.model_predictions import (
    get_predictions,
    upsert_prediction,
)
from trading_bot.db.repositories.positions import (
    close_position,
    get_open_positions,
    upsert_position,
)
from trading_bot.db.repositories.portfolio_snapshots import (
    create_snapshot,
    get_snapshots,
)
from trading_bot.db.repositories.scan_results import (
    get_scan_results,
    upsert_scan_result,
)
from trading_bot.db.repositories.trades import (
    get_open_trades,
    get_trades,
    update_trade_exit,
    upsert_trade,
)
from trading_bot.db.repositories.events import (
    log_event,
    get_events,
)

__all__ = [
    "get_latest_bar_timestamp",
    "get_market_bars",
    "is_market_data_stale",
    "upsert_market_bars",
    "get_scan_results",
    "upsert_scan_result",
    "get_open_trades",
    "get_trades",
    "upsert_trade",
    "update_trade_exit",
    "get_open_positions",
    "upsert_position",
    "close_position",
    "create_snapshot",
    "get_snapshots",
    "get_predictions",
    "upsert_prediction",
    "log_event",
    "get_events",
]
