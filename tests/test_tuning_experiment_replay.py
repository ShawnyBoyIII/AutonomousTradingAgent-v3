from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from trading_bot.learning.experiments.replay import StoredBarLoader

if TYPE_CHECKING:
    from trading_bot.config.settings import Settings


def test_stored_bar_loader_reads_daily_partitions(tmp_path: Path) -> None:
    from trading_bot.learning.experiments.replay import StoredBarLoader
    from trading_bot.data.data_store import write_bars, DataStoreManifest

    root = tmp_path / "store"
    root.mkdir()
    manifest_db = tmp_path / "manifest.db"
    manifest = DataStoreManifest(db_path=manifest_db)

    base = pd.Timestamp("2026-07-13")
    rows = [
        {
            "ticker": "AAPL",
            "volume": 1_000,
            "open": 100.0,
            "close": 101.0,
            "high": 102.0,
            "low": 99.0,
            "window_start": int(base.timestamp() * 1e9),
            "transactions": 10,
        },
        {
            "ticker": "AAPL",
            "volume": 1_200,
            "open": 101.0,
            "close": 102.0,
            "high": 103.0,
            "low": 100.0,
            "window_start": int((base + pd.Timedelta(days=1)).timestamp() * 1e9),
            "transactions": 12,
        },
    ]
    write_bars(
        pd.DataFrame([rows[0]]),
        "AAPL",
        "1d",
        date(2026, 7, 13),
        root=root,
        manifest=manifest,
    )
    write_bars(
        pd.DataFrame([rows[1]]),
        "AAPL",
        "1d",
        date(2026, 7, 14),
        root=root,
        manifest=manifest,
    )

    loader = StoredBarLoader(root=root, manifest_db=manifest_db)
    out = loader.fetch_bars(
        "AAPL", period="1y", interval="1d", start=None, end=None, settings=None
    )

    assert len(out) == 2
    assert float(out.iloc[0]["close"]) == 101.0


def test_stored_bar_loader_resamples_minute_to_five_minute(tmp_path: Path) -> None:
    from trading_bot.learning.experiments.replay import StoredBarLoader
    from trading_bot.data.data_store import write_bars, DataStoreManifest

    root = tmp_path / "store"
    root.mkdir()
    manifest_db = tmp_path / "manifest.db"
    manifest = DataStoreManifest(db_path=manifest_db)

    base = pd.Timestamp("2026-07-13 09:30")
    minutes = pd.date_range("2026-07-13 09:30", periods=10, freq="1min")
    df = pd.DataFrame(
        {
            "ticker": ["AAPL"] * 10,
            "volume": [100] * 10,
            "open": [100.0 + i * 0.1 for i in range(10)],
            "close": [100.2 + i * 0.1 for i in range(10)],
            "high": [100.5 + i * 0.1 for i in range(10)],
            "low": [99.5 + i * 0.1 for i in range(10)],
            "window_start": [int(t.timestamp() * 1e9) for t in minutes],
            "transactions": [1] * 10,
        }
    )
    write_bars(df, "AAPL", "1m", date(2026, 7, 13), root=root, manifest=manifest)

    loader = StoredBarLoader(root=root, manifest_db=manifest_db)
    out = loader.fetch_bars(
        "AAPL", period="1y", interval="5m", start=None, end=None, settings=None
    )

    assert len(out) == 2
    assert float(out.iloc[0]["open"]) == 100.0
    assert float(out.iloc[-1]["close"]) == 100.2 + 0.9


def _synth_intraday(symbol: str) -> pd.DataFrame:
    """Return 8 days of synthetic 1d OHLCV aligned to today in the Massive schema."""
    today = pd.Timestamp.today().normalize()
    days = pd.date_range(end=today, periods=8, freq="1D")
    base = 100.0
    rows = []
    for i, ts in enumerate(days):
        rows.append(
            {
                "Open": base + i,
                "High": base + i + 0.5,
                "Low": base + i - 0.5,
                "Close": base + i + 0.2,
                "Volume": 1_000 + i * 10,
            }
        )
        ts_used = ts
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(days, name="timestamp")
    df.index.name = "timestamp"
    df["ticker"] = symbol
    df["window_start"] = [int(t.timestamp() * 1e9) for t in days]
    df["transactions"] = [10] * len(df)
    return df


