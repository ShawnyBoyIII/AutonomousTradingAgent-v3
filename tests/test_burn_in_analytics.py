from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import pytest

from trading_bot.reports.burn_in_analytics import (
    _parse_decision_log,
    compute_trade_summary,
    compute_signal_summary,
    compute_exit_summary,
    compute_counter_thesis_summary,
    compute_risk_summary,
    compute_ticker_performance,
    compute_time_analysis,
    generate_recommendations,
    compute_burn_in_report,
    format_report,
)


class TestParseDecisionLog:
    def test_parses_valid_jsonl(self, tmp_path):
        log_path = tmp_path / "decision-log.jsonl"
        log_path.write_text(
            '{"command": "scan", "ticker": "AAPL", "status": "APPROVED"}\n'
            '{"command": "scan", "ticker": "SPY", "status": "NO_SIGNAL"}\n'
            '{"invalid json\n'
            '{"command": "paper-trade", "ticker": "TSLA", "status": "FILLED"}\n',
            encoding="utf-8",
        )
        events = _parse_decision_log(log_path)
        assert len(events) == 3
        assert events[0]["ticker"] == "AAPL"
        assert events[1]["ticker"] == "SPY"
        assert events[2]["ticker"] == "TSLA"

    def test_returns_empty_for_missing_file(self, tmp_path):
        log_path = tmp_path / "nonexistent.jsonl"
        events = _parse_decision_log(log_path)
        assert events == []

    def test_skips_empty_lines(self, tmp_path):
        log_path = tmp_path / "decision-log.jsonl"
        log_path.write_text("\n\n{\"command\": \"scan\", \"ticker\": \"AAPL\"}\n\n", encoding="utf-8")
        events = _parse_decision_log(log_path)
        assert len(events) == 1


class TestComputeTradeSummary:
    def test_matches_buys_with_sells(self, tmp_path):
        log_path = tmp_path / "decision-log.jsonl"
        log_path.write_text(
            json.dumps({"command": "paper-trade", "ticker": "AAPL", "status": "FILLED",
                        "quantity": 10, "fill_price": 100.0, "fees": 1.0}) + "\n"
            + json.dumps({"command": "manage-positions", "ticker": "AAPL", "status": "FILLED",
                          "reason": "profit_target", "quantity": 10, "fill_price": 110.0, "fees": 1.0}) + "\n",
            encoding="utf-8",
        )
        db_path = tmp_path / "state.db"
        ledger = None  # Will create a minimal mock

        # Create a minimal ledger mock
        from unittest.mock import MagicMock
        ledger = MagicMock()
        ledger.list_order_rows.return_value = []

        summary = compute_trade_summary(_parse_decision_log(log_path), ledger)
        assert summary["total_fills"] == 2
        assert summary["closed_trades"] == 1
        assert summary["wins"] == 1
        assert summary["losses"] == 0
        assert summary["total_pnl"] == 98.0  # (110-100)*10 - 1 - 1

    def test_counts_losses(self, tmp_path):
        log_path = tmp_path / "decision-log.jsonl"
        log_path.write_text(
            json.dumps({"command": "paper-trade", "ticker": "SPY", "status": "FILLED",
                        "quantity": 5, "fill_price": 500.0, "fees": 1.0}) + "\n"
            + json.dumps({"command": "manage-positions", "ticker": "SPY", "status": "FILLED",
                          "reason": "stop_loss", "quantity": 5, "fill_price": 490.0, "fees": 1.0}) + "\n",
            encoding="utf-8",
        )
        from unittest.mock import MagicMock
        ledger = MagicMock()
        ledger.list_order_rows.return_value = []

        summary = compute_trade_summary(_parse_decision_log(log_path), ledger)
        assert summary["losses"] == 1
        assert summary["total_pnl"] == -52.0  # (490-500)*5 - 1 - 1

    def test_handles_orphan_sells(self, tmp_path):
        log_path = tmp_path / "decision-log.jsonl"
        log_path.write_text(
            json.dumps({"command": "manage-positions", "ticker": "TSLA", "status": "FILLED",
                        "reason": "eod", "quantity": 3, "fill_price": 200.0, "fees": 1.0}) + "\n",
            encoding="utf-8",
        )
        from unittest.mock import MagicMock
        ledger = MagicMock()
        ledger.list_order_rows.return_value = []

        summary = compute_trade_summary(_parse_decision_log(log_path), ledger)
        assert summary["losses"] == 1

    def test_empty_events(self):
        from unittest.mock import MagicMock
        ledger = MagicMock()
        ledger.list_order_rows.return_value = []
        summary = compute_trade_summary([], ledger)
        assert summary["total_fills"] == 0
        assert summary["closed_trades"] == 0


