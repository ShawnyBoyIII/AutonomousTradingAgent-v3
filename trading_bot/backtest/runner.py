from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trading_bot.backtest.diagnostics import attach_diagnostics, diagnostics
from trading_bot.config.settings import Settings
from trading_bot.data import market_data
from trading_bot.data.indicators import add_atr, add_bollinger_bands, add_ema, add_rsi, add_sma
from trading_bot.execution.order_manager import submit_signal_as_order
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.runtime.decision_log import append_decision_event
from trading_bot.runtime.snapshots import write_snapshot
from trading_bot.strategy.daily_signal_engine import generate_daily_signal
from trading_bot.strategy.intraday_signal_engine import generate_signal

logger = logging.getLogger(__name__)


def _fetch_bars_compat(fetch_fn: Any, symbol: str, period: str, interval: str, **kwargs: Any) -> Any:
    """Call fetch_bars, falling back to no-settings signature for test mocks."""
    try:
        return fetch_fn(symbol, period, interval, **kwargs)
    except TypeError:
        kwargs.pop("settings", None)
        return fetch_fn(symbol, period, interval, **kwargs)


def _build_equity_curve(
    closes,
    trade_records: list[dict[str, float | int]],
    starting_cash: float,
) -> list[float]:
    n = len(closes)
    equity = np.full(n, float(starting_cash))
    cash = float(starting_cash)
    for trade in trade_records:
        entry_idx = int(trade["entry_index"])
        exit_idx = int(trade["exit_index"])
        qty = trade["quantity"]
        if qty == 0:
            continue
        cash -= qty * trade["entry_price"] + trade["entry_fees"]
        last_hold = min(exit_idx, n - 1)
        for bar in range(entry_idx, last_hold + 1):
            equity[bar] = cash + qty * closes[bar]
        cash += qty * trade["exit_price"] - trade["exit_fees"]
        for bar in range(exit_idx + 1, n):
            equity[bar] = cash
    return equity.tolist()


