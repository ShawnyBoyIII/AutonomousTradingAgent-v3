from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_bot.config.settings import Settings
from trading_bot.data import market_data
from trading_bot.data.indicators import add_ema, add_sma
from trading_bot.execution.order_manager import submit_signal_as_order
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.runtime.decision_log import append_decision_event
from trading_bot.runtime.snapshots import write_snapshot
from trading_bot.strategy.intraday_signal_engine import generate_signal


def run_backtest(
    symbols: list[str],
    settings: Settings,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, float | int | list[dict[str, float | int | str | None]]]:
    trades = 0
    wins = 0
    losses = 0
    net_pnl = 0.0
    rows: list[dict[str, float | int | str | None]] = []
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"

    for symbol in (value.strip() for value in symbols if value.strip()):
        daily_frame = market_data.fetch_bars(
            symbol,
            settings.market_data.daily_period,
            "1d",
        )
        intraday_frame = market_data.fetch_bars(
            symbol,
            settings.market_data.intraday_period,
            settings.market_data.intraday_interval,
        )
        daily_frame = add_ema(daily_frame, period=20, column_name="ema_20")
        daily_frame = add_sma(daily_frame, period=50, column_name="sma_50")
        intraday_frame = _filter_frame_by_date(intraday_frame, start=start, end=end)
        intraday_frame = intraday_frame.copy(deep=True)
        intraday_frame["volume_avg_5"] = intraday_frame["volume"].rolling(5).mean()

        result = _run_symbol_backtest(symbol, daily_frame, intraday_frame, settings)
        trades += result["trades"]
        wins += result["wins"]
        losses += result["losses"]
        net_pnl += result["net_pnl"]
        rows.append(
            {
                "ticker": symbol,
                "trades": result["trades"],
                "wins": result["wins"],
                "losses": result["losses"],
                "net_pnl": result["net_pnl"],
                "start": start,
                "end": end,
            }
        )
        append_decision_event(
            log_path,
            {
                "command": "backtest",
                "ticker": symbol,
                "trades": result["trades"],
                "wins": result["wins"],
                "losses": result["losses"],
                "net_pnl": result["net_pnl"],
                "start": start,
                "end": end,
            },
        )

    summary = {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": 0.0 if trades == 0 else wins / trades,
        "net_pnl": round(net_pnl, 2),
        "rows": rows,
    }
    write_snapshot(
        settings.app.backtest_summary_path,
        {
            "mode": "backtest",
            "summary": {
                "trades": summary["trades"],
                "wins": summary["wins"],
                "losses": summary["losses"],
                "win_rate": summary["win_rate"],
                "net_pnl": summary["net_pnl"],
            },
            "rows": rows,
        },
    )
    return summary


def iterate_bars(frame: Any, warmup: int):
    if warmup <= 0:
        raise ValueError("warmup must be positive")

    for end_index in range(warmup, len(frame)):
        yield frame.iloc[:end_index].copy()


def _run_symbol_backtest(
    symbol: str,
    daily_frame: Any,
    intraday_frame: Any,
    settings: Settings,
) -> dict[str, float | int]:
    if len(intraday_frame) < 5:
        return {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}

    broker = PaperBroker(starting_cash=10_000.0, fee_per_order=1.0, slippage_bps=0)
    trades = 0
    wins = 0
    losses = 0
    net_pnl = 0.0
    entry_blocked_until = -1

    for end_index, window in enumerate(iterate_bars(intraday_frame, warmup=5), start=5):
        if end_index <= entry_blocked_until:
            continue

        signal = generate_signal(symbol, daily_frame, window)
        if signal is None:
            continue

        fill = submit_signal_as_order(
            signal=signal,
            broker=broker,
            account_equity=10_000.0,
            open_tickers=set(broker.positions),
            risk_settings=settings.risk,
        )
        if fill is None:
            continue

        entry_index = len(window) - 1
        exit_price, exit_index = _resolve_exit(signal, intraday_frame, entry_index)
        quantity = broker.positions.get(symbol, 0)
        entry_value = fill.fill_price * quantity
        exit_value = exit_price * quantity
        trade_pnl = exit_value - entry_value - fill.fees - broker.fee_per_order
        net_pnl += trade_pnl
        trades += 1
        if trade_pnl > 0:
            wins += 1
        else:
            losses += 1

        broker.cash = round(broker.cash + exit_value - broker.fee_per_order, 2)
        broker.positions.pop(symbol, None)
        entry_blocked_until = exit_index

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "net_pnl": round(net_pnl, 2),
    }


def _filter_frame_by_date(frame: Any, start: str | None, end: str | None):
    if frame.empty or "timestamp" not in frame.columns:
        return frame

    filtered = frame
    timestamp_series = filtered["timestamp"]
    timestamp_tz = getattr(timestamp_series.dt, "tz", None)
    if start is not None:
        start_dt = datetime.combine(date.fromisoformat(start), datetime.min.time())
        if timestamp_tz is not None:
            start_dt = start_dt.replace(tzinfo=timestamp_tz)
        filtered = filtered[filtered["timestamp"] >= start_dt]
    if end is not None:
        end_dt = datetime.combine(date.fromisoformat(end), datetime.min.time()) + timedelta(days=1)
        if timestamp_tz is not None:
            end_dt = end_dt.replace(tzinfo=timestamp_tz)
        filtered = filtered[filtered["timestamp"] < end_dt]
    return filtered.reset_index(drop=True)


def _resolve_exit(signal, intraday_frame: Any, entry_index: int) -> tuple[float, int]:
    for row_index, (_, row) in enumerate(
        intraday_frame.iloc[entry_index + 1 :].iterrows(),
        start=entry_index + 1,
    ):
        if float(row["low"]) <= signal.stop_loss:
            return signal.stop_loss, row_index
        if float(row["high"]) >= signal.profit_target:
            return signal.profit_target, row_index

    return float(intraday_frame.iloc[-1]["close"]), len(intraday_frame) - 1
