#!/usr/bin/env python3
"""Compare all existing RL models on the same OOS test data.

Evaluates every model in state/rl_logs/ against 6 months of fresh data
and ranks them by performance.

Usage:
    .venv/bin/python scripts/compare_models.py
"""
import sys
import json
from pathlib import Path
from datetime import datetime

from trading_bot.rl.backtest import RLBacktestRunner, RLBacktestConfig

RL_LOGS_DIR = Path("state/rl_logs")
RESULTS_DIR = Path("state/rl_logs/comparison_results")
TEST_PERIOD = "6mo"
TEST_INTERVAL = "1d"
STARTING_CASH = 100_000.0


def find_all_models() -> list[tuple[str, Path, dict]]:
    models = []
    for meta_file in sorted(RL_LOGS_DIR.rglob("*_meta.json")):
        model_name = meta_file.stem.replace("_meta", "")
        model_dir = meta_file.parent
        model_path = model_dir / f"{model_name}.zip"

        if not model_path.exists():
            continue

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        rel_path = meta_file.relative_to(RL_LOGS_DIR)
        label = str(rel_path.parent / model_name)

        models.append((label, model_path, meta))

    return models


def evaluate_model(label: str, model_path: Path, meta: dict) -> dict:
    symbols = meta.get("symbols", [])
    if not symbols:
        return {"label": label, "error": "no symbols in metadata"}

    print(f"\n  Evaluating: {label}", flush=True)
    print(f"    Symbols: {symbols}", flush=True)

    from trading_bot.data import market_data

    frames = {}
    for sym in symbols:
        df = market_data.fetch_bars(sym, period=TEST_PERIOD, interval=TEST_INTERVAL)
        if df is not None and not df.empty:
            frames[sym] = df
            print(f"    {sym}: {len(df)} bars", flush=True)
        else:
            print(f"    WARNING: No data for {sym}", flush=True)

    if not frames:
        return {"label": label, "meta": meta, "error": "no data fetched"}

    available_symbols = list(frames.keys())
    action_scheme = meta.get("action_scheme", "bsh")

    config = RLBacktestConfig(
        model_path=str(model_path),
        symbols=available_symbols,
        starting_cash=STARTING_CASH,
        fee_per_order=1.0,
        slippage_bps=5,
        use_intraday_exit=False,
        stop_loss_pct=0.05,
        profit_target_pct=0.08,
        action_scheme=action_scheme,
    )

    try:
        runner = RLBacktestRunner(config=config)
        runner.load_model()

        result = runner.run_backtest(
            daily_frames=frames,
            starting_cash=STARTING_CASH,
            trade_symbols=available_symbols,
        )
    except Exception as e:
        return {"label": label, "meta": meta, "error": str(e)}

    result["label"] = label
    result["meta"] = meta
    result["test_symbols"] = available_symbols
    result["test_period"] = TEST_PERIOD
    result["evaluated_at"] = datetime.now().isoformat()

    print(f"    Trades: {result['trades']} | WR: {result['win_rate']:.0%} | PnL: ${result['net_pnl']:.2f}", flush=True)

    return result


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    models = find_all_models()
    print(f"Found {len(models)} models to evaluate\n", flush=True)

    if not models:
        print("No models found in state/rl_logs/", flush=True)
        return 1

    all_results = []

    for label, model_path, meta in models:
        try:
            result = evaluate_model(label, model_path, meta)
            all_results.append(result)

            safe_name = label.replace("/", "_").replace(" ", "_")
            result_file = RESULTS_DIR / f"{safe_name}.json"
            result_file.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

        except Exception as e:
            print(f"  FAILED {label}: {e}", flush=True)
            import traceback
            traceback.print_exc()
            all_results.append({"label": label, "meta": meta, "error": str(e)})

    summary_file = RESULTS_DIR / "comparison_summary.json"
    summary = {
        "total_models": len(all_results),
        "test_period": TEST_PERIOD,
        "starting_cash": STARTING_CASH,
        "results": all_results,
        "generated_at": datetime.now().isoformat(),
    }
    summary_file.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    successful = [r for r in all_results if "error" not in r and r.get("trades", 0) > 0]

    print(f"\n{'='*90}")
    print(f"  MODEL COMPARISON RESULTS")
    print(f"{'='*90}")
    print(f"  {'Model':<45} {'Trades':>7} {'Win Rate':>9} {'Net PnL':>12} {'Symbols'}")
    print(f"  {'-'*45} {'-'*7} {'-'*9} {'-'*12} {'-'*20}")

    for r in sorted(successful, key=lambda x: x.get("net_pnl", 0), reverse=True):
        syms = ",".join(r.get("test_symbols", r.get("meta", {}).get("symbols", [])))
        print(f"  {r['label']:<45} {r['trades']:>7} {r['win_rate']:>8.0%} ${r['net_pnl']:>10.2f} {syms}")

    failed = [r for r in all_results if "error" in r]
    if failed:
        print(f"\n  Failed models ({len(failed)}):")
        for r in failed:
            print(f"    {r['label']}: {r['error']}")

    print(f"\n  Results saved to: {RESULTS_DIR}")
    print(f"{'='*90}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
