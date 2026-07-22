"""Shared feature pipeline.

Adds the indicators that runtime V2.5/V3 and backtest both need to
a single DataFrame. Before this module existed, runtime and
backtest each added slightly different subsets of features. The
backtest preprocessing skipped RSI and VWAP for the daily-fallback
path even though V3 mean-reversion detectors (oversold-bounce and
VWAP-reversion) require them. The result: ``backtest --strategy v3``
silently omitted those strategies because the features they depended
on were missing.

Now both call sites call :func:`add_all_features` and use the same
indicator pipeline. The function is intentionally pure: it does not
mutate the input, does not depend on the runtime config, and does not
touch the broker or ledger. The only configurable knob is the RSI
and Bollinger period (default 14 and 20) so V3 strategies that expect
``rsi_14`` and ``bb_*`` continue to work unchanged.
"""

from __future__ import annotations

import pandas as pd

from trading_bot.data.indicators import (
    add_atr,
    add_bollinger_bands,
    add_ema,
    add_rsi,
    add_sma,
    add_vwap,
)


def add_all_features(
    frame: pd.DataFrame,
    *,
    atr_period: int = 14,
    rsi_period: int = 14,
    bollinger_period: int = 20,
    bollinger_std_dev: float = 2.0,
    ema_period: int = 20,
    sma_period: int = 50,
) -> pd.DataFrame:
    """Return a new DataFrame with all V2.5/V3 features added.

    Adds EMA(20), SMA(50), ATR, RSI(14), Bollinger Bands (20, 2.0),
    and VWAP (when volume + close are present). The original frame
    is not mutated.

    The function never raises on missing columns: it returns a frame
    with whatever features could be added. Callers that require a
    specific feature should assert its presence.
    """
    out = frame.copy()
    if out.empty:
        return out
    try:
        out = add_ema(out, period=ema_period, column_name="ema_20")
    except Exception:  # noqa: BLE001
        pass
    try:
        out = add_sma(out, period=sma_period, column_name="sma_50")
    except Exception:  # noqa: BLE001
        pass
    try:
        out = add_atr(out, period=atr_period)
    except Exception:  # noqa: BLE001
        pass
    try:
        out = add_rsi(out, period=rsi_period)
    except Exception:  # noqa: BLE001
        pass
    try:
        out = add_bollinger_bands(out, period=bollinger_period, std_dev=bollinger_std_dev)
    except Exception:  # noqa: BLE001
        pass
    try:
        if "volume" in out.columns and "close" in out.columns:
            out = add_vwap(out)
    except Exception:  # noqa: BLE001
        pass
    return out
