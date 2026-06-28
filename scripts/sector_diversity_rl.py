#!/usr/bin/env python3
"""Train + backtest PPO models on sector-diverse symbols.

Symbols: XOM (energy), CVX (energy), UNH (healthcare), LLY (healthcare),
         CAT (industrials), DE (industrials)

Usage:
    .venv/bin/python scripts/sector_diversity_rl.py
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

from trading_bot.rl.agent import RLAgent, RLAgentConfig
from trading_bot.rl.env import TradingConfig
from trading_bot.rl.trainer import TrainingConfig
from trading_bot.rl.backtest import RLBacktestRunner, RLBacktestConfig

SYMBOLS = ["XOM", "CVX", "UNH", "LLY", "CAT", "DE"]
TIMESTEPS = 300_000
SEEDS = [42, 123, 789]
TRAIN_END_DATE = "2025-06-24"
BACKTEST_START = "2025-06-25"
BACKTEST_END = "2026-06-25"
OUTPUT_DIR = Path("state/rl_logs/sector_diversity")
RESULTS_DIR = Path("state/rl_logs/sector_diversity/results")
STARTING_CASH = 100_000.0


def _parse_seeds(raw: str) -> list[int]:
    return [int(seed.strip()) for seed in raw.split(",") if seed.strip()]


def _model_path(seed: int) -> Path:
    return OUTPUT_DIR / f"PPO_seed_{seed}"


def _confidence_verdict(result: dict, starting_cash: float = STARTING_CASH) -> str:
    trades = int(result.get("trades", 0))
    net_pnl = float(result.get("net_pnl", 0.0))
    profit_factor = float(result.get("profit_factor", 0.0))
    checks = {
        "trades>=10": trades >= 10,
        "return>=5pct": net_pnl >= starting_cash * 0.05,
        "profit_factor>=1.20": profit_factor >= 1.20,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return "PASS" if not failed else f"FAIL failed={','.join(failed)}"


def train_one(seed: int, timesteps: int, verbose: int) -> Path:
    tag = f"seed_{seed}"
    print(f"\n{'='*60}")
    print(f"  Training seed {seed}: {SYMBOLS}")
    print(f"  Timesteps: {timesteps:,}")
    print(f"  Train end: {TRAIN_END_DATE}")
    print(f"{'='*60}\n", flush=True)

    env_config = TradingConfig(
        symbols=SYMBOLS,
        bar_period="2y",
        bar_interval="1d",
        data_end_date=TRAIN_END_DATE,
        observer_window=10,
        starting_cash=STARTING_CASH,
        fee_per_order=1.0,
        slippage_bps=5,
        max_positions=10,
        max_episode_steps=500,
        reward_scheme="risk_adjusted",
        action_scheme="proportion",
    )

    training_config = TrainingConfig(
        env_config=env_config,
        model_type="PPO",
        total_timesteps=timesteps,
        learning_rate=3e-4,
        n_epochs=10,
        batch_size=64,
        n_steps=128,
        gamma=0.995,
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=0.01,
        vf_coef=0.5,
        max_grad_norm=0.5,
        verbose=verbose,
        log_dir=str(OUTPUT_DIR),
        eval_freq=5000,
        checkpoint_freq=10000,
        seed=seed,
    )

    agent_config = RLAgentConfig(
        enabled=True,
        env_config=env_config,
        training=training_config,
        prediction_mode="deterministic",
    )

    agent = RLAgent(config=agent_config)
    agent.train()

    model_path = _model_path(seed)
    agent.save(model_path)

    meta = {
        "symbols": SYMBOLS,
        "agent": "PPO",
        "seed": seed,
        "ent_coef": 0.01,
        "gamma": 0.995,
        "total_timesteps": timesteps,
        "reward_scheme": "risk_adjusted",
        "action_scheme": "proportion",
        "train_end_date": TRAIN_END_DATE,
        "trained_at": datetime.now().isoformat(),
    }
    meta_path = OUTPUT_DIR / f"PPO_{tag}_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"[Seed {seed}] Model saved: {model_path}.zip", flush=True)
    return model_path


def backtest_model(model_path: Path, seed: int) -> dict:
    print(f"\n{'='*60}")
    print(f"  Backtesting seed {seed}: {model_path}")
    print(f"{'='*60}\n", flush=True)

    from trading_bot.data import market_data

    frames = {}
    for sym in SYMBOLS:
        print(f"  Fetching data for {sym}...", flush=True)
        df = market_data.fetch_bars(
            sym,
            period="1y",
            interval="1d",
            start=BACKTEST_START,
            end=BACKTEST_END,
        )
        if df is not None and not df.empty:
            frames[sym] = df
            print(f"  {sym}: {len(df)} bars", flush=True)
        else:
            print(f"  WARNING: No data for {sym}", flush=True)

    if not frames:
        print("  No data loaded, skipping backtest", flush=True)
        return {"seed": seed, "error": "no data"}

    config = RLBacktestConfig(
        model_path=str(model_path.with_suffix(".zip")),
        symbols=list(frames.keys()),
        starting_cash=STARTING_CASH,
        fee_per_order=1.0,
        slippage_bps=5,
        use_intraday_exit=False,
        stop_loss_pct=0.05,
        profit_target_pct=0.08,
        action_scheme="proportion",
    )

    runner = RLBacktestRunner(config=config)
    runner.load_model()

    result = runner.run_backtest(
        daily_frames=frames,
        starting_cash=STARTING_CASH,
        trade_symbols=list(frames.keys()),
    )

    result["seed"] = seed
    result["model"] = str(model_path)
    result["symbols"] = list(frames.keys())
    result["backtested_at"] = datetime.now().isoformat()

    print(f"\n  Results (seed {seed}):", flush=True)
    print(f"    Trades:    {result['trades']}", flush=True)
    print(f"    Wins:      {result['wins']}", flush=True)
    print(f"    Win Rate:  {result['win_rate']:.0%}", flush=True)
    print(f"    Net PnL:   ${result['net_pnl']:.2f}", flush=True)
    print(f"    Confidence: {_confidence_verdict(result)}", flush=True)

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default=",".join(str(seed) for seed in SEEDS))
    parser.add_argument("--timesteps", type=int, default=TIMESTEPS)
    parser.add_argument("--verbose", type=int, choices=[0, 1, 2], default=1)
    parser.add_argument("--evaluate-only", action="store_true", help="Backtest existing seed models without training")
    args = parser.parse_args()
    seeds = _parse_seeds(args.seeds)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Sector diversity training: {SYMBOLS}")
    print(f"Seeds: {seeds}, Timesteps: {args.timesteps:,} each")
    print(f"Training data ends: {TRAIN_END_DATE}")
    print(f"Backtest window: {BACKTEST_START} to {BACKTEST_END}\n", flush=True)

    all_results = []

    for seed in seeds:
        try:
            model_path = _model_path(seed) if args.evaluate_only else train_one(seed, args.timesteps, args.verbose)
            if args.evaluate_only and not model_path.with_suffix(".zip").exists():
                raise FileNotFoundError(f"missing model: {model_path.with_suffix('.zip')}")
            result = backtest_model(model_path, seed)
            all_results.append(result)

            result_file = RESULTS_DIR / f"backtest_seed_{seed}.json"
            result_file.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
            print(f"  Saved: {result_file}", flush=True)

        except Exception as exc:
            print(f"\n[Seed {seed}] FAILED: {exc}", flush=True)
            import traceback
            traceback.print_exc()
            all_results.append({"seed": seed, "error": str(exc)})

    summary_file = RESULTS_DIR / "summary.json"
    summary = {
        "symbols": SYMBOLS,
        "seeds": seeds,
        "timesteps": args.timesteps,
        "train_end_date": TRAIN_END_DATE,
        "backtest_start": BACKTEST_START,
        "backtest_end": BACKTEST_END,
        "results": all_results,
        "generated_at": datetime.now().isoformat(),
    }
    summary_file.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    for r in all_results:
        if "error" in r:
            print(f"  Seed {r['seed']}: FAILED - {r['error']}")
        else:
            print(
                f"  Seed {r['seed']}: trades={r['trades']} WR={r['win_rate']:.0%} "
                f"PnL=${r['net_pnl']:.2f} {_confidence_verdict(r)}"
            )
    print(f"\n  Results saved to: {RESULTS_DIR}")
    print(f"{'='*60}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
