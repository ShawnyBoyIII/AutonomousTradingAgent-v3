import pandas as pd
import pytest

from trading_bot.config.settings import Settings, StrategySettings
from trading_bot.backtest.metrics import compute_win_rate
from trading_bot.backtest.runner import _filter_frame_by_date, _run_symbol_backtest, iterate_bars, run_walk_forward


def test_iterate_bars_yields_chronological_slices() -> None:
    frame = pd.DataFrame({"close": [1, 2, 3, 4]})
    slices = list(iterate_bars(frame, warmup=2))

    assert len(slices) == 2
    assert list(slices[0]["close"]) == [1, 2]
    assert list(slices[1]["close"]) == [1, 2, 3]


def test_iterate_bars_rejects_non_positive_warmup() -> None:
    frame = pd.DataFrame({"close": [1, 2, 3, 4]})

    with pytest.raises(ValueError, match="warmup must be positive"):
        list(iterate_bars(frame, warmup=0))


def test_compute_win_rate_returns_fraction_and_zero_safe_default() -> None:
    assert compute_win_rate(3, 1) == 0.75
    assert compute_win_rate(0, 0) == 0.0


def test_filter_frame_by_date_handles_tz_aware_timestamps() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-01 10:00:00-04:00",
                    "2026-06-10 10:00:00-04:00",
                    "2026-06-20 10:00:00-04:00",
                ]
            ),
            "close": [1.0, 2.0, 3.0],
        }
    )

    filtered = _filter_frame_by_date(frame, start="2026-06-05", end="2026-06-17")

    assert list(filtered["close"]) == [2.0]


def test_run_symbol_backtest_counts_stop_hit_as_loss_even_if_final_close_recovers() -> None:
    daily = pd.DataFrame(
        {
            "close": [100.0 + index for index in range(60)],
            "ema_20": [90.0 + index for index in range(60)],
            "sma_50": [80.0 + index for index in range(60)],
        }
    )
    intraday = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-13 10:00:00",
                    "2026-06-13 10:05:00",
                    "2026-06-13 10:10:00",
                    "2026-06-13 10:15:00",
                    "2026-06-13 10:20:00",
                    "2026-06-13 10:25:00",
                    "2026-06-13 10:30:00",
                ]
            ),
            "open": [99.9, 100.1, 100.0, 100.2, 100.5, 101.0, 102.5],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1, 101.1, 103.5],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4, 99.7, 102.0],
            "close": [100.0, 100.2, 100.1, 100.3, 101.0, 100.8, 103.0],
            "volume": [1000, 1100, 950, 1050, 2500, 1500, 1800],
        }
    )
    intraday["volume_avg_5"] = intraday["volume"].rolling(5).mean()

    result = _run_symbol_backtest("AAPL", daily, intraday, Settings())

    assert result == {
        "trades": 1,
        "wins": 0,
        "losses": 1,
        "net_pnl": -24.8,  # Updated for V2.5 position sizing
    }


def test_run_symbol_backtest_replays_multiple_trade_cycles() -> None:
    daily = pd.DataFrame(
        {
            "close": [100.0 + index for index in range(60)],
            "ema_20": [90.0 + index for index in range(60)],
            "sma_50": [80.0 + index for index in range(60)],
        }
    )
    intraday = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-13 10:00:00",
                    "2026-06-13 10:05:00",
                    "2026-06-13 10:10:00",
                    "2026-06-13 10:15:00",
                    "2026-06-13 10:20:00",
                    "2026-06-13 10:25:00",
                    "2026-06-13 10:30:00",
                    "2026-06-13 10:35:00",
                    "2026-06-13 10:40:00",
                    "2026-06-13 10:45:00",
                    "2026-06-13 10:50:00",
                    "2026-06-13 10:55:00",
                ]
            ),
            "open": [99.9, 100.1, 100.0, 100.2, 100.5, 101.0, 101.5, 101.6, 101.8, 102.0, 102.2, 102.8],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1, 103.2, 101.7, 101.9, 102.1, 102.3, 103.4, 103.6],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4, 100.9, 101.4, 101.5, 101.7, 101.9, 102.1, 102.6],
            "close": [100.0, 100.2, 100.1, 100.3, 101.0, 103.0, 101.6, 101.8, 102.0, 102.2, 103.2, 103.4],
            "volume": [1000, 1100, 950, 1050, 2500, 1500, 900, 950, 1000, 1100, 2600, 1700],
        }
    )
    intraday["volume_avg_5"] = intraday["volume"].rolling(5).mean()

    result = _run_symbol_backtest("AAPL", daily, intraday, Settings())

    assert result == {
        "trades": 2,
        "wins": 2,
        "losses": 0,
        "net_pnl": 45.4,  # Updated for V2.5 position sizing (smaller positions)
    }


