from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_bot.config.settings import Settings
from trading_bot.data import market_data
from trading_bot.data.indicators import add_atr, add_bollinger_bands, add_ema, add_rsi, add_sma
from trading_bot.execution.order_manager import submit_signal_as_order
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.runtime.decision_log import append_decision_event
from trading_bot.runtime.snapshots import write_snapshot
from trading_bot.strategy.daily_signal_engine import generate_daily_signal
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
            start=start,
            end=end,
        )
        try:
            intraday_frame = market_data.fetch_bars(
                symbol,
                settings.market_data.intraday_period,
                settings.market_data.intraday_interval,
                start=start,
                end=end,
            )
        except ValueError:
            # Try 1h data (730 days available)
            try:
                intraday_frame = market_data.fetch_bars(
                    symbol, "1y", "1h", start=start, end=end
                )
                print(f"Note: Using 1h data for {symbol} (5m unavailable for date range)")
            except ValueError:
                # Fall back to daily bars as "intraday"
                intraday_frame = daily_frame.copy()
                print(f"Note: Using daily bars for {symbol} (intraday unavailable)")
        
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
        return {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}

    broker = PaperBroker(starting_cash=10_000.0, fee_per_order=1.0, slippage_bps=0)
    trades = 0
    wins = 0
    losses = 0
    net_pnl = 0.0
    entry_blocked_until = -1

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


def _run_symbol_backtest_daily(
    symbol: str,
    daily_frame: Any,
    settings: Settings,
) -> dict[str, float | int]:
    """Run backtest using daily bar signals (no intraday data needed)."""
    if len(daily_frame) < 50:  # Need warmup for EMA/SMA
        return {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}

    broker = PaperBroker(starting_cash=10_000.0, fee_per_order=1.0, slippage_bps=0)
    trades = 0
    wins = 0
    losses = 0
    net_pnl = 0.0
    entry_blocked_until = -1

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
    rows: list[dict[str, float | int | str | None]] = []
    log_path = Path(settings.app.log_dir) / "decision-log.jsonl"

    rl_config = RLBacktestConfig(
        model_path=model_path or getattr(settings.rl, "model_path", None) if hasattr(settings, "rl") else None,
        symbols=symbols,
        starting_cash=10_000.0,
        fee_per_order=settings.paper.fee_per_order,
        slippage_bps=settings.paper.slippage_bps,
    )

    runner = RLBacktestRunner(config=rl_config)

    if rl_config.model_path:
        runner.load_model()
    else:
        print("Warning: No RL model path specified. Setting to dummy for testing.")
        runner.set_model(None)

    for symbol in (value.strip() for value in symbols if value.strip()):
        daily_frame = market_data.fetch_bars(
            symbol,
            settings.market_data.daily_period,
            "1d",
            start=start,
            end=end,
        )
        try:
            intraday_frame = market_data.fetch_bars(
                symbol,
                settings.market_data.intraday_period,
                settings.market_data.intraday_interval,
                start=start,
                end=end,
            )
        except ValueError:
            try:
                intraday_frame = market_data.fetch_bars(
                    symbol, "1y", "1h", start=start, end=end
                )
                print(f"Note: Using 1h data for {symbol} (5m unavailable for date range)")
            except ValueError:
                intraday_frame = daily_frame.copy()
                print(f"Note: Using daily bars for {symbol} (intraday unavailable)")

        intraday_frame = _filter_frame_by_date(intraday_frame, start=start, end=end)

        if len(intraday_frame) < 15:
            continue

        result = runner.run_backtest(symbol, daily_frame, intraday_frame)
        trades += result["trades"]
        wins += result["wins"]
        losses += result["losses"]
        net_pnl += result["net_pnl"]
        rows.append({
            "ticker": symbol,
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
                "ticker": symbol,
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
        "rows": rows,
        "strategy": "rl",
    }
    write_snapshot(
        settings.app.backtest_summary_path,
        {
            "mode": "backtest",
            "strategy": "rl",
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


def run_strategy_comparison(
    symbols: list[str],
    settings: Settings,
    start: str | None = None,
    end: str | None = None,
    strategies: list[str] | None = None,
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
        print(f"Running {strategy} backtest...")
        if strategy == "rl":
            result = run_rl_backtest(symbols, settings, start=start, end=end)
        else:
            result = run_backtest(symbols, settings, start=start, end=end)
            result["strategy"] = strategy
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
        }

    best_pnl = max(comparison["summary"].items(), key=lambda x: x[1]["net_pnl"])
    best_winrate = max(comparison["summary"].items(), key=lambda x: x[1]["win_rate"])
    comparison["best_pnl_strategy"] = best_pnl[0]
    comparison["best_winrate_strategy"] = best_winrate[0]

    return comparison
