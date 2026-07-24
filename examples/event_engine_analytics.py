"""Executable Stage 5 analytics example using synthetic backtest output.

Run from the repository root:

    .venv/bin/python -m examples.event_engine_analytics --output-dir artifacts/demo
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from event_engine.analytics import (
    CombinatorialPurgedCV,
    DSRDiagnostics,
    PerformanceAnalytics,
    export_equity_curve_html,
    generate_markdown_summary,
)


def synthetic_run(seed: int = 42) -> tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2022-01-03", periods=756, freq="B", tz="UTC")
    strategy_returns = pd.DataFrame(
        {
            "selected": rng.normal(0.00045, 0.009, len(index)),
            "trial_2": rng.normal(0.00020, 0.010, len(index)),
            "trial_3": rng.normal(0.00005, 0.011, len(index)),
            "trial_4": rng.normal(-0.00005, 0.010, len(index)),
        },
        index=index,
    )
    equity = pd.Series(
        100_000.0 * np.cumprod(1.0 + strategy_returns["selected"]),
        index=index,
        name="equity",
    )
    trade_r = rng.normal(0.25, 1.0, 180)
    trades = pd.DataFrame(
        {
            "pnl": trade_r * 500.0,
            "initial_risk": np.full(len(trade_r), 500.0),
        }
    )
    return equity, trades, strategy_returns


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/analytics"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    equity, trades, trial_returns = synthetic_run()
    analytics = PerformanceAnalytics(equity, trades)
    trial_sharpes = trial_returns.apply(
        lambda values: values.mean() / values.std(ddof=1)
    ).to_numpy()
    dsr = DSRDiagnostics.deflated_sharpe_ratio(
        equity.pct_change().dropna(),
        trial_sharpes=trial_sharpes,
        n_trials=200,
    )
    event_ends = pd.Series(equity.index + pd.Timedelta(days=1), index=equity.index)
    cpcv = CombinatorialPurgedCV(
        n_groups=6,
        n_test_groups=2,
        embargo_pct=0.05,
    ).evaluate(trial_returns, event_ends=event_ends)

    markdown = generate_markdown_summary(
        analytics,
        dsr_result=dsr,
        cpcv_result=cpcv,
    )
    markdown_path = args.output_dir / "performance-summary.md"
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path = export_equity_curve_html(
        equity,
        args.output_dir / "equity-curve.html",
        title="Synthetic Event-Engine Validation",
    )
    print(markdown)
    print(f"Wrote {markdown_path} and {html_path}")


if __name__ == "__main__":
    main()