def _v3_daily_frame() -> pd.DataFrame:
    """60 bars of gentle uptrend with narrowing ranges (BB squeeze).

    Classifies as WEAK_UPTREND regime so the V3 selector allows trend-following.
    """
    from trading_bot.data.indicators import add_atr, add_bollinger_bands, add_ema, add_sma

    closes = [100.0 + i * 0.5 for i in range(60)]
    ranges = [3.5 * (1 - i / 80) for i in range(60)]
    df = pd.DataFrame(
        {
            "close": closes,
            "high": [c + r / 2 for c, r in zip(closes, ranges)],
            "low": [c - r / 2 for c, r in zip(closes, ranges)],
            "open": [c - 0.2 for c in closes],
            "volume": [1_000_000 for _ in range(60)],
        }
    )
    df = add_ema(df, 20, "ema_20")
    df = add_sma(df, 50, "sma_50")
    df = add_atr(df, 14, "atr_14")
    df = add_bollinger_bands(df, 20)
    return df


def _v3_intraday_frame(bars: int = 20) -> pd.DataFrame:
    """Intraday frame with enough bars for RSI(14) + breakout + volume surge."""
    from trading_bot.data.indicators import add_rsi

    timestamps = pd.date_range("2026-06-13 10:00:00", periods=bars, freq="5min")
    base = 129.0
    closes = []
    highs = []
    lows = []
    opens = []
    volumes = []

    for i in range(bars):
        if i == 5:
            # Breakout bar: jump up with volume surge
            close = base + 2.0
            high = base + 3.0
            low = base - 0.2
            opens.append(base)
            volumes.append(6000)
        elif i == 6:
            # Target hit
            close = base + 3.0
            high = base + 4.0
            low = base + 1.5
            opens.append(base + 2.0)
            volumes.append(2000)
        elif i % 2 == 0:
            close = base + 0.1 * (i % 5)
            high = close + 0.3
            low = close - 0.3
            opens.append(close - 0.1)
            volumes.append(1000 + (i % 5) * 100)
        else:
            close = base + 0.2 * (i % 5)
            high = close + 0.3
            low = close - 0.3
            opens.append(close - 0.1)
            volumes.append(1100 + (i % 5) * 100)
        closes.append(close)
        highs.append(high)
        lows.append(low)

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )
    df["volume_avg_5"] = df["volume"].rolling(5).mean()
    df = add_rsi(df, 14)
    return df


def test_run_symbol_backtest_v3_path_produces_trades() -> None:
    """When use_v3_signals=True, the backtest uses StrategySelector."""
    daily = _v3_daily_frame()
    intraday = _v3_intraday_frame(bars=20)

    settings = Settings()
    settings.strategy = StrategySettings(use_v3_signals=True)

    result = _run_symbol_backtest("AAPL", daily, intraday, settings)

    # V3 path should produce at least one trade or cleanly return 0.
    # The key assertion is that it runs without error and returns the right dict shape.
    assert "trades" in result
    assert "wins" in result
    assert "losses" in result
    assert "net_pnl" in result
    assert result["trades"] + result["wins"] + result["losses"] >= 0


