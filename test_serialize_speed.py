import pandas as pd
import numpy as np
import time
import json

df = pd.DataFrame({
    'timestamp': pd.date_range('2020-01-01', periods=10000, freq='min'),
    'open': np.random.randn(10000),
    'high': np.random.randn(10000),
    'low': np.random.randn(10000),
    'close': np.random.randn(10000),
    'volume': np.random.randint(100, 1000, 10000)
})
df.set_index('timestamp', inplace=True)

def orig_serialize(df: pd.DataFrame) -> str:
    if df.empty:
        return json.dumps({"empty": True})

    def _convert_value(v):
        if isinstance(v, (pd.Timestamp, datetime)):
            return v.isoformat() if hasattr(v, "isoformat") else str(v)
        return v
    from datetime import datetime
    data = []
    for _, row in df.iterrows():
        data.append([_convert_value(v) for v in row])

    index_data = []
    if hasattr(df.index, "tolist"):
        index_data = df.index.tolist()
    else:
        index_data = list(df.index)
    index_data = [_convert_value(v) for v in index_data]

    return json.dumps({
        "columns": df.columns.tolist(),
        "index": index_data,
        "data": data,
        "dtype": str(df.dtypes.to_dict()),
    })

def new_serialize(df: pd.DataFrame) -> str:
    if df.empty:
        return json.dumps({"empty": True})

    # The OHLCV DataFrame only has numeric data and the index is datetime.
    # iterrows is extremely slow. We can just use df.values.tolist()
    # It will be much faster. If there are datetime columns, we could handle them,
    # but for market data cache it's numeric values.

    # We can do df.values.tolist() directly.
    # If there are any non-primitive types, we could iterate over values, but usually it's fast

    # Let's handle the index safely
    # If the index is a DatetimeIndex, we can convert it using strftime or .astype(str)

    if isinstance(df.index, pd.DatetimeIndex):
        # isoformat requires T separator, pandas default str casts it with space sometimes, but
        # isoformat handles timezones etc.
        index_data = [v.isoformat() for v in df.index]
    else:
        index_data = df.index.tolist()

    data = df.values.tolist()

    # if data contains timestamps, they won't be serialized by json easily.
    # We can rely on the fact that market data is floats/ints.
    # Just in case there are datetimes in values:
    # data = df.where(pd.notnull(df), None).values.tolist()

    return json.dumps({
        "columns": df.columns.tolist(),
        "index": index_data,
        "data": data,
        "dtype": str(df.dtypes.to_dict()),
    })

def iter_serialize(df: pd.DataFrame) -> str:
    if df.empty:
        return json.dumps({"empty": True})

    from datetime import datetime

    if isinstance(df.index, pd.DatetimeIndex):
        index_data = [v.isoformat() for v in df.index]
    else:
        index_data = df.index.tolist()

    # use itertuples
    data = []
    # itertuples returns namedtuples. we want a list of lists/tuples
    # index=False means just data
    for row in df.itertuples(index=False, name=None):
        # We assume values are numeric. If there are NaNs, they become floats which JSON handles (or we need to map them to null)
        data.append(row)

    return json.dumps({
        "columns": df.columns.tolist(),
        "index": index_data,
        "data": data,
        "dtype": str(df.dtypes.to_dict()),
    })

start = time.time()
res1 = orig_serialize(df)
orig_time = time.time() - start
print(f"Orig: {orig_time:.3f}s")

start = time.time()
res2 = new_serialize(df)
new_time = time.time() - start
print(f"New (values.tolist): {new_time:.3f}s")

start = time.time()
res3 = iter_serialize(df)
iter_time = time.time() - start
print(f"Iter (itertuples): {iter_time:.3f}s")
