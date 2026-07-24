from __future__ import annotations

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


def _inputs() -> tuple[pd.Series, pd.DataFrame]:
    index = pd.date_range("2024-01-01", periods=120, freq="B", tz="UTC")
    rng = np.random.default_rng(13)
    returns = rng.normal(0.0008, 0.01, len(index))
    equity = pd.Series(100_000 * np.cumprod(1 + returns), index=index)
    trades = pd.DataFrame(
        {"pnl": [500.0, -200.0, 750.0], "initial_risk": [250.0] * 3}
    )
    return equity, trades


def test_markdown_summary_is_structured_and_discloses_significance() -> None:
    equity, trades = _inputs()
    analytics = PerformanceAnalytics(equity, trades)
    dsr = DSRDiagnostics.deflated_sharpe_ratio(
        equity.pct_change().dropna(),
        trial_sharpes=[-0.1, 0.0, 0.1, 0.2],
        n_trials=20,
    )

    markdown = generate_markdown_summary(analytics, dsr_result=dsr)

    assert "# Quantitative Performance Summary" in markdown
    assert "## Van Tharp System Quality" in markdown
    assert "SQN" in markdown
    assert "## Overfitting Diagnostics" in markdown
    assert "Deflated Sharpe Ratio" in markdown
    assert "p-value" in markdown
    assert "p < 0.05" in markdown


def test_plotly_export_writes_self_contained_equity_and_drawdown_html(
    tmp_path: Path,
) -> None:
    equity, _ = _inputs()
    output = tmp_path / "equity.html"

    returned = export_equity_curve_html(equity, output, title="Synthetic Run")

    assert returned == output
    html = output.read_text(encoding="utf-8")
    assert "plotly" in html.lower()
    assert "Synthetic Run" in html
    assert "Equity" in html
    assert "Drawdown" in html


def test_markdown_can_include_cpcv_pbo() -> None:
    equity, trades = _inputs()
    trial_returns = pd.DataFrame(
        {
            "a": equity.pct_change().fillna(0.0),
            "b": equity.pct_change().fillna(0.0) * -0.5,
        },
        index=equity.index,
    )
    ends = pd.Series(equity.index, index=equity.index)
    cpcv = CombinatorialPurgedCV(4, 2).evaluate(
        trial_returns, event_ends=ends
    )

    markdown = generate_markdown_summary(
        PerformanceAnalytics(equity, trades), cpcv_result=cpcv
    )

    assert "Probability of Backtest Overfitting" in markdown
    assert "PBO" in markdown


def test_stage_five_api_is_exported_from_package_root() -> None:
    from event_engine import (
        CombinatorialPurgedCV as ExportedCPCV,
        DSRDiagnostics as ExportedDSR,
        PerformanceAnalytics as ExportedAnalytics,
    )

    assert ExportedAnalytics is PerformanceAnalytics
    assert ExportedDSR is DSRDiagnostics
    assert ExportedCPCV is CombinatorialPurgedCV
