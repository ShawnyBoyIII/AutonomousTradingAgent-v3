from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd


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
