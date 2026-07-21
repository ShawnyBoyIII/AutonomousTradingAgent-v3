"""Shadow harness: JSONL reload must preserve valid prefix on malformed tail.

Bug: ShadowLedger._load_state parsed all JSONL lines in a single list
comprehension. A torn final record raised json.JSONDecodeError which was
caught at the comprehension level, discarding every previously valid
record. Restarting the harness with a malformed tail wiped state.

Fix: parse line-by-line and skip the malformed line. Valid records
preceding it remain in the rebuilt state.
"""

from __future__ import annotations

from trading_bot.learning.experiments.shadow import ShadowFill, ShadowLedger


def _seed_ledger(tmp_path, fills):
    harness = ShadowLedger(artifacts_dir=tmp_path, starting_cash=10_000.0)
    for f in fills:
        harness.record(f)
    return harness


def test_reload_preserves_valid_prefix_when_tail_is_torn(tmp_path):
    """A torn trailing JSON line must not destroy previously valid fills."""
    fills = [
        ShadowFill(ticker="AAPL", side="BUY", quantity=10, fill_price=100.0, fees=1.0),
        ShadowFill(ticker="AAPL", side="SELL", quantity=5, fill_price=110.0, fees=1.0),
        ShadowFill(ticker="MSFT", side="BUY", quantity=8, fill_price=200.0, fees=1.0),
        ShadowFill(ticker="MSFT", side="SELL", quantity=8, fill_price=210.0, fees=1.0),
    ]
    original = _seed_ledger(tmp_path, fills)
    cash_before = original._cash
    positions_before = dict(original._positions)

    # Append a torn trailing record (incomplete JSON).
    fills_path = tmp_path / "shadow-fills.jsonl"
    with fills_path.open("a", encoding="utf-8") as handle:
        handle.write('{"ticker": "GOOG", "side": "BUY", "quantity": 5, "fill_pr')  # truncated

    # Re-load: should preserve the valid prefix, skip the torn tail.
    reloaded = ShadowLedger(artifacts_dir=tmp_path, starting_cash=10_000.0)
    assert reloaded._cash == cash_before, (
        f"cash must survive torn tail; got {reloaded._cash}, expected {cash_before}"
    )
    assert reloaded._positions == positions_before


def test_reload_skips_malformed_middle_lines(tmp_path):
    """A malformed line in the middle of the file is skipped without losing
    records that follow it."""
    fills = [
        ShadowFill(ticker="AAPL", side="BUY", quantity=10, fill_price=100.0, fees=1.0),
        ShadowFill(ticker="AAPL", side="SELL", quantity=10, fill_price=110.0, fees=1.0),
    ]
    _seed_ledger(tmp_path, fills)

    fills_path = tmp_path / "shadow-fills.jsonl"
    with fills_path.open("a", encoding="utf-8") as handle:
        # Inject a completely malformed line, then a valid one.
        handle.write("not json at all\n")
        handle.write(
            '{"ticker": "MSFT", "side": "BUY", "quantity": 5,'
            ' "fill_price": 300.0, "fees": 1.0}\n'
        )

    reloaded = ShadowLedger(artifacts_dir=tmp_path, starting_cash=10_000.0)
    # MSFT position should be reconstructed from the line after the malformed one
    assert "MSFT" in reloaded._positions
    assert reloaded._positions["MSFT"]["qty"] == 5.0


def test_reload_returns_empty_state_on_empty_file(tmp_path):
    reloaded = ShadowLedger(artifacts_dir=tmp_path, starting_cash=10_000.0)
    assert reloaded._cash == 10_000.0
    assert reloaded._positions == {}


def test_reload_handles_blank_lines_gracefully(tmp_path):
    fills = [
        ShadowFill(ticker="AAPL", side="BUY", quantity=10, fill_price=100.0, fees=1.0),
    ]
    _seed_ledger(tmp_path, fills)
    fills_path = tmp_path / "shadow-fills.jsonl"
    with fills_path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n\n")  # blank lines

    reloaded = ShadowLedger(artifacts_dir=tmp_path, starting_cash=10_000.0)
    assert "AAPL" in reloaded._positions
    assert reloaded._positions["AAPL"]["qty"] == 10.0