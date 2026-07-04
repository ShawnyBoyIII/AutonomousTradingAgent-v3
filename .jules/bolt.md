## 2025-02-27 - [Iterrows bottleneck]
**Learning:** `iterrows()` is used in critical data paths (`cache.py`, `market_data.py`, `runner.py`) which creates significant overhead by instantiating a Series for every row.
**Action:** Replace `iterrows()` with `itertuples(index=False)` or `itertuples(index=False, name=None)` for faster iteration, especially in serialization/caching and backtesting hot loops.