def _aggregate_strategy_returns(equity_curves: list[list[float]]) -> list[float]:
    if not equity_curves:
        return []
    min_len = min((len(c) for c in equity_curves), default=0)
    if min_len < 2:
        return []
    combined = np.zeros(min_len)
    for curve in equity_curves:
        combined += np.array(curve[:min_len], dtype=float)
    returns = pd.Series(combined).pct_change().dropna()
    return returns.tolist()


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
    gross_profit = 0.0
    gross_loss = 0.0
    rows: list[dict[str, float | int | str | None]] = []
    equity_curves: list[list[float]] = []
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"

    for symbol in (value.strip() for value in symbols if value.strip()):
        daily_frame = _fetch_bars_compat(
            market_data.fetch_bars,
            symbol,
            settings.market_data.daily_period,
            "1d",
            start=start,
            end=end,
            settings=settings.market_data,
        )
        try:
            intraday_frame = _fetch_bars_compat(
                market_data.fetch_bars,
                symbol,
                settings.market_data.intraday_period,
                settings.market_data.intraday_interval,
                start=start,
                end=end,
                settings=settings.market_data,
            )
        except ValueError:
            # Try 1h data (730 days available)
            try:
                intraday_frame = _fetch_bars_compat(
                    market_data.fetch_bars,
                    symbol, "1y", "1h", start=start, end=end, settings=settings.market_data
                )
                logger.info("Note: Using 1h data for %s (5m unavailable for date range)", symbol)
            except ValueError:
                # Fall back to daily bars as "intraday"
                intraday_frame = daily_frame.copy()
                logger.info("Note: Using daily bars for %s (intraday unavailable)", symbol)
        
        # Detect daily mode: when intraday is same frequency as daily
        daily_mode = len(intraday_frame) == len(daily_frame) and all(
            intraday_frame["timestamp"].iloc[i] == daily_frame["timestamp"].iloc[i]
            for i in range(min(len(intraday_frame), len(daily_frame)))
        ) if not intraday_frame.empty and not daily_frame.empty else False
        
        daily_frame = add_ema(daily_frame, period=20, column_name="ema_20")
        daily_frame = add_sma(daily_frame, period=50, column_name="sma_50")

        use_v3 = (
            getattr(settings, "strategy", None) is not None
            and settings.strategy.use_v3_signals
        )
        if use_v3:
            daily_frame = add_atr(daily_frame, period=settings.risk.atr_period)
            daily_frame = add_bollinger_bands(daily_frame, period=20)

        intraday_frame = _filter_frame_by_date(intraday_frame, start=start, end=end)
        intraday_frame = intraday_frame.copy(deep=True)
        intraday_frame["volume_avg_5"] = intraday_frame["volume"].rolling(5).mean()
        intraday_frame = add_atr(intraday_frame, period=settings.risk.atr_period)
        if use_v3:
            intraday_frame = add_rsi(intraday_frame, period=14)

        if daily_mode:
            result = _run_symbol_backtest_daily(symbol, daily_frame, settings)
        else:
            result = _run_symbol_backtest(symbol, daily_frame, intraday_frame, settings)
        trades += result["trades"]
        wins += result["wins"]
        losses += result["losses"]
        net_pnl += result["net_pnl"]
        gross_profit += float(result.get("gross_profit", 0.0))
        gross_loss += float(result.get("gross_loss", 0.0))
        curve = result.get("equity_curve") or []
        if curve:
            equity_curves.append(list(curve))
        rows.append(
            {
                "ticker": symbol,
                "trades": result["trades"],
                "wins": result["wins"],
                "losses": result["losses"],
                "net_pnl": result["net_pnl"],
                "gross_profit": result.get("gross_profit", 0.0),
                "gross_loss": result.get("gross_loss", 0.0),
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
    summary.update(
        diagnostics(
            trades=trades,
            wins=wins,
            losses=losses,
            net_pnl=net_pnl,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
        )
    )
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

    # Run attribution analysis
    summary["strategy_returns"] = _aggregate_strategy_returns(equity_curves)
    try:
        from trading_bot.backtest.attribution import run_attribution
        benchmark_data = None
        if benchmark_symbol := getattr(settings.app, "benchmark_symbol", None):
            try:
                benchmark_data = _fetch_bars_compat(
                    market_data.fetch_bars,
                    benchmark_symbol,
                    settings.market_data.daily_period,
                    "1d",
                    start=start,
                    end=end,
                    settings=settings.market_data,
                )
            except Exception as e:
                logger.debug("Backtest error: %s", e)
        if benchmark_data is not None and not benchmark_data.empty:
            benchmark_returns = benchmark_data["close"].astype(float).pct_change().dropna()
            summary["benchmark_returns"] = benchmark_returns.tolist()
        summary["attribution"] = run_attribution(
            summary,
            benchmark_data=benchmark_data,
        )
    except Exception as e:
        logger.warning("Attribution analysis failed: %s", e)

    return summary


def iterate_bars(frame: Any, warmup: int) -> Any:
    if warmup <= 0:
        raise ValueError("warmup must be positive")

    for end_index in range(warmup, len(frame)):
        yield frame.iloc[:end_index].copy()


def _evaluate_counter_thesis_for_backtest(
    symbol: str,
    signal: Any,
    daily_frame: Any,
    intraday_frame: Any,
    settings: Settings,
):
    """Build counter-thesis context from in-memory frames and evaluate.

    Uses ``build_counter_thesis_context`` (pure builder, no network) so the
    backtest replays identically to live scan/paper-trade without making
    extra fetch calls. Returns None when the feature is disabled; returns a
    non-blocking empty result when data is insufficient (never blocks on
    missing data — same safety contract as the orchestrator).
    """
    if not settings.counter_thesis.enabled:
        return None
    from trading_bot.strategy.counter_thesis import (
        build_counter_thesis_context,
        evaluate_counter_thesis,
    )

    ctx = build_counter_thesis_context(
        symbol=symbol,
        signal=signal,
        daily_frame=daily_frame,
        intraday_frame=intraday_frame,
        atr_period=settings.risk.atr_period,
    )
    return evaluate_counter_thesis(ctx, signal, settings.counter_thesis)


def _run_symbol_backtest(
    symbol: str,
    daily_frame: Any,
    intraday_frame: Any,
    settings: Settings,
) -> dict[str, float | int]:
    if len(intraday_frame) < 5:
        return {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "equity_curve": []}

    broker = PaperBroker(starting_cash=10_000.0, fee_per_order=1.0, slippage_bps=0)
    trades = 0
    wins = 0
    losses = 0
    net_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    entry_blocked_until = -1
    trade_records: list[dict[str, float | int]] = []

    use_v3 = (
        getattr(settings, "strategy", None) is not None
        and settings.strategy.use_v3_signals
    )
    selector = None
    if use_v3:
        from trading_bot.strategy.strategy_selector import StrategySelector, selection_to_signal

        selector = StrategySelector(risk_tolerance=settings.strategy.risk_tolerance)
        selector.min_confidence = settings.strategy.min_confidence
        selector.atr_stop_multiplier = settings.risk.atr_stop_multiplier

    for end_index, window in enumerate(iterate_bars(intraday_frame, warmup=5), start=5):
        if end_index <= entry_blocked_until:
            continue

        if use_v3 and selector is not None:
            selection = selector.select_strategy(symbol, daily_frame, window)
            if not selection.should_trade or selection.signal_score is None:
                continue
            signal = selection_to_signal(symbol, selection, window)
            if signal is None:
                continue
        else:
            signal = generate_signal(symbol, daily_frame, window)
        if signal is None:
            continue

        counter_result = _evaluate_counter_thesis_for_backtest(
            symbol, signal, daily_frame, window, settings
        )

        fill = submit_signal_as_order(
            signal=signal,
            broker=broker,
            account_equity=10_000.0,
            open_tickers=set(broker.positions),
            risk_settings=settings.risk,
            counter_thesis=counter_result,
        )
        if fill is None:
            continue

        entry_index = len(window) - 1
        # NOTE: _resolve_exit looks at the full intraday_frame for exit
        # resolution.  This introduces look-ahead bias (the backtest sees
        # future bars) because refactoring to a true event-driven model is
        # a larger change.  The backtest is intended for rough estimation,
        # not precise real-time simulation.
        exit_price, exit_index = _resolve_exit(signal, intraday_frame, entry_index)
        quantity = broker.positions.get(symbol, 0)
        trade_records.append({
            "entry_index": entry_index,
            "exit_index": exit_index,
            "quantity": quantity,
            "entry_price": fill.fill_price,
            "exit_price": exit_price,
            "entry_fees": fill.fees,
            "exit_fees": broker.fee_per_order,
        })
        entry_value = fill.fill_price * quantity
        exit_value = exit_price * quantity
        trade_pnl = exit_value - entry_value - fill.fees - broker.fee_per_order
        net_pnl += trade_pnl
        if trade_pnl > 0:
            gross_profit += trade_pnl
        else:
            gross_loss += trade_pnl
        trades += 1
        if trade_pnl > 0:
            wins += 1
        else:
            losses += 1

        broker.cash = round(broker.cash + exit_value - broker.fee_per_order, 2)
        broker.positions.pop(symbol, None)
        entry_blocked_until = exit_index

    closes = intraday_frame["close"].astype(float).reset_index(drop=True).values
    equity_curve = _build_equity_curve(closes, trade_records, 10_000.0)

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "net_pnl": round(net_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "equity_curve": equity_curve,
        **diagnostics(
            trades=trades,
            wins=wins,
            losses=losses,
            net_pnl=net_pnl,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
        ),
    }


def _run_symbol_backtest_daily(
    symbol: str,
    daily_frame: Any,
    settings: Settings,
) -> dict[str, float | int]:
    """Run backtest using daily bar signals (no intraday data needed)."""
    if len(daily_frame) < 50:  # Need warmup for EMA/SMA
        return {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "equity_curve": []}

    broker = PaperBroker(starting_cash=10_000.0, fee_per_order=1.0, slippage_bps=0)
    trades = 0
    wins = 0
    losses = 0
    net_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    entry_blocked_until = -1
    trade_records: list[dict[str, float | int]] = []

    use_v3 = (
        getattr(settings, "strategy", None) is not None
        and settings.strategy.use_v3_signals
    )

    selector = None
    if use_v3:
        from trading_bot.strategy.strategy_selector import StrategySelector, selection_to_signal

        selector = StrategySelector(risk_tolerance=settings.strategy.risk_tolerance)
        selector.min_confidence = settings.strategy.min_confidence
        selector.atr_stop_multiplier = settings.risk.atr_stop_multiplier

    for index in range(50, len(daily_frame)):
        if index <= entry_blocked_until:
            continue

        if use_v3 and selector is not None:
            window = daily_frame.iloc[: index + 1]
            selection = selector.select_strategy(symbol, daily_frame, window)
            if not selection.should_trade or selection.signal_score is None:
                continue
            signal = selection_to_signal(symbol, selection, window)
            if signal is None:
                continue
        else:
            signal = generate_daily_signal(symbol, daily_frame, index)
            if signal is None:
                continue
            window = daily_frame.iloc[: index + 1]

        counter_result = _evaluate_counter_thesis_for_backtest(
            symbol, signal, daily_frame, window, settings
        )

        fill = submit_signal_as_order(
            signal=signal,
            broker=broker,
            account_equity=broker.cash + sum(
                quantity * daily_frame.iloc[index]["close"]
                for quantity in broker.positions.values()
            ),
            open_tickers=set(broker.positions),
            risk_settings=settings.risk,
            counter_thesis=counter_result,
        )
        if fill is None:
            continue

        # Hold for fixed period or use trailing stop logic
        # Simple: hold for 5 days or exit on stop/target
        hold_days = 5
        exit_index = min(index + hold_days, len(daily_frame) - 1)
        
        # Check if stop/target hit during hold period
        for check_idx in range(index + 1, exit_index + 1):
            bar = daily_frame.iloc[check_idx]
            if bar["low"] <= signal.stop_loss:
                exit_price = signal.stop_loss
                exit_index = check_idx
                break
            if bar["high"] >= signal.profit_target:
                exit_price = signal.profit_target
                exit_index = check_idx
                break
        else:
            # No stop/target hit, exit at close
            exit_price = daily_frame.iloc[exit_index]["close"]

        quantity = broker.positions.get(symbol, 0)
        trade_records.append({
            "entry_index": index,
            "exit_index": exit_index,
            "quantity": quantity,
            "entry_price": fill.fill_price,
            "exit_price": exit_price,
            "entry_fees": fill.fees,
            "exit_fees": broker.fee_per_order,
        })
        entry_value = fill.fill_price * quantity
        exit_value = exit_price * quantity
        trade_pnl = exit_value - entry_value - fill.fees - broker.fee_per_order
        net_pnl += trade_pnl
        if trade_pnl > 0:
            gross_profit += trade_pnl
        else:
            gross_loss += trade_pnl
        trades += 1
        if trade_pnl > 0:
            wins += 1
        else:
            losses += 1

        broker.cash = round(broker.cash + exit_value - broker.fee_per_order, 2)
        broker.positions.pop(symbol, None)
        entry_blocked_until = exit_index

    closes = daily_frame["close"].astype(float).reset_index(drop=True).values
    equity_curve = _build_equity_curve(closes, trade_records, 10_000.0)

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "net_pnl": round(net_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "equity_curve": equity_curve,
        **diagnostics(
            trades=trades,
            wins=wins,
            losses=losses,
            net_pnl=net_pnl,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
        ),
    }


def _filter_frame_by_date(frame: Any, start: str | None, end: str | None) -> Any:
    if frame.empty or "timestamp" not in frame.columns:
        return frame

    timestamp_series = pd.to_datetime(frame["timestamp"], utc=True)
    if timestamp_series.isna().all():
        return frame

    filtered = frame.copy()
    filtered["_ts"] = timestamp_series
    timestamp_tz = getattr(filtered["_ts"].dt, "tz", None)
    if start is not None:
        start_dt = datetime.combine(date.fromisoformat(start), datetime.min.time())
        if timestamp_tz is not None:
            start_dt = start_dt.replace(tzinfo=timestamp_tz)
        filtered = filtered[filtered["_ts"] >= start_dt]
    if end is not None:
        end_dt = datetime.combine(date.fromisoformat(end), datetime.min.time()) + timedelta(days=1)
        if timestamp_tz is not None:
            end_dt = end_dt.replace(tzinfo=timestamp_tz)
        filtered = filtered[filtered["_ts"] < end_dt]
    return filtered.drop(columns=["_ts"]).reset_index(drop=True)


def _resolve_exit(signal, intraday_frame: Any, entry_index: int) -> tuple[float, int]:
    """Resolve exit price/index from bars after the entry bar.

    Looks at the full ``intraday_frame`` starting from ``entry_index + 1``.
    This introduces look-ahead bias (the backtest sees future bars) because
    refactoring to a true event-driven model is a larger change.  The
    backtest is intended for rough estimation, not precise real-time
    simulation.
    """
    after = intraday_frame.iloc[entry_index + 1 :]
    if after.empty:
        # No future bars to check; exit at entry bar close.
        return float(intraday_frame.iloc[entry_index]["close"]), entry_index
    for row_index, (_, row) in enumerate(after.iterrows(), start=entry_index + 1):
        if float(row["low"]) <= signal.stop_loss:
            return signal.stop_loss, row_index
        if float(row["high"]) >= signal.profit_target:
            return signal.profit_target, row_index

    last_idx = entry_index + len(after) - 1
    return float(after.iloc[-1]["close"]), last_idx


def _fetch_rl_frames(
    meta_symbols: list[str],
    settings: Settings,
    start: str | None,
    end: str | None,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Fetch daily + intraday data for every symbol the RL model was trained on.

    Falls back from 5m -> 1h -> daily bars when intraday is unavailable.
    """
    daily_frames: dict[str, pd.DataFrame] = {}
    intraday_frames: dict[str, pd.DataFrame] = {}
    for sym in (value.strip() for value in meta_symbols if value.strip()):
        daily_frames[sym] = _fetch_bars_compat(
            market_data.fetch_bars,
            sym,
            settings.market_data.daily_period,
            "1d",
            start=start,
            end=end,
            settings=settings.market_data,
        )
        try:
            intraday_frames[sym] = _fetch_bars_compat(
                market_data.fetch_bars,
                sym,
                settings.market_data.intraday_period,
                settings.market_data.intraday_interval,
                start=start,
                end=end,
                settings=settings.market_data,
            )
        except ValueError:
            try:
                intraday_frames[sym] = _fetch_bars_compat(
                    market_data.fetch_bars,
                    sym, "1y", "1h", start=start, end=end,
                    settings=settings.market_data,
                )
                logger.info("Note: Using 1h data for %s (5m unavailable for date range)", sym)
            except ValueError:
                intraday_frames[sym] = daily_frames[sym].copy()
                logger.info("Note: Using daily bars for %s (intraday unavailable)", sym)
        intraday_frames[sym] = _filter_frame_by_date(
            intraday_frames[sym], start=start, end=end
        )
    return daily_frames, intraday_frames


def _write_rl_summary(
    settings: Settings,
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    write_snapshot(
        settings.app.backtest_summary_path,
        {
            "mode": "backtest",
            "strategy": "rl",
            "summary": {
                k: summary[k]
                for k in ("trades", "wins", "losses", "win_rate", "net_pnl")
                if k in summary
            },
            "rows": rows,
        },
    )


def run_walk_forward(
    symbols: list[str],
    settings: Settings,
    start: str | None = None,
    end: str | None = None,
    windows: int = 10,
) -> dict[str, object]:
    """Walk-forward backtest: split the date range into *windows* sequential
    windows, run an independent backtest on each, and aggregate the out-of-
    sample results.

    For rule-based strategies this serves as **regime-stability analysis**.
    Consistent performance across every window = robust strategy.
    High variance = fragile or overfit to a specific market regime.

    Returns a dict like ``run_backtest`` but with an extra *windows* key
    containing per-window breakdown.
    """
    if start is None:
        start = (date.today() - timedelta(days=365)).isoformat()
    if end is None:
        end = date.today().isoformat()

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    total_days = (end_d - start_d).days
    if total_days < 1:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0, "windows": []}

    if total_days < windows * 20:
        windows = max(1, total_days // 20)

    window_size = max(1, total_days // windows)
    all_trades = 0
    all_wins = 0
    all_losses = 0
    all_net_pnl = 0.0
    window_results: list[dict[str, object]] = []

    for w in range(windows):
        ws = start_d + timedelta(days=w * window_size)
        we = ws + timedelta(days=window_size) if w < windows - 1 else end_d

        result = run_backtest(symbols, settings, start=ws.isoformat(), end=we.isoformat())
        t = result.get("trades", 0)
        w_cnt = result.get("wins", 0)
        l_cnt = result.get("losses", 0)
        pnl = result.get("net_pnl", 0.0)
        if not isinstance(t, (int, float)):
            t = 0
        if not isinstance(w_cnt, (int, float)):
            w_cnt = 0
        if not isinstance(l_cnt, (int, float)):
            l_cnt = 0
        if not isinstance(pnl, (int, float)):
            pnl = 0.0
        t_i, w_i, l_i = int(t), int(w_cnt), int(l_cnt)
        pnl_f = float(pnl)

        all_trades += t_i
        all_wins += w_i
        all_losses += l_i
        all_net_pnl += pnl_f

        window_results.append({
            "window": w + 1,
            "start": ws.isoformat(),
            "end": we.isoformat(),
            "trades": t_i,
            "wins": w_i,
            "losses": l_i,
            "win_rate": 0.0 if t_i == 0 else w_i / t_i,
            "net_pnl": round(pnl_f, 2),
        })

    return {
        "trades": all_trades,
        "wins": all_wins,
        "losses": all_losses,
        "win_rate": 0.0 if all_trades == 0 else all_wins / all_trades,
        "net_pnl": round(all_net_pnl, 2),
        "windows": window_results,
    }


def run_rl_walk_forward(
    symbols: list[str],
    settings: Settings,
    *,
    start: str | None = None,
    end: str | None = None,
    windows: int = 5,
    model_path: str | None = None,
) -> dict[str, object]:
    """Run fixed-model sequential benchmark windows.

    This is segmented evaluation, not retrain-per-window walk-forward.
    """
    if start is None:
        start = (date.today() - timedelta(days=365)).isoformat()
    if end is None:
        end = date.today().isoformat()

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    total_days = (end_d - start_d).days
    if total_days < 1:
        return {"windows": [], "results": {}}

    if total_days < windows * 20:
        windows = max(1, total_days // 20)
    window_size = max(1, total_days // windows)

    strategy_totals: dict[str, dict[str, float]] = {}
    window_results: list[dict[str, object]] = []

    for window_index in range(windows):
        ws = start_d + timedelta(days=window_index * window_size)
        we = ws + timedelta(days=window_size) if window_index < windows - 1 else end_d
        comparison = run_strategy_comparison(
            symbols,
            settings,
            start=ws.isoformat(),
            end=we.isoformat(),
            strategies=["v2.5", "v3", "rl"],
            model_path=model_path,
        )
        strategy_results = comparison["results"]
        window_results.append(
            {
                "window": window_index + 1,
                "start": ws.isoformat(),
                "end": we.isoformat(),
                "results": strategy_results,
                "best_pnl_strategy": comparison["best_pnl_strategy"],
                "best_winrate_strategy": comparison["best_winrate_strategy"],
            }
        )
        for strategy_name, result in strategy_results.items():
            totals = strategy_totals.setdefault(
                strategy_name,
                {
                    "trades": 0.0,
                    "wins": 0.0,
                    "losses": 0.0,
                    "net_pnl": 0.0,
                    "gross_profit": 0.0,
                    "gross_loss": 0.0,
                },
            )
            totals["trades"] += float(result["trades"])
            totals["wins"] += float(result["wins"])
            totals["losses"] += float(result["losses"])
            totals["net_pnl"] += float(result["net_pnl"])
            totals["gross_profit"] += float(result.get("gross_profit", 0.0))
            totals["gross_loss"] += float(result.get("gross_loss", 0.0))

    aggregate_results = {
        strategy_name: {
            "trades": int(values["trades"]),
            "wins": int(values["wins"]),
            "losses": int(values["losses"]),
            "win_rate": 0.0 if values["trades"] == 0 else values["wins"] / values["trades"],
            "net_pnl": round(values["net_pnl"], 2),
            **diagnostics(
                trades=int(values["trades"]),
                wins=int(values["wins"]),
                losses=int(values["losses"]),
                net_pnl=values["net_pnl"],
                gross_profit=values["gross_profit"],
                gross_loss=values["gross_loss"],
            ),
        }
        for strategy_name, values in strategy_totals.items()
    }
    return {"windows": window_results, "results": aggregate_results}


def run_rl_backtest(
    symbols: list[str],
    settings: Settings,
    start: str | None = None,
    end: str | None = None,
    model_path: str | None = None,
) -> dict[str, float | int | list[dict[str, float | int | str | None]]]:
    """Run backtest using RL agent inference on pre-loaded market data.

    Requires a trained RL model. The agent predicts actions (buy/sell/hold)
    for each bar based on technical indicators and portfolio state.

    Args:
        symbols: List of ticker symbols to backtest
        settings: Application settings
        start: Inclusive start date (YYYY-MM-DD)
        end: Inclusive end date (YYYY-MM-DD)
        model_path: Path to trained RL model (uses config if None)

    Returns:
        Summary dict with trades, wins, losses, net_pnl, rows
    """
    from trading_bot.rl.backtest import RLBacktestConfig, RLBacktestRunner

    trades = 0
    wins = 0
    losses = 0
    net_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    rows: list[dict[str, float | int | str | None]] = []
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"

    # Load model metadata to determine training symbol order and max_symbols
    resolved_model_path = model_path or (getattr(settings.rl, "model_path", None) if hasattr(settings, "rl") else None)
    meta_symbols = list(symbols)
    meta_max_symbols: int | None = None
    meta_action_scheme = "bsh"
    if resolved_model_path:
        import json
        meta_path = Path(resolved_model_path).with_suffix(".zip")
        if meta_path.suffix != ".zip":
            meta_path = Path(resolved_model_path)
        meta_path = meta_path.parent / (meta_path.stem + "_meta.json")
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta_symbols = [s.strip().upper() for s in meta.get("symbols", symbols)]
                meta_max_symbols = meta.get("max_symbols")
                meta_action_scheme = str(meta.get("action_scheme", meta_action_scheme))
            except Exception as e:
                logger.debug("Backtest error: %s", e)

    rl_config = RLBacktestConfig(
        model_path=resolved_model_path,
        symbols=meta_symbols,
        starting_cash=settings.rl.backtest_starting_cash,
        fee_per_order=settings.paper.fee_per_order,
        slippage_bps=settings.paper.slippage_bps,
        max_shares=settings.rl.backtest_max_shares,
        max_symbols=meta_max_symbols,
        stop_loss_pct=settings.rl.backtest_stop_loss_pct,
        profit_target_pct=settings.rl.backtest_profit_target_pct,
        action_scheme=meta_action_scheme,
    )

    runner = RLBacktestRunner(config=rl_config)

    if rl_config.model_path:
        try:
            runner.load_model()
            model_obs_space = getattr(runner._model, "observation_space", None)
            if model_obs_space is not None and hasattr(model_obs_space, "shape") and len(model_obs_space.shape) >= 2:
                expected_features = model_obs_space.shape[1]
                from trading_bot.rl.features import CROSS_SYMBOL_FEATURES
                n_market_features = len(RLBacktestRunner.FEATURE_COLS) + len(CROSS_SYMBOL_FEATURES)
                n_portfolio_features = 5
                expected_max = (expected_features - n_portfolio_features) // n_market_features
                actual_max = rl_config.max_symbols or len(rl_config.symbols)
                if rl_config.max_symbols is None and expected_max >= len(rl_config.symbols):
                    rl_config.max_symbols = expected_max
                    actual_max = expected_max
                if expected_features != actual_max * n_market_features + n_portfolio_features:
                    logger.warning("RL model expects %s features (%s max symbols) but backtest uses %s max symbols. Skipping RL backtest.", expected_features, expected_max, actual_max)
                    return {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "win_rate": 0.0, "rows": rows, "rl_actions": []}
        except Exception as e:
            logger.warning("Failed to load RL model: %s. Skipping RL backtest.", e)
            return {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "win_rate": 0.0, "rows": rows, "rl_actions": []}
    else:
        logger.warning("No RL model path specified. Setting to dummy for testing.")
        runner.set_model(None)

    daily_frames, intraday_frames = _fetch_rl_frames(
        meta_symbols, settings, start, end
    )

    primary = next(
        (
            symbol
            for symbol in (value.strip() for value in meta_symbols if value.strip())
            if intraday_frames.get(symbol) is not None and len(intraday_frames[symbol]) >= 15
        ),
        None,
    )
    if primary is not None:
        result = runner.run_backtest(
            symbol=primary,
            daily_frames=daily_frames,
            intraday_frames=intraday_frames,
            trade_symbols=symbols,
        )
        trades = result["trades"]
        wins = result["wins"]
        losses = result["losses"]
        net_pnl = result["net_pnl"]
        gross_profit = float(result.get("gross_profit", 0.0))
        gross_loss = float(result.get("gross_loss", 0.0))
        rows.append({
            "ticker": ",".join(meta_symbols),
            "trades": result["trades"],
            "wins": result["wins"],
            "losses": result["losses"],
            "net_pnl": result["net_pnl"],
            "start": start,
            "end": end,
            "strategy": "rl",
        })
        append_decision_event(
            log_path,
            {
                "command": "backtest",
                "ticker": ",".join(meta_symbols),
                "strategy": "rl",
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
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "rows": rows,
        "strategy": "rl",
    }
    _write_rl_summary(settings, summary, rows)
    return summary


def run_strategy_comparison(
    symbols: list[str],
    settings: Settings,
    start: str | None = None,
    end: str | None = None,
    strategies: list[str] | None = None,
    model_path: str | None = None,
) -> dict[str, Any]:
    """Run backtest across multiple strategies and compare results.

    Compares rule-based strategies (v2.5, v3) against RL agent inference.

    Args:
        symbols: List of ticker symbols to backtest
        settings: Application settings
        start: Inclusive start date (YYYY-MM-DD)
        end: Inclusive end date (YYYY-MM-DD)
        strategies: List of strategies to compare (default: all available)

    Returns:
        Dict with per-strategy results and aggregated comparison
    """
    if strategies is None:
        strategies = []
        if getattr(settings, "strategy", None) is not None and settings.strategy.use_v3_signals:
            strategies.append("v3")
        strategies.append("v2.5")
        if getattr(settings, "rl", None) is not None and settings.rl.enabled:
            strategies.append("rl")
        if not strategies:
            strategies = ["v2.5"]

    results: dict[str, dict[str, Any]] = {}

    for strategy in strategies:
        logger.info("Running %s backtest...", strategy)
        if strategy == "rl":
            result = run_rl_backtest(symbols, settings, start=start, end=end, model_path=model_path)
        else:
            import copy
            strategy_settings = copy.deepcopy(settings)
            if getattr(strategy_settings, "strategy", None) is not None:
                strategy_settings.strategy.use_v3_signals = strategy == "v3"
            result = run_backtest(symbols, strategy_settings, start=start, end=end)
            result["strategy"] = strategy
        attach_diagnostics(result)
        results[strategy] = result

    comparison = {
        "symbols": symbols,
        "start": start,
        "end": end,
        "strategies": strategies,
        "results": results,
        "summary": {},
    }

    for strategy, result in results.items():
        comparison["summary"][strategy] = {
            "trades": result.get("trades", 0),
            "wins": result.get("wins", 0),
            "losses": result.get("losses", 0),
            "win_rate": result.get("win_rate", 0.0),
            "net_pnl": result.get("net_pnl", 0.0),
            "avg_win": result.get("avg_win", 0.0),
            "avg_loss": result.get("avg_loss", 0.0),
            "expectancy": result.get("expectancy", 0.0),
            "profit_factor": result.get("profit_factor", 0.0),
            "pnl_per_trade": result.get("pnl_per_trade", 0.0),
        }

    best_pnl = max(comparison["summary"].items(), key=lambda x: x[1]["net_pnl"])
    best_winrate = max(comparison["summary"].items(), key=lambda x: x[1]["win_rate"])
    comparison["best_pnl_strategy"] = best_pnl[0]
    comparison["best_winrate_strategy"] = best_winrate[0]

    return comparison
