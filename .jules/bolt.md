## 2025-02-27 - [Iterrows bottleneck]
**Learning:** `iterrows()` is used in critical data paths (`cache.py`, `market_data.py`, `runner.py`) which creates significant overhead by instantiating a Series for every row.
**Action:** Replace `iterrows()` with `itertuples(index=False)` or `itertuples(index=False, name=None)` for faster iteration, especially in serialization/caching and backtesting hot loops.

## 2024-07-28 - Vectorized pandas .iloc is slow
**Learning:** Iterating over pandas Series with `.iloc` inside a loop is extremely slow compared to extracting the underlying numpy arrays and iterating over those.
**Action:** Extract `.to_numpy()` before the loop and index into the numpy arrays instead when processing large time series sequentially in Python.