class TestComputeSignalSummary:
    def test_counts_statuses(self):
        events = [
            {"command": "scan", "ticker": "AAPL", "status": "APPROVED", "quality": "GREEN"},
            {"command": "scan", "ticker": "SPY", "status": "NO_SIGNAL"},
            {"command": "scan", "ticker": "TSLA", "status": "REJECTED", "reason": "stale"},
            {"command": "scan", "ticker": "NVDA", "status": "APPROVED", "quality": "YELLOW",
             "confidence": 0.75},
        ]
        summary = compute_signal_summary(events)
        assert summary["total_scans"] == 4
        assert summary["approved_count"] == 2
        assert summary["status_counts"]["APPROVED"] == 2
        assert summary["status_counts"]["NO_SIGNAL"] == 1
        assert summary["status_counts"]["REJECTED"] == 1
        assert summary["avg_confidence"] == 0.75

    def test_rejection_reasons(self):
        events = [
            {"command": "scan", "ticker": "A", "status": "REJECTED", "reason": "stale"},
            {"command": "scan", "ticker": "B", "status": "REJECTED", "reason": "stale"},
            {"command": "scan", "ticker": "C", "status": "REJECTED", "reason": "low_confidence"},
        ]
        summary = compute_signal_summary(events)
        assert summary["rejection_reasons"]["stale"] == 2
        assert summary["rejection_reasons"]["low_confidence"] == 1


class TestComputeExitSummary:
    def test_counts_exit_reasons(self):
        events = [
            {"command": "manage-positions", "ticker": "AAPL", "status": "FILLED", "reason": "stop_loss"},
            {"command": "manage-positions", "ticker": "SPY", "status": "FILLED", "reason": "profit_target"},
            {"command": "manage-positions", "ticker": "TSLA", "status": "FILLED", "reason": "eod"},
            {"command": "manage-positions", "ticker": "NVDA", "status": "FILLED", "reason": "stop_loss"},
            {"command": "manage-positions", "ticker": "META", "status": "FILLED", "reason": "trailing_stop"},
        ]
        summary = compute_exit_summary(events)
        assert summary["total_exits"] == 5
        assert summary["exit_reasons"]["stop_loss"] == 2
        assert summary["exit_reasons"]["profit_target"] == 1
        assert summary["exit_reasons"]["eod"] == 1
        assert summary["exit_reasons"]["trailing_stop"] == 1


class TestComputeCounterThesisSummary:
    def test_counts_blocks_and_scaled(self):
        events = [
            {"command": "scan", "ticker": "AAPL", "status": "APPROVED",
             "counter_thesis": {"severity": "medium", "findings": [{"type": "extension"}]},
             "confidence_multiplier": 0.65},
            {"command": "scan", "ticker": "SPY", "status": "REJECTED",
             "counter_thesis_block": True,
             "counter_thesis": {"severity": "high", "findings": [{"type": "rsi_divergence"}]}},
            {"command": "scan", "ticker": "TSLA", "status": "APPROVED",
             "counter_thesis": {"severity": "low", "findings": []}},
        ]
        summary = compute_counter_thesis_summary(events)
        assert summary["total_with_findings"] == 3
        assert summary["total_blocked"] == 1
        assert summary["total_scaled"] == 1
        assert summary["block_rate_pct"] == 33.3

    def test_empty_events(self):
        summary = compute_counter_thesis_summary([])
        assert summary["total_with_findings"] == 0
        assert summary["total_blocked"] == 0


class TestComputeRiskSummary:
    def test_counts_all_risk_events(self):
        events = [
            {"command": "scan", "status": "KILL_SWITCH", "reason": "halt"},
            {"command": "paper-trade", "status": "CIRCUIT_BREAKER", "reason": "max_drawdown"},
            {"command": "paper-trade", "status": "REJECTED", "reason": "stale market data"},
            {"command": "scan", "status": "REJECTED", "reason": "stale market data"},
            {"command": "scan", "status": "VALIDATION_ERROR", "reason": "price_jump"},
        ]
        summary = compute_risk_summary(events)
        assert summary["kill_switch_triggers"] == 1
        assert summary["circuit_breaker_triggers"] == 1
        assert summary["stale_data_rejections"] == 2
        assert summary["validation_errors"] == 1
        assert summary["stale_by_command"]["paper-trade"] == 1
        assert summary["stale_by_command"]["scan"] == 1


class TestComputeTickerPerformance:
    def test_groups_by_ticker(self):
        events = [
            {"command": "scan", "ticker": "AAPL", "status": "APPROVED"},
            {"command": "scan", "ticker": "AAPL", "status": "REJECTED"},
            {"command": "paper-trade", "ticker": "AAPL", "status": "FILLED"},
            {"command": "scan", "ticker": "SPY", "status": "NO_SIGNAL"},
            {"command": "scan", "ticker": "SPY", "status": "APPROVED"},
        ]
        perf = compute_ticker_performance(events)
        assert perf["AAPL"]["total_events"] == 3
        assert perf["AAPL"]["approved_signals"] == 1
        assert perf["SPY"]["total_events"] == 2
        assert "TSLA" not in perf


