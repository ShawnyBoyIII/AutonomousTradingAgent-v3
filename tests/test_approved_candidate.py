"""TDD: ApprovedCandidate contract round-trip and lookup."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_bot.runtime.approved_candidate import (
    ApprovedCandidate,
    list_candidates,
    new_scan_id,
    read_candidate,
    write_candidates_jsonl,
)


def _sample(ticker: str = "AAPL", **overrides) -> ApprovedCandidate:
    base = dict(
        ticker=ticker,
        quality="GREEN",
        timestamp="2026-07-21T13:30:00+00:00",
        entry=190.50,
        stop=187.20,
        target=196.30,
        qty=10,
        rr=1.6,
        confidence=0.78,
        risk=33.0,
        allocation=0.01,
        strategy="v3-trend_following",
        supermodel_decision="caution",
        scan_id=new_scan_id(),
        v3_score=72.5,
        v3_confidence="medium",
        v3_regime="range_bound",
        v3_setup="trend_following",
        source_votes=[{"source": "v3", "confidence": 0.7, "strategy": "trend_following"}],
    )
    base.update(overrides)
    return ApprovedCandidate(**base)


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    sample = _sample()
    path = write_candidates_jsonl([sample], tmp_path / "candidates.jsonl")
    loaded = list_candidates(path)[0]
    assert loaded == sample


def test_write_uses_jsonl_one_record_per_line(tmp_path: Path) -> None:
    path = write_candidates_jsonl(
        [_sample("AAPL"), _sample("MSFT")], tmp_path / "candidates.jsonl"
    )
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)["scan_id"]


def test_read_candidate_finds_first_match(tmp_path: Path) -> None:
    target = _sample("AAPL", entry=190.0)
    other = _sample("MSFT", entry=400.0)
    path = write_candidates_jsonl([target, other], tmp_path / "candidates.jsonl")
    found = read_candidate("AAPL", path)
    assert found == target
    assert found.entry == 190.0


def test_read_candidate_is_case_insensitive(tmp_path: Path) -> None:
    path = write_candidates_jsonl([_sample("AAPL")], tmp_path / "candidates.jsonl")
    assert read_candidate("aapl", path) is not None
    assert read_candidate("AAPL", path) is not None


def test_read_candidate_returns_none_when_missing(tmp_path: Path) -> None:
    path = write_candidates_jsonl(
        [_sample("AAPL")], tmp_path / "candidates.jsonl"
    )
    assert read_candidate("MSFT", path) is None
    assert read_candidate("AAPL", tmp_path / "missing.jsonl") is None


def test_list_candidates_skips_blank_and_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "candidates.jsonl"
    path.write_text(
        "\n".join(
            [
                "",
                json.dumps(_sample("AAPL").to_dict()),
                "garbage line",
                json.dumps(_sample("MSFT").to_dict()),
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = list_candidates(path)
    assert [c.ticker for c in loaded] == ["AAPL", "MSFT"]


def test_candidate_is_frozen() -> None:
    sample = _sample()
    with pytest.raises(Exception):
        sample.ticker = "MSFT"  # type: ignore[misc]


def test_scan_id_is_unique_and_parseable() -> None:
    a = new_scan_id()
    b = new_scan_id()
    assert a != b
    assert "T" in a
