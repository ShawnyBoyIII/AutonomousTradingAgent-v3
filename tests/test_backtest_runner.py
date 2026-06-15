from trading_bot.backtest.runner import iterate_bars


class _FrameSlice:
    def __init__(self, rows: list[dict[str, int]]) -> None:
        self._rows = rows

    def __getitem__(self, key: str) -> list[int]:
        return [row[key] for row in self._rows]

    def copy(self) -> "_FrameSlice":
        return _FrameSlice(self._rows.copy())


class _FakeFrame:
    def __init__(self, rows: list[dict[str, int]]) -> None:
        self._rows = rows

    @property
    def iloc(self) -> "_FakeFrame":
        return self

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, item: slice) -> _FrameSlice:
        return _FrameSlice(self._rows[item])


def test_iterate_bars_yields_chronological_slices() -> None:
    frame = _FakeFrame(
        [
            {"close": 1},
            {"close": 2},
            {"close": 3},
            {"close": 4},
        ]
    )
    slices = list(iterate_bars(frame, warmup=2))
    assert len(slices) == 2
    assert list(slices[0]["close"]) == [1, 2]
    assert list(slices[1]["close"]) == [1, 2, 3]