class TestComputeTimeAnalysis:
    def test_computes_duration_and_distribution(self):
        events = [
            {"command": "scan", "ticker": "AAPL", "status": "APPROVED",
             "timestamp": "2026-06-22T09:30:00"},
            {"command": "scan", "ticker": "SPY", "status": "APPROVED",
             "timestamp": "2026-06-22T10:30:00"},
            {"command": "scan", "ticker": "TSLA", "status": "APPROVED",
             "timestamp": "2026-06-22T11:30:00"},
        ]
        analysis = compute_time_analysis(events)
        assert "error" not in analysis
        assert analysis["duration_hours"] >= 2.0
        assert analysis["events_per_hour"] > 0
        assert "09" in analysis["hourly_distribution"] or "9" in analysis["hourly_distribution"]
        assert "10" in analysis["hourly_distribution"]
        assert "11" in analysis["hourly_distribution"]

    def test_returns_error_for_no_timestamps(self):
        events = [
            {"command": "scan", "ticker": "AAPL", "status": "APPROVED"},
        ]
        analysis = compute_time_analysis(events)
        assert "error" in analysis


class TestGenerateRecommendations:
    def test_recommends_increasing_stale_threshold(self):
        trade_summary = {"total_fills": 10, "total_pnl": -50.0}
        signal_summary = {"rejection_reasons": {}}
        risk_summary = {"stale_data_rejections": 500, "circuit_breaker_triggers": 0}
        ct_summary = {}
        exit_summary = {}

        recs = generate_recommendations(trade_summary, signal_summary, risk_summary, ct_summary, exit_summary)
        assert any("stale" in r.lower() for r in recs)

    def test_recommends_tightening_entry_criteria(self):
        trade_summary = {"closed_trades": 10, "wins": 2, "losses": 8, "win_rate_pct": 20.0, "total_pnl": -200.0}
        signal_summary = {"rejection_reasons": {}}
        risk_summary = {"stale_data_rejections": 0, "circuit_breaker_triggers": 0}
        ct_summary = {}
        exit_summary = {"exit_reasons": {"stop_loss": 6, "profit_target": 2}}

        recs = generate_recommendations(trade_summary, signal_summary, risk_summary, ct_summary, exit_summary)
        assert any("win rate" in r.lower() for r in recs)
        assert any("stop" in r.lower() for r in recs)

    def test_no_recommendations_when_normal(self):
        trade_summary = {"closed_trades": 10, "wins": 6, "losses": 4, "win_rate_pct": 60.0, "total_pnl": 500.0, "total_fills": 20}
        signal_summary = {"rejection_reasons": {}}
        risk_summary = {"stale_data_rejections": 0, "circuit_breaker_triggers": 0}
        ct_summary = {}
        exit_summary = {"exit_reasons": {"stop_loss": 3, "profit_target": 5}}

        recs = generate_recommendations(trade_summary, signal_summary, risk_summary, ct_summary, exit_summary)
        assert any("normal" in r.lower() for r in recs)


class TestComputeBurnInReport:
    def test_computes_full_report(self, tmp_path):
        log_path = tmp_path / "decision-log.jsonl"
        log_path.write_text(
            json.dumps({"command": "scan", "ticker": "AAPL", "status": "APPROVED",
                        "confidence": 0.8, "entry": 150.0, "quality": "GREEN"}) + "\n"
            + json.dumps({"command": "paper-trade", "ticker": "AAPL", "status": "FILLED",
                          "quantity": 10, "fill_price": 151.0, "fees": 1.0}) + "\n"
            + json.dumps({"command": "manage-positions", "ticker": "AAPL", "status": "FILLED",
                          "reason": "profit_target", "quantity": 10, "fill_price": 160.0, "fees": 1.0}) + "\n",
            encoding="utf-8",
        )
        db_path = tmp_path / "state.db"

        report = compute_burn_in_report(log_path, db_path)

        assert "portfolio" in report
        assert "trades" in report
        assert "signals" in report
        assert "risk" in report
        assert "counter_thesis" in report
        assert "recommendations" in report
        assert report["trades"]["total_fills"] == 2
        assert report["signals"]["total_scans"] == 1


class TestFormatReport:
    def test_formats_human_readable(self, tmp_path):
        log_path = tmp_path / "decision-log.jsonl"
        log_path.write_text(
            json.dumps({"command": "scan", "ticker": "AAPL", "status": "APPROVED",
                        "confidence": 0.8, "quality": "GREEN"}) + "\n"
            + json.dumps({"command": "paper-trade", "ticker": "AAPL", "status": "FILLED",
                          "quantity": 10, "fill_price": 150.0, "fees": 1.0}) + "\n"
            + json.dumps({"command": "manage-positions", "ticker": "AAPL", "status": "FILLED",
                          "reason": "stop_loss", "quantity": 10, "fill_price": 145.0, "fees": 1.0}) + "\n",
            encoding="utf-8",
        )
        db_path = tmp_path / "state.db"

        report = compute_burn_in_report(log_path, db_path)
        text = format_report(report)

        assert "BURN-IN ANALYTICS REPORT" in text
        assert "PORTFOLIO" in text
        assert "TRADES" in text
        assert "SIGNALS" in text
        assert "RISK MANAGEMENT" in text
        assert "RECOMMENDATIONS" in text
        assert "AAPL" in text
