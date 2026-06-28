#!/usr/bin/env python3
"""Evaluate multi-symbol RL model on OOS data."""
import sys
from pathlib import Path

from trading_bot.data.providers.alpaca_provider import AlpacaProvider
from trading_bot.rl.backtest import RLBacktestRunner, RLBacktestConfig
import pandas as pd

SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA"]
MODEL_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("state/rl_logs/multisymbol/PPO_seed_42.zip")
OOS_START = "2025-06-25"
OOS_END = "2026-06-25"
STARTING_CASH = 10_000.0


def load_data(symbols):
    frames = {}
    provider = AlpacaProvider()
    for sym in symbols:
        df = provider.fetch_bars(sym, "2y", "1d", end=OOS_END)
        if df is None or df.empty:
            print(f"  WARNING: No data for {sym}")
            continue
        if "timestamp" in df.columns:
            df = df.copy()
            df.index = pd.to_datetime(df["timestamp"])
            df = df.drop(columns=["timestamp"])
        df = df[df.index >= OOS_START]
        frames[sym] = df
        print(f"  {sym}: {len(df)} bars, {df.index[0].date()} to {df.index[-1].date()}")
    return frames


def make_runner(symbols):
    config = RLBacktestConfig(
        model_path=str(MODEL_PATH),
        symbols=symbols,
        starting_cash=STARTING_CASH,
        fee_per_order=1.0,
        slippage_bps=0,
        use_intraday_exit=False,
        stop_loss_pct=0.05,
        profit_target_pct=0.08,
    )
    runner = RLBacktestRunner(config=config)
    runner.load_model()
    return runner


def run_walkforward(frames, symbols, n_windows=5):
    all_dates = sorted({d for sym in frames.values() for d in sym.index})
    if len(all_dates) < 60:
        print(f"Not enough data: {len(all_dates)} bars")
        return None

    window_size = len(all_dates) // n_windows
    results_by_window = []
    runner = make_runner(symbols)

    for w in range(n_windows):
        w_start_idx = w * window_size
        w_end_idx = (w + 1) * window_size if w < n_windows - 1 else len(all_dates)

        test_start = all_dates[w_start_idx + 1] if w_start_idx + 1 < len(all_dates) else None
        test_end = all_dates[w_end_idx - 1] if w_end_idx > 0 else None

        if test_start is None or test_end is None:
            continue

        test_frames = {}
        for sym, df in frames.items():
            window_df = df[(df.index >= test_start) & (df.index <= test_end)].copy()
            if not window_df.empty:
                test_frames[sym] = window_df

        if not test_frames:
            continue

        result = runner.run_backtest(
            daily_frames=test_frames,
            starting_cash=STARTING_CASH,
            trade_symbols=symbols,
        )

        results_by_window.append({
            "window": w + 1,
            "start": str(test_start.date()),
            "end": str(test_end.date()),
            "trades": result["trades"],
            "wins": result["wins"],
            "win_rate": result["win_rate"],
            "net_pnl": result["net_pnl"],
        })
        print(f"  Window {w+1} ({test_start.date()} - {test_end.date()}): "
              f"trades={result['trades']} WR={result['win_rate']:.0%} PnL=${result['net_pnl']:.2f}")

    return results_by_window


def main():
    print(f"Loading data for {SYMBOLS} ({OOS_START} to {OOS_END})...")
    frames = load_data(SYMBOLS)
    if not frames:
        print("No data loaded, aborting")
        return 1

    print(f"\nWalk-forward evaluation...")
    results = run_walkforward(frames, SYMBOLS, n_windows=5)

    if results is None:
        return 1

    total_trades = sum(r["trades"] for r in results)
    total_wins = sum(r["wins"] for r in results)
    total_pnl = sum(r["net_pnl"] for r in results)
    avg_wr = total_wins / total_trades if total_trades > 0 else 0

    print(f"\n=== WALK-FORWARD TOTALS ===")
    print(f"  Trades: {total_trades}")
    print(f"  Win Rate: {avg_wr:.0%}")
    print(f"  Net PnL: ${total_pnl:.2f}")

    # Full-period result
    print(f"\n=== FULL PERIOD ({OOS_START} to {OOS_END}) ===")
    runner = make_runner(SYMBOLS)
    full = runner.run_backtest(
        daily_frames=frames,
        starting_cash=STARTING_CASH,
        trade_symbols=SYMBOLS,
    )
    print(f"  Trades: {full['trades']}")
    print(f"  Win Rate: {full['win_rate']:.0%}")
    print(f"  Net PnL: ${full['net_pnl']:.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
