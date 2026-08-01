## 2025-02-27 - [Iterrows bottleneck]
**Learning:** `iterrows()` is used in critical data paths (`cache.py`, `market_data.py`, `runner.py`) which creates significant overhead by instantiating a Series for every row.
**Action:** Replace `iterrows()` with `itertuples(index=False)` or `itertuples(index=False, name=None)` for faster iteration, especially in serialization/caching and backtesting hot loops.

## 2024-07-28 - Vectorized pandas .iloc is slow
**Learning:** Iterating over pandas Series with `.iloc` inside a loop is extremely slow compared to extracting the underlying numpy arrays and iterating over those.
**Action:** Extract `.to_numpy()` before the loop and index into the numpy arrays instead when processing large time series sequentially in Python.

## 2025-02-12 - [Fast DataFrame to float List Conversion]
**Learning:** Using list comprehensions over .tolist() to cast Pandas series to floats (e.g., `[float(x) for x in df["col"].tolist()]`) is significantly slower (~2.6x) than using NumPy's C-level casting (`df["col"].to_numpy(dtype=float).tolist()`).
**Action:** Always use `.to_numpy(dtype=float).tolist()` when converting DataFrame columns to typed Python lists for high-performance iterative processing.
