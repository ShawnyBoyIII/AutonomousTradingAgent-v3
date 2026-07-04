from __future__ import annotations

import pandas as pd


def add_future_return_pct(
    frame: pd.DataFrame,
    *,
    horizon: int = 1,
    price_col: str = "close",
    column_name: str | None = None,
) -> pd.DataFrame:
    result = frame.copy()
    name = column_name or f"future_return_{horizon}"
    future_price = result[price_col].shift(-horizon)
    result[name] = ((future_price - result[price_col]) / result[price_col]) * 100.0
    return result


def add_future_direction_label(
    frame: pd.DataFrame,
    *,
    horizon: int = 1,
    threshold_pct: float = 0.0,
    price_col: str = "close",
    column_name: str | None = None,
) -> pd.DataFrame:
    result = add_future_return_pct(
        frame,
        horizon=horizon,
        price_col=price_col,
        column_name="__future_return_tmp__",
    )
    name = column_name or f"future_up_{horizon}"
    result[name] = (result["__future_return_tmp__"] > threshold_pct).astype("float")
    result.loc[result["__future_return_tmp__"].isna(), name] = pd.NA
    return result.drop(columns=["__future_return_tmp__"])