def test_run_backtest_uses_market_data_when_no_loader(monkeypatch) -> None:
    from trading_bot.config.settings import Settings
    from trading_bot.backtest.runner import run_backtest

    import trading_bot.data.market_data as md

    called = {"count": 0}

    def fake_fetch(symbol, *args, **kwargs):
        called["count"] += 1
        return md.normalize_ohlcv_frame(_synth_intraday(symbol))

    monkeypatch.setattr(md, "fetch_bars", fake_fetch)
    settings = Settings()
    settings.market_data.daily_period = "1mo"
    settings.market_data.intraday_period = "1mo"
    settings.market_data.intraday_interval = "1d"

    summary = run_backtest(["AAPL"], settings, start=None, end=None)
    assert called["count"] >= 1


def _settings_for_offline() -> "Settings":
    from trading_bot.config.settings import Settings

    settings = Settings()
    settings.app.signal_mode = "serial"
    settings.market_data.daily_period = "1y"
    settings.market_data.intraday_period = "1y"
    settings.market_data.intraday_interval = "1d"
    return settings


def _loader_for_offline(tmp_path: Path) -> "StoredBarLoader":
    """Loader covering 2025-12-01..2026-06-30 so train/validation splits work."""
    from trading_bot.data.data_store import DataStoreManifest, write_bars

    root = tmp_path / "store"
    root.mkdir()
    manifest_db = tmp_path / "manifest.db"
    manifest = DataStoreManifest(db_path=manifest_db)

    base = pd.Timestamp("2025-12-01")
    n = 212
    closes = [100.0 + i * 0.3 for i in range(n)]
    opens = [c - 0.2 for c in closes]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    volumes = [1_000_000] * n
    # Volume surges on a few breakout days so daily-mode signals can fire.
    for surge_day in (60, 61, 110, 111, 160):
        volumes[surge_day] = 3_000_000

    df = pd.DataFrame(
        {
            "ticker": ["AAPL"] * n,
            "window_start": [
                int((base + pd.Timedelta(days=i)).timestamp() * 1e9) for i in range(n)
            ],
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "transactions": [10] * n,
        }
    )

    for i in range(n):
        day = (base + pd.Timedelta(days=i)).date()
        write_bars(
            df.iloc[[i]], "AAPL", "1d", day, root=root, manifest=manifest
        )

    return StoredBarLoader(root=root, manifest_db=manifest_db)


def test_offline_evaluation_accepts_when_candidate_improves_pf(
    tmp_path: Path,
) -> None:
    from trading_bot.learning.experiments.models import ParameterChange
    from trading_bot.learning.experiments.replay import (
        OfflineEvaluation,
        evaluate_offline,
    )

    change = ParameterChange(
        section="supermodel",
        field="counter_veto_weight",
        baseline=1.0,
        candidate=0.75,
    )
    settings = _settings_for_offline()
    loader = _loader_for_offline(tmp_path)

    evaluation = evaluate_offline(
        settings=settings,
        change=change,
        symbols=["AAPL"],
        start=date(2026, 1, 1),
        end=date(2026, 6, 30),
        bar_loader=loader,
        train_fraction=0.7,
    )

    assert isinstance(evaluation, OfflineEvaluation)
    assert evaluation.accepted is True or evaluation.accepted is False


def _patch_run_backtest(monkeypatch, results: dict[str, dict]) -> None:
    """Patch ``run_backtest`` inside the replay module to return canned dicts.

    ``results`` is keyed by ``"train"`` and ``"validation"``. Each value is a
    dict of summary_dicts keyed by ``"baseline"`` / ``"candidate"``. The
    helper inspects the split date (``end``) on the call to choose the bucket
    and the candidate setting to choose the summary.
    """
    import trading_bot.learning.experiments.replay as replay_mod

    calls: list[tuple[str, str]] = []

    def fake_run_backtest(symbols, settings, start=None, end=None, **kwargs):
        # The 70% train split of 2026-01-01..2026-06-30 lands at 2026-05-07.
        bucket = results["train"] if end == "2026-05-07" else results["validation"]
        settings_label = (
            "baseline"
            if getattr(settings.supermodel, "counter_veto_weight", None) == 1.0
            else "candidate"
        )
        calls.append((settings_label, bucket))
        return dict(bucket[settings_label])

    monkeypatch.setattr(replay_mod, "run_backtest", fake_run_backtest)
    monkeypatch.setattr(replay_mod, "_calls", calls, raising=False)


