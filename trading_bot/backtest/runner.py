from typing import Any


def iterate_bars(frame: Any, warmup: int):
    for end_index in range(warmup, len(frame)):
        yield frame.iloc[:end_index].copy()
