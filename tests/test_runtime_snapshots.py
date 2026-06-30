"""Tests for runtime.snapshots module (33 lines)."""

from __future__ import annotations

import json

import pytest

from trading_bot.runtime import snapshots


class TestWriteSnapshot:
    def test_writes_file_with_generated_at(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "snap.json"
        snapshots.write_snapshot(str(path), {"ticker": "AAPL", "price": 100.0})
        assert path.exists()
        data = json.loads(path.read_text())
        assert "generated_at" in data
        assert data["generated_at"].endswith("+00:00")
        assert data["ticker"] == "AAPL"
        assert data["price"] == 100.0

    def test_accepts_path_object(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "dir" / "snap.json"
        snapshots.write_snapshot(path, {"x": 1})
        assert path.exists()
        assert json.loads(path.read_text())["x"] == 1

    def test_generates_isoformat_without_microseconds(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "snap.json"
        snapshots.write_snapshot(str(path), {})
        data = json.loads(path.read_text())
        # isoformat with replace(microsecond=0) -> no ".XXXXXX" before tz
        assert "." not in data["generated_at"].split("+")[0] or data["generated_at"].count(".") == 0


class TestReadRecentDecisionRows:
    def test_missing_file_returns_empty(self, tmp_path) -> None:  # noqa: ANN001
        assert snapshots.read_recent_decision_rows(tmp_path / "nope.jsonl") == []

    def test_reads_jsonl_rows(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "decisions.jsonl"
        path.write_text(
            json.dumps({"i": 1}) + "\n" + json.dumps({"i": 2}) + "\n" + json.dumps({"i": 3}) + "\n",
            encoding="utf-8",
        )
        rows = snapshots.read_recent_decision_rows(path)
        assert [r["i"] for r in rows] == [1, 2, 3]

    def test_limit_returns_last_n(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "decisions.jsonl"
        lines = [json.dumps({"i": i}) for i in range(10)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rows = snapshots.read_recent_decision_rows(path, limit=3)
        assert [r["i"] for r in rows] == [7, 8, 9]

    def test_default_limit_is_10(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "decisions.jsonl"
        lines = [json.dumps({"i": i}) for i in range(15)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rows = snapshots.read_recent_decision_rows(path)
        assert len(rows) == 10
        assert rows[-1]["i"] == 14

    def test_skips_blank_lines(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "decisions.jsonl"
        path.write_text('{"a": 1}\n\n   \n{"a": 2}\n', encoding="utf-8")
        rows = snapshots.read_recent_decision_rows(path)
        assert [r["a"] for r in rows] == [1, 2]

    def test_skips_invalid_json_lines(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "decisions.jsonl"
        path.write_text('{"a": 1}\nnot json\n{"a": 2}\n', encoding="utf-8")
        rows = snapshots.read_recent_decision_rows(path)
        assert [r["a"] for r in rows] == [1, 2]

    def test_empty_file_returns_empty(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "decisions.jsonl"
        path.write_text("", encoding="utf-8")
        assert snapshots.read_recent_decision_rows(path) == []