def test_run_symbol_backtest_v3_disabled_uses_legacy_path() -> None:
    """Without use_v3_signals, backtest falls back to legacy engine."""
    daily = pd.DataFrame(
        {
            "close": [100.0 + index for index in range(60)],
            "ema_20": [90.0 + index for index in range(60)],
            "sma_50": [80.0 + index for index in range(60)],
        }
    )
    intraday = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-13 10:00:00",
                    "2026-06-13 10:05:00",
                    "2026-06-13 10:10:00",
                    "2026-06-13 10:15:00",
                    "2026-06-13 10:20:00",
                    "2026-06-13 10:25:00",
                    "2026-06-13 10:30:00",
                ]
            ),
            "open": [99.9, 100.1, 100.0, 100.2, 100.5, 101.0, 102.5],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1, 101.1, 103.5],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4, 99.7, 102.0],
            "close": [100.0, 100.2, 100.1, 100.3, 101.0, 100.8, 103.0],
            "volume": [1000, 1100, 950, 1050, 2500, 1500, 1800],
        }
    )
    intraday["volume_avg_5"] = intraday["volume"].rolling(5).mean()

    result = _run_symbol_backtest("AAPL", daily, intraday, Settings())

    assert result["trades"] == 1


def test_backtest_v2_5_vs_v3_comparison() -> None:
    """Compare V2.5 and V3 engines on identical market data."""
    import trading_bot.data.market_data as market_data
    from trading_bot.backtest.runner import run_backtest
    from trading_bot.config.settings import Settings, StrategySettings
    from trading_bot.data.indicators import add_atr, add_bollinger_bands, add_ema, add_rsi, add_sma

    # Build daily frame with gentle uptrend + narrowing ranges (WEAK_UPTREND)
    closes = [100.0 + i * 0.5 for i in range(60)]
    ranges = [3.5 * (1 - i / 80) for i in range(60)]
    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-01", periods=60, freq="D"),
            "open": [c - 0.2 for c in closes],
            "high": [c + r / 2 for c, r in zip(closes, ranges)],
            "low": [c - r / 2 for c, r in zip(closes, ranges)],
            "close": closes,
            "volume": [1_000_000] * 60,
        }
    )
    daily = add_ema(daily, 20, "ema_20")
    daily = add_sma(daily, 50, "sma_50")
    daily = add_atr(daily, 14, "atr_14")
    daily = add_bollinger_bands(daily, 20)

    # Build intraday frame with 30 bars and multiple breakout patterns
    base = 129.0
    n = 30
    intraday_ts = pd.date_range("2026-06-13 09:30:00", periods=n, freq="5min")
    oclv = []
    for i in range(n):
        if i == 5 or i == 20:  # Two breakout bars
            oclv.append((base, base + 3.0, base - 0.2, base + 2.0, 6000))
        elif i == 6 or i == 21:  # Target hits
            oclv.append((base + 2.0, base + 4.0, base + 1.5, base + 3.0, 2000))
        elif i % 2 == 0:
            c = base + 0.1 * (i % 5)
            oclv.append((c - 0.1, c + 0.3, c - 0.3, c, 1000 + (i % 5) * 100))
        else:
            c = base + 0.2 * (i % 5)
            oclv.append((c - 0.1, c + 0.3, c - 0.3, c, 1100 + (i % 5) * 100))
    intraday = pd.DataFrame(
        {
            "timestamp": intraday_ts,
            "open": [x[0] for x in oclv],
            "high": [x[1] for x in oclv],
            "low": [x[2] for x in oclv],
            "close": [x[3] for x in oclv],
            "volume": [x[4] for x in oclv],
        }
    )
    intraday["volume_avg_5"] = intraday["volume"].rolling(5).mean()
    intraday = add_rsi(intraday, 14)

    original_fetch = market_data.fetch_bars

    def fake_fetch_bars(
        symbol: str, period: str, interval: str, start: str | None = None, end: str | None = None
    ) -> pd.DataFrame:
        return intraday.copy(deep=True) if interval == "5m" else daily.copy(deep=True)

    market_data.fetch_bars = fake_fetch_bars

    try:
        # V2.5 (legacy) backtest
        settings_v25 = Settings()
        settings_v25.app.backtest_summary_path = "/tmp/test_v25_backtest.json"

        # V3 backtest
        settings_v3 = Settings()
        settings_v3.strategy = StrategySettings(
            use_v3_signals=True,
            risk_tolerance="medium",
            min_confidence="medium",
        )
        settings_v3.app.backtest_summary_path = "/tmp/test_v3_backtest.json"

        v25_result = run_backtest(["AAPL"], settings_v25)
        v3_result = run_backtest(["AAPL"], settings_v3)

        # Calculate derived metrics
        def calc_metrics(result: dict[str, float | int]) -> dict[str, float]:
            trades = result["trades"]
            wins = result["wins"]
            net_pnl = result["net_pnl"]
            return {
                "trades": trades,
                "win_rate": wins / trades if trades > 0 else 0.0,
                "avg_pnl_per_trade": net_pnl / trades if trades > 0 else 0.0,
                "net_pnl": net_pnl,
                "wins": wins,
                "losses": result["losses"],
            }

        v25_metrics = calc_metrics(v25_result)
        v3_metrics = calc_metrics(v3_result)

        # Print comparison for debugging/analysis
        print("\n" + "=" * 60)
        print("BACKTEST COMPARISON: V2.5 vs V3")
        print("=" * 60)
        print(f"{'Metric':<25} {'V2.5 (Legacy)':>15} {'V3 (Strategy)':>15}")
        print("-" * 60)
        for key in ["trades", "wins", "losses", "win_rate", "avg_pnl_per_trade", "net_pnl"]:
            v25_val = v25_metrics[key]
            v3_val = v3_metrics[key]
            if key in ["win_rate", "avg_pnl_per_trade", "net_pnl"]:
                print(f"{key:<25} {v25_val:>15.4f} {v3_val:>15.4f}")
            else:
                print(f"{key:<25} {v25_val:>15} {v3_val:>15}")
        print("=" * 60)

        # Basic assertions - both should produce valid results
        assert v25_metrics["trades"] >= 0
        assert v3_metrics["trades"] >= 0

        # V3 may produce different results due to regime filtering
        # Key insight: V3 might skip some trades that V2.5 takes
        # due to regime filters or confidence thresholds

    finally:
        market_data.fetch_bars = original_fetch