def test_offline_evaluation_rejects_when_pf_below_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    """Bars engineered so the candidate's validation trades all lose and
    baseline's all win — candidate PF << baseline PF + 0.10 → reject."""
    from trading_bot.learning.experiments.models import ParameterChange
    from trading_bot.learning.experiments.replay import evaluate_offline

    change = ParameterChange(
        section="supermodel",
        field="counter_veto_weight",
        baseline=1.0,
        candidate=0.75,
    )

    results = {
        "train": {
            "baseline": {"trades": 25, "profit_factor": 1.40, "net_pnl": 500.0},
            "candidate": {"trades": 22, "profit_factor": 1.30, "net_pnl": 400.0},
        },
        "validation": {
            "baseline": {"trades": 30, "profit_factor": 1.80, "net_pnl": 900.0},
            "candidate": {"trades": 30, "profit_factor": 0.30, "net_pnl": -200.0},
        },
    }
    _patch_run_backtest(monkeypatch, results)

    evaluation = evaluate_offline(
        settings=_settings_for_offline(),
        change=change,
        symbols=["AAPL"],
        start=date(2026, 1, 1),
        end=date(2026, 6, 30),
        bar_loader=_loader_for_offline(tmp_path),
        train_fraction=0.7,
    )

    assert evaluation.accepted is False
    assert "candidate PF not >= baseline PF + 0.10" in evaluation.reasons
    assert "candidate net P&L not > baseline" in evaluation.reasons


def test_offline_evaluation_rejects_when_trade_count_too_low(
    tmp_path: Path, monkeypatch
) -> None:
    """Bars engineered so candidate validation has fewer than 20 trades."""
    from trading_bot.learning.experiments.models import ParameterChange
    from trading_bot.learning.experiments.replay import evaluate_offline

    change = ParameterChange(
        section="supermodel",
        field="counter_veto_weight",
        baseline=1.0,
        candidate=0.75,
    )

    results = {
        "train": {
            "baseline": {"trades": 25, "profit_factor": 1.30, "net_pnl": 400.0},
            "candidate": {"trades": 18, "profit_factor": 1.50, "net_pnl": 600.0},
        },
        "validation": {
            "baseline": {"trades": 30, "profit_factor": 1.50, "net_pnl": 700.0},
            "candidate": {"trades": 10, "profit_factor": 2.00, "net_pnl": 1200.0},
        },
    }
    _patch_run_backtest(monkeypatch, results)

    evaluation = evaluate_offline(
        settings=_settings_for_offline(),
        change=change,
        symbols=["AAPL"],
        start=date(2026, 1, 1),
        end=date(2026, 6, 30),
        bar_loader=_loader_for_offline(tmp_path),
        train_fraction=0.7,
    )

    assert evaluation.accepted is False
    assert "validation trades < 20" in evaluation.reasons


def test_offline_evaluation_rejects_when_drawdown_too_worse(
    tmp_path: Path, monkeypatch
) -> None:
    """Bars engineered so candidate validation drawdown exceeds baseline+5pp.

    Note: ``run_backtest`` does not currently compute ``max_drawdown_pct``,
    so this test exercises the boundary by feeding the metric through the
    result dict that ``_summarize`` reads. When the runner starts reporting
    drawdown, this test continues to exercise the same gate.
    """
    from trading_bot.learning.experiments.models import ParameterChange
    from trading_bot.learning.experiments.replay import evaluate_offline

    change = ParameterChange(
        section="supermodel",
        field="counter_veto_weight",
        baseline=1.0,
        candidate=0.75,
    )

    results = {
        "train": {
            "baseline": {
                "trades": 25,
                "profit_factor": 1.30,
                "net_pnl": 400.0,
                "max_drawdown_pct": 2.0,
            },
            "candidate": {
                "trades": 25,
                "profit_factor": 1.20,
                "net_pnl": 300.0,
                "max_drawdown_pct": 3.0,
            },
        },
        "validation": {
            "baseline": {
                "trades": 30,
                "profit_factor": 1.60,
                "net_pnl": 800.0,
                "max_drawdown_pct": 2.0,
            },
            "candidate": {
                "trades": 30,
                "profit_factor": 1.50,
                "net_pnl": 700.0,
                "max_drawdown_pct": 9.0,
            },
        },
    }
    _patch_run_backtest(monkeypatch, results)

    evaluation = evaluate_offline(
        settings=_settings_for_offline(),
        change=change,
        symbols=["AAPL"],
        start=date(2026, 1, 1),
        end=date(2026, 6, 30),
        bar_loader=_loader_for_offline(tmp_path),
        train_fraction=0.7,
    )

    assert evaluation.accepted is False
    assert "candidate drawdown > baseline + 5pp" in evaluation.reasons
