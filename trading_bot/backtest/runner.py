from typing import Any


def iterate_bars(frame: Any, warmup: int):
    if warmup <= 0:
        raise ValueError("warmup must be positive")

    for end_index in range(warmup, len(frame)):
        yield frame.iloc[:end_index].copy()