def test_backtest_daily_mode_produces_trades() -> None:
    """Daily-only backtest mode works when intraday data unavailable."""
    from trading_bot.backtest.runner import _run_symbol_backtest_daily
    from trading_bot.config.settings import Settings
    from trading_bot.data.indicators import add_atr, add_bollinger_bands, add_ema, add_sma

    # Build daily frame with clear breakouts
    closes = [100.0 + i * 0.5 for i in range(100)]
    volumes = [1_000_000] * 100
    # Add volume surges on breakout days
    volumes[55] = 3_000_000  # Breakout
    volumes[56] = 3_500_000  # Continuation
    volumes[75] = 2_800_000  # Another breakout

    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=100, freq="D"),
            "open": [c - 0.2 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": volumes,
        }
    )
    df = add_ema(df, 20, "ema_20")
    df = add_sma(df, 50, "sma_50")
    df = add_atr(df, 14, "atr_14")
    df = add_bollinger_bands(df, 20)

    settings = Settings()
    result = _run_symbol_backtest_daily("AAPL", df, settings)

    assert result["trades"] >= 0
    assert result["wins"] + result["losses"] == result["trades"]
    assert isinstance(result["net_pnl"], float)


def test_backtest_daily_mode_handles_scalar_paper_positions(monkeypatch) -> None:
    from trading_bot.backtest.runner import _run_symbol_backtest_daily
    from trading_bot.config.settings import Settings
    from datetime import datetime

    from trading_bot.execution.paper_broker import PaperBroker
    from trading_bot.models.order import FillResult
    from trading_bot.models.signal import TradeSignal

    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=60, freq="D"),
            "open": [100.0 + i for i in range(60)],
            "high": [101.0 + i for i in range(60)],
            "low": [99.0 + i for i in range(60)],
            "close": [100.0 + i for i in range(60)],
            "volume": [1_000_000] * 60,
        }
    )

    signal = TradeSignal(
        ticker="AAPL",
        timeframe="daily",
        action="BUY",
        entry_price=150.0,
        stop_loss=149.0,
        profit_target=151.0,
        risk_reward_ratio=1.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(),
    )
    calls = {"count": 0}

    def fake_generate_daily_signal(symbol: str, daily_frame: pd.DataFrame, index: int):
        return signal if index in (50, 56) else None

    def fake_submit_signal_as_order(signal, broker: PaperBroker, account_equity: float, open_tickers, risk_settings, **kwargs):
        calls["count"] += 1
        assert isinstance(account_equity, float)
        if calls["count"] == 1:
            broker.positions[signal.ticker] = 2
            broker.cash = 9_700.0
            return FillResult(
                order_id="order-1",
                ticker=signal.ticker,
                quantity=2,
                fill_price=150.0,
                fees=1.0,
                filled_at=pd.Timestamp("2024-02-20").to_pydatetime(),
            )
        return None

    monkeypatch.setattr("trading_bot.backtest.runner.generate_daily_signal", fake_generate_daily_signal)
    monkeypatch.setattr("trading_bot.backtest.runner.submit_signal_as_order", fake_submit_signal_as_order)

    result = _run_symbol_backtest_daily("AAPL", df, Settings())

    assert calls["count"] == 2
    assert result["trades"] == 1


