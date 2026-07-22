"""TDD: FillTransaction makes BUY and SELL atomic across stores."""
from __future__ import annotations

from typing import Any

import pytest

from trading_bot.runtime.fill_transaction import (
    FillTransaction,
    FillTransactionError,
)


def test_empty_transaction_runs() -> None:
    tx = FillTransaction()
    assert tx.run(fill=object(), side="BUY") is None


def test_steps_run_in_order() -> None:
    tx = FillTransaction()
    seen: list[str] = []
    tx.register(lambda **kw: seen.append("a"))
    tx.register(lambda **kw: seen.append("b"))
    tx.run(fill=object(), side="BUY")
    assert seen == ["a", "b"]


def test_failure_stops_subsequent_steps() -> None:
    tx = FillTransaction()
    seen: list[str] = []
    tx.register(lambda **kw: seen.append("a"))
    tx.register(lambda **kw: 1 / 0)
    tx.register(lambda **kw: seen.append("c"))
    with pytest.raises(FillTransactionError):
        tx.run(fill=object(), side="BUY")
    assert seen == ["a"]


def test_transaction_passes_fill_and_side_and_ctx() -> None:
    tx = FillTransaction()
    captured: dict[str, Any] = {}

    def capture(*, fill, side, **ctx):
        captured["fill"] = fill
        captured["side"] = side
        captured["ctx"] = ctx

    tx.register(capture)
    sentinel = object()
    tx.run(fill=sentinel, side="SELL", realized_pnl=12.5, foo="bar")
    assert captured["fill"] is sentinel
    assert captured["side"] == "SELL"
    assert captured["ctx"] == {"realized_pnl": 12.5, "foo": "bar"}


def test_explicit_fill_transaction_error_passes_through() -> None:
    tx = FillTransaction()

    def fail(**kw):
        raise FillTransactionError("custom")

    tx.register(fail)
    with pytest.raises(FillTransactionError, match="custom"):
        tx.run(fill=object(), side="BUY")