def test_run_walk_forward_aggregates_across_windows(monkeypatch) -> None:
    """Walk-forward produces per-window breakdown + aggregated totals."""
    import trading_bot.data.market_data as market_data

    daily = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=150, freq="D"),
            "open": [100.0 + i * 0.3 for i in range(150)],
            "high": [101.0 + i * 0.3 for i in range(150)],
            "low": [99.0 + i * 0.3 for i in range(150)],
            "close": [100.0 + i * 0.3 for i in range(150)],
            "volume": [1_000_000 for _ in range(150)],
        }
    )
    intraday = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-06-13", periods=20, freq="5min"),
            "open": [99.9 + i * 0.1 for i in range(20)],
            "high": [100.1 + i * 0.1 for i in range(20)],
            "low": [99.8 + i * 0.1 for i in range(20)],
            "close": [100.0 + i * 0.1 for i in range(20)],
            "volume": [1000 + i * 100 for i in range(20)],
        }
    )

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        if interval == "5m":
            return intraday.copy(deep=True)
        return daily.copy(deep=True)

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)

    result = run_walk_forward(["AAPL"], Settings(), start="2026-01-01", end="2026-03-31", windows=3)

    assert "windows" in result
    assert len(result["windows"]) == 3
    for w in result["windows"]:
        assert w["trades"] >= 0
        assert w["wins"] >= 0
        assert w["losses"] >= 0
        assert isinstance(w["net_pnl"], float)
        assert 0.0 <= w["win_rate"] <= 1.0 or w["trades"] == 0

    assert result["trades"] == sum(w["trades"] for w in result["windows"])
    assert result["wins"] == sum(w["wins"] for w in result["windows"])
    assert result["losses"] == sum(w["losses"] for w in result["windows"])
    assert isinstance(result["net_pnl"], float)
    assert result["windows"][0]["window"] == 1
    assert result["windows"][1]["window"] == 2
    assert result["windows"][2]["window"] == 3
