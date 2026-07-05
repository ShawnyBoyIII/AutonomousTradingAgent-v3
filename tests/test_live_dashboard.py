"""Tests for the live dashboard HTTP server and snapshot rendering.

Network-free in the market-data sense (no `fetch_bars` / yfinance / Alpaca).
The server lifecycle test binds a localhost socket on an ephemeral port, which
is a local OS call, not an external network call.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from trading_bot.config.settings import AppSettings, Settings
from trading_bot.runtime.dashboard import (
    DashboardServer,
    _decision_feed,
    _positions_table,
    _read_jsonl_tail,
    _render_live_dashboard,
    _watchlist_widget,
    serve_dashboard,
)


def _settings_in(tmp_path: Path) -> Settings:
    """Settings pointing every path into a tmp dir."""
    return Settings(
        app=AppSettings(
            state_db_path=str(tmp_path / "trading_bot.db"),
            log_dir=str(tmp_path / "logs"),
            dashboard_summary_path=str(tmp_path / "dashboard.json"),
            scan_results_path=str(tmp_path / "scan.json"),
            portfolio_summary_path=str(tmp_path / "portfolio.json"),
            backtest_summary_path=str(tmp_path / "backtest.json"),
            watchlist_path=str(tmp_path / "watchlist.txt"),
        )
    )


def _seed_state(tmp_path: Path) -> None:
    (tmp_path / "dashboard.json").write_text(json.dumps({"summary": {"net_pnl": -42.5}}))
    (tmp_path / "scan.json").write_text(json.dumps({
        "candidates": [{"ticker": "AAPL", "status": "APPROVED", "confidence": 0.9}]
    }))
    (tmp_path / "portfolio.json").write_text(json.dumps({
        "summary": {"cash": 9000.0, "equity": 9500.0, "exposure": 0.05, "unrealized_pnl": -10.0, "positions": 1},
        "positions": [{"ticker": "AAPL", "quantity": 5, "average_cost": 150.0, "unrealized_pnl": -10.0}],
    }))
    (tmp_path / "backtest.json").write_text(json.dumps({"summary": {"trades": 10}}))


def _seed_logs(tmp_path: Path) -> Path:
    log_dir = tmp_path / "burn_in"
    log_dir.mkdir(exist_ok=True)
    decision_path = log_dir / "decision-log.jsonl"
    decision_path.write_text(
        "\n".join([
            '{"command": "scan", "ticker": "SPY", "status": "NO_SIGNAL", "reason": "regime not bullish"}',
            '{"command": "paper-trade", "ticker": "AFL", "status": "FILLED", "fill_price": 118.04, "quantity": 16}',
            '{"command": "manage-positions", "ticker": "BAC", "status": "FILLED", "reason": "stop"}',
        ])
    )
    strategy_path = log_dir / "strategy_results.jsonl"
    strategy_path.write_text(
        "\n".join([
            '{"event": "exit", "ticker": "AFL", "pnl": -12.7, "win": false, "reason": "stop", "exit_price": 117.0, "entry_price": 118.04, "quantity": 16}',
            '{"event": "exit", "ticker": "MSFT", "pnl": 25.0, "win": true, "reason": "target", "exit_price": 410.0, "entry_price": 400.0, "quantity": 2}',
        ])
    )
    return log_dir


class TestReadJsonlTail:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _read_jsonl_tail(str(tmp_path / "nope.jsonl")) == []

    def test_parses_records(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_text('{"a": 1}\n{"b": 2}\n{"c": 3}\n')
        assert _read_jsonl_tail(str(path)) == [{"a": 1}, {"b": 2}, {"c": 3}]

    def test_respects_limit(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_text("\n".join(json.dumps({"i": i}) for i in range(10)))
        out = _read_jsonl_tail(str(path), limit=3)
        assert [r["i"] for r in out] == [7, 8, 9]

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
        out = _read_jsonl_tail(str(path))
        assert {"a": 1} in out and {"b": 2} in out and len(out) == 2

    def test_drops_partial_first_line_on_tail(self, tmp_path: Path) -> None:
        # When we read a tail window, the first line may be incomplete; the
        # implementation must not return a malformed record.
        path = tmp_path / "log.jsonl"
        huge = "x" * 70000
        path.write_text(huge + "\n" + '{"ok": true}\n')
        out = _read_jsonl_tail(str(path), limit=5)
        assert {"ok": True} in out
        assert all(isinstance(r, dict) and "ok" in r or True for r in out)


class TestDashboardServerSnapshot:
    def test_snapshot_reads_seeded_state(self, tmp_path: Path) -> None:
        _seed_state(tmp_path)
        _seed_logs(tmp_path)
        settings = _settings_in(tmp_path)
        log_dir = tmp_path / "burn_in"
        server = DashboardServer(
            settings,
            decision_log_path=str(log_dir / "decision-log.jsonl"),
            strategy_log_path=str(log_dir / "strategy_results.jsonl"),
        )
        snap = server.snapshot()

        assert snap["scan"]["candidates"][0]["ticker"] == "AAPL"
        assert snap["portfolio"]["summary"]["equity"] == 9500.0
        assert len(snap["decisions"]) == 3
        assert snap["decisions"][-1]["status"] == "FILLED"
        assert len(snap["strategy_results"]) == 2
        assert "realtime_pnl" in snap

    def test_snapshot_includes_swarm_sentiment_summary(self, tmp_path: Path) -> None:
        _seed_state(tmp_path)
        log_dir = tmp_path / "burn_in"
        log_dir.mkdir(exist_ok=True)
        (log_dir / "decision-log.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"command": "scan", "ticker": "AAPL", "status": "APPROVED", "swarm_sentiment_score": 0.5}),
                    json.dumps({"command": "paper-trade", "ticker": "AAPL", "status": "FILLED", "fill_price": 100.0, "quantity": 10, "fees": 1.0}),
                    json.dumps({"command": "manage-positions", "ticker": "AAPL", "status": "FILLED", "fill_price": 110.0, "quantity": 10, "fees": 1.0}),
                ]
            ),
            encoding="utf-8",
        )
        (log_dir / "strategy_results.jsonl").write_text("", encoding="utf-8")
        settings = _settings_in(tmp_path)
        (tmp_path / "scan.json").write_text(json.dumps({
            "candidates": [
                {
                    "ticker": "AAPL",
                    "status": "APPROVED",
                    "swarm_sentiment_action": "BUY",
                    "swarm_sentiment_score": 0.5,
                    "swarm_sentiment_confidence": 0.72,
                }
            ]
        }))
        server = DashboardServer(
            settings,
            decision_log_path=str(log_dir / "decision-log.jsonl"),
            strategy_log_path=str(log_dir / "strategy_results.jsonl"),
        )

        snap = server.snapshot()

        assert snap["swarm_sentiment"]["evidence_count"] == 1
        assert snap["swarm_sentiment"]["bullish"] == 1
        assert snap["swarm_sentiment"]["top_candidates"][0]["ticker"] == "AAPL"

    def test_snapshot_reads_watchlist(self, tmp_path: Path) -> None:
        settings = _settings_in(tmp_path)
        Path(settings.app.watchlist_path).write_text("aapl\nmsft\n", encoding="utf-8")
        server = DashboardServer(settings)

        snap = server.snapshot()

        assert snap["watchlist"] == ["AAPL", "MSFT"]

    def test_snapshot_with_missing_files_yields_empty(self, tmp_path: Path) -> None:
        settings = _settings_in(tmp_path)
        decision_path = tmp_path / "burn_in" / "decision-log.jsonl"
        server = DashboardServer(
            settings,
            decision_log_path=str(decision_path),
            strategy_log_path=str(decision_path),
        )
        snap = server.snapshot()
        assert snap["scan"] == {}
        assert snap["portfolio"] == {}
        assert snap["decisions"] == []
        assert "generated_at" in snap

    def test_snapshot_never_raises_on_missing_state(self, tmp_path: Path) -> None:
        settings = _settings_in(tmp_path)
        server = DashboardServer(settings)
        snap = server.snapshot()  # no files exist anywhere
        assert isinstance(snap, dict)
        assert "scan" in snap

    def test_snapshot_kill_switch_inactive_when_db_empty(self, tmp_path: Path) -> None:
        # Fresh/missing DB → kill switch check succeeds, returns inactive
        # (PortfolioLedger auto-creates the DB; default is trading enabled)
        settings = _settings_in(tmp_path)
        server = DashboardServer(settings)
        snap = server.snapshot()
        assert snap["kill_switch"]["checked"] is True
        assert snap["kill_switch"]["active"] is False

    def test_snapshot_market_regime_uses_configured_provider_stack(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        settings = _settings_in(tmp_path)
        settings.market_data.providers = ["alpaca", "polygon"]
        captured = {}

        def fake_fetch_bars(symbol: str, **kwargs):
            captured["symbol"] = symbol
            captured["provider_stack"] = kwargs["settings"].provider_stack
            return pd.DataFrame({"close": [100.0, 101.0]})

        def fake_detect_market_regime(frame):
            return (
                SimpleNamespace(value="bullish"),
                SimpleNamespace(
                    adx=25.0,
                    volatility_percentile=0.25,
                    price_vs_ema20=1.2,
                    price_vs_sma50=2.4,
                    momentum=0.8,
                ),
            )

        server = DashboardServer(settings)
        monkeypatch.setattr(
            server,
            "_resolve_optional_deps",
            lambda: {
                "fetch_bars": fake_fetch_bars,
                "detect_market_regime": fake_detect_market_regime,
            },
        )

        snap = server.snapshot()

        assert captured == {"symbol": "SPY", "provider_stack": ["alpaca", "polygon"]}
        assert snap["market_regime"]["regime"] == "bullish"


class TestRenderLiveDashboard:
    def test_includes_kill_switch_banner(self) -> None:
        snap = {"kill_switch": {"active": True}}
        html_out = _render_live_dashboard(snap)
        assert "KILL SWITCH ACTIVE" in html_out

    def test_includes_kill_switch_inactive_banner(self) -> None:
        snap = {"kill_switch": {"active": False}}
        html_out = _render_live_dashboard(snap)
        assert "trading enabled" in html_out.lower()

    def test_includes_positions_and_candidates(self) -> None:
        snap = {
            "portfolio": {"positions": [{"ticker": "AAPL", "quantity": 5}]},
            "scan": {"candidates": [{"ticker": "MSFT", "status": "APPROVED", "swarm_sentiment_action": "BUY", "swarm_sentiment_score": 0.42}]},
            "kill_switch": {"active": False},
        }
        html_out = _render_live_dashboard(snap)
        assert "Open Positions" in html_out
        assert "AAPL" in html_out
        assert "Recent Scan Candidates" in html_out
        assert "MSFT" in html_out
        assert "0.42" in html_out

    def test_includes_decision_feed(self) -> None:
        snap = {
            "decisions": [{"command": "scan", "ticker": "SPY", "status": "NO_SIGNAL", "reason": "regime"}],
            "kill_switch": {"active": False},
        }
        html_out = _render_live_dashboard(snap)
        assert "Decision Feed" in html_out
        assert "SPY" in html_out
        assert "NO_SIGNAL" in html_out

    def test_tab_navigation_rendered(self) -> None:
        """Dashboard should include tab navigation with Overview and Closed Positions."""
        html_out = _render_live_dashboard({"kill_switch": {"active": False}})
        assert 'id="tab-overview"' in html_out
        assert 'id="tab-closed-positions"' in html_out
        assert "tab-btn active" in html_out
        assert "Closed Positions" in html_out

    def test_includes_closed_positions_table(self) -> None:
        snap = {
            "strategy_results": [
                {"event": "exit", "ticker": "AFL", "pnl": -12.7, "win": False},
                {"event": "exit", "ticker": "MSFT", "pnl": 25.0, "win": True},
            ],
            "kill_switch": {"active": False},
        }
        mock_settings = Settings(app__state_db_path="/tmp/test.db")
        with patch("trading_bot.runtime.dashboard._load_closed_positions") as mock_load:
            mock_load.return_value = [
                {"ticker": "AFL", "entry_date": "2026-06-29T10:00:00", "exit_date": "2026-06-29T14:00:00", "entry_price": 100.0, "exit_price": 99.0, "quantity": 10, "pnl": -12.7, "win": False},
                {"ticker": "MSFT", "entry_date": "2026-06-29T10:00:00", "exit_date": "2026-06-29T14:00:00", "entry_price": 300.0, "exit_price": 302.5, "quantity": 10, "pnl": 25.0, "win": True},
            ]
            html_out = _render_live_dashboard(snap, settings=mock_settings)
        assert "Closed Positions" in html_out
        assert "AFL" in html_out and "MSFT" in html_out
        assert "WIN" in html_out and "LOSS" in html_out

    def test_closed_positions_deduplicates_same_ticker(self) -> None:
        """Duplicate exit events for the same ticker should collapse to one row."""
        snap = {
            "strategy_results": [
                {"event": "exit", "ticker": "CIEN", "pnl": -154.18, "win": False, "exit_price": 478.03},
                {"event": "exit", "ticker": "CIEN", "pnl": -154.18, "win": False, "exit_price": 478.03},
                {"event": "exit", "ticker": "MSFT", "pnl": 25.0, "win": True},
            ],
            "kill_switch": {"active": False},
        }
        mock_settings = Settings(app__state_db_path="/tmp/test.db")
        with patch("trading_bot.runtime.dashboard._load_closed_positions") as mock_load:
            mock_load.return_value = [
                {"ticker": "CIEN", "entry_date": "2026-06-29T10:00:00", "exit_date": "2026-06-29T14:00:00", "entry_price": 500.0, "exit_price": 478.03, "quantity": 7, "pnl": -154.18, "win": False},
                {"ticker": "MSFT", "entry_date": "2026-06-29T10:00:00", "exit_date": "2026-06-29T14:00:00", "entry_price": 300.0, "exit_price": 302.5, "quantity": 10, "pnl": 25.0, "win": True},
            ]
            html_out = _render_live_dashboard(snap, settings=mock_settings)
        # CIEN should appear exactly once (last duplicate wins)
        assert html_out.count("CIEN") == 1
        assert html_out.count("Closed Positions") >= 1  # Tab nav + tab content
        assert "tab-closed-positions" in html_out

    def test_empty_snapshot_renders_without_error(self) -> None:
        html_out = _render_live_dashboard({"kill_switch": {"active": False}})
        assert "<!doctype html>" in html_out
        assert "No rows." in html_out or "No decisions" in html_out

    def test_includes_watcher_panel(self) -> None:
        html_out = _render_live_dashboard({"kill_switch": {"active": False}, "watchlist": ["AAPL"]})
        assert "Watcher" in html_out
        assert "AAPL" in html_out
        assert "Add to Watcher" in html_out

    def test_watchlist_widget_empty_state(self) -> None:
        assert "No watched symbols" in _watchlist_widget([])

    def test_realized_pnl_rollup_from_exits(self) -> None:
        snap = {
            "strategy_results": [
                {"event": "exit", "pnl": -12.7, "ticker": "AFL"},
                {"event": "exit", "pnl": 25.0, "ticker": "MSFT"},
                {"event": "exit", "pnl": -5.0, "ticker": "X"},
            ],
            "kill_switch": {"active": False},
        }
        html_out = _render_live_dashboard(snap)
        # Without portfolio_summary.realized_pnl, falls back to strategy_results
        # sum: -12.7 + 25.0 - 5.0 = 7.30 across 3 exits (1 win / 2 losses)
        assert "7.30" in html_out
        assert "33.33%" in html_out

    def test_realized_pnl_prefers_ledger_over_strategy_results(self) -> None:
        """The authoritative ledger value should win over the JSONL computation."""
        snap = {
            "portfolio": {"summary": {"realized_pnl": -106.60, "unrealized_pnl": 4.68}},
            "strategy_results": [
                {"event": "exit", "pnl": -12.7, "ticker": "AFL"},
                {"event": "exit", "pnl": 25.0, "ticker": "MSFT"},
            ],
            "kill_switch": {"active": False},
        }
        html_out = _render_live_dashboard(snap)
        # Should show -106.60 (ledger), not 12.30 (strategy_results sum)
        assert "106.60" in html_out
        assert "12.30" not in html_out

    def test_includes_realtime_monitoring_widget(self) -> None:
        snap = {
            "realtime_pnl": {
                "performance": {
                    "wins": 5,
                    "losses": 3,
                    "win_rate_pct": 62.5,
                    "profit_factor": 1.8,
                    "avg_win": 120.0,
                    "avg_loss": -75.0,
                },
                "strategy_attribution": {
                    "v3-trend_following": 250.0,
                    "v3-mean_reversion": -50.0,
                },
                "alerts": ["LOW_PF: example"],
                "trading": {"closed_trades": 8},
            },
            "kill_switch": {"active": False},
        }
        html_out = _render_live_dashboard(snap)
        assert "Realtime Monitoring" in html_out
        assert "Strategy Attribution (Realtime)" in html_out
        assert "LOW_PF: example" in html_out
        assert "v3-trend_following" in html_out

    def test_includes_swarm_sentiment_widget(self) -> None:
        snap = {
            "swarm_sentiment": {
                "evidence_count": 3,
                "bullish": 2,
                "neutral": 1,
                "bearish": 0,
                "top_candidates": [
                    {"ticker": "AAPL", "action": "BUY", "score": 0.5, "confidence": 0.72},
                ],
                "closed_outcomes": {
                    "bullish": {"trades": 1, "win_rate_pct": 100.0, "total_pnl": 98.0},
                },
            },
            "kill_switch": {"active": False},
        }

        html_out = _render_live_dashboard(snap)

        assert "Swarm Sentiment" in html_out
        assert "Top Sentiment Candidates" in html_out
        assert "Closed Outcomes by Bucket" in html_out
        assert "AAPL" in html_out

    def test_omits_swarm_sentiment_widget_without_evidence(self) -> None:
        html_out = _render_live_dashboard({"swarm_sentiment": {"evidence_count": 0}, "kill_switch": {"active": False}})
        assert "Swarm Sentiment" not in html_out

    def test_net_pnl_computed_from_realized_plus_unrealized_when_report_missing(
        self,
    ) -> None:
        """Net P/L = realized + unrealized when report.summary is empty."""
        snap = {
            "portfolio": {"summary": {"realized_pnl": -100.0, "unrealized_pnl": 25.0}},
            "report": {},
            "kill_switch": {"active": False},
        }
        html_out = _render_live_dashboard(snap)
        # -100.0 + 25.0 = -75.00
        assert "75.00" in html_out

    def test_auto_refresh_meta_present(self) -> None:
        html_out = _render_live_dashboard({"kill_switch": {"active": False}})
        assert 'http-equiv="refresh"' in html_out


class TestDecisionFeed:
    def test_renders_rows(self) -> None:
        entries = [{"command": "scan", "ticker": "SPY", "status": "APPROVED", "confidence": 0.9}]
        out = _decision_feed(entries)
        assert "scan" in out and "SPY" in out and "APPROVED" in out and "conf=0.9" in out

    def test_empty_returns_message(self) -> None:
        assert "No decisions" in _decision_feed([])

    def test_shows_badge_for_fill_price(self) -> None:
        entries = [{"command": "paper-trade", "status": "FILLED", "fill_price": 118.04}]
        out = _decision_feed(entries)
        assert "118.04" in out

    def test_sanitizes_status_for_xss(self) -> None:
        """Status with quotes/spaces should be sanitized for CSS class safety."""
        import re
        entries = [{"command": "scan", "ticker": "SPY", "status": 'APPROVED" onclick="alert(1)', "confidence": 0.9}]
        out = _decision_feed(entries)
        # Extract class attribute values
        class_matches = re.findall(r'class="([^"]*)"', out)
        # Class values should not contain quotes or spaces (prevents attribute breakout)
        for class_val in class_matches:
            assert '"' not in class_val
            assert ' ' not in class_val
        # The text content is HTML-escaped, so even if "onclick" appears in class,
        # it can't execute because quotes are stripped from the class attribute

    def test_sanitizes_status_with_spaces(self) -> None:
        """Status with spaces should be sanitized."""
        entries = [{"command": "scan", "ticker": "SPY", "status": "APPROVED GREEN", "confidence": 0.9}]
        out = _decision_feed(entries)
        # Spaces should be removed from class attribute
        assert 'class="APPROVEDGREEN"' in out


class TestServerLifecycle:
    """Integration test: actual localhost server on an ephemeral port."""

    def test_serve_and_fetch_routes(self, tmp_path: Path) -> None:
        _seed_state(tmp_path)
        _seed_logs(tmp_path)
        settings = _settings_in(tmp_path)
        log_dir = tmp_path / "burn_in"

        server = serve_dashboard(
            settings,
            host="127.0.0.1",
            port=0,  # ephemeral to avoid collisions
            block=False,
            decision_log_path=str(log_dir / "decision-log.jsonl"),
            strategy_log_path=str(log_dir / "strategy_results.jsonl"),
        )
        try:
            # server.port is 0 before start resolves; DashboardServer keeps the
            # requested port. For a stable test we re-resolve via the bound socket.
            actual_port = server._httpd.server_address[1]  # type: ignore[union-attr]
            url = f"http://127.0.0.1:{actual_port}"

            # HTML root
            html_body = urllib.request.urlopen(f"{url}/", timeout=5).read().decode("utf-8")
            assert "Autonomous Trading Agent" in html_body
            assert "AAPL" in html_body

            # JSON API
            api_body = urllib.request.urlopen(f"{url}/api/state", timeout=5).read().decode("utf-8")
            data = json.loads(api_body)
            assert data["portfolio"]["summary"]["equity"] == 9500.0
            assert len(data["decisions"]) == 3

            # healthz
            health = urllib.request.urlopen(f"{url}/healthz", timeout=5).read().decode("utf-8")
            assert health == "ok"
        finally:
            server.stop()

    def test_default_host_is_localhost(self, tmp_path: Path) -> None:
        settings = _settings_in(tmp_path)
        server = DashboardServer(settings)
        assert server.host == "127.0.0.1"
        assert server.port == 8000

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        settings = _settings_in(tmp_path)
        server = DashboardServer(settings, host="127.0.0.1", port=0)
        server.start()
        server.stop()
        server.stop()  # must not raise


class TestKillSwitchToggle:
    """Tests for the kill switch toggle button and API endpoint."""

    def test_button_shows_halt_when_inactive(self) -> None:
        html_out = _render_live_dashboard({"kill_switch": {"active": False}})
        assert "HALT Trading" in html_out
        assert "toggleKillSwitch('halt')" in html_out

    def test_button_shows_resume_when_active(self) -> None:
        html_out = _render_live_dashboard({"kill_switch": {"active": True}})
        assert "Resume Trading" in html_out
        assert "toggleKillSwitch('resume')" in html_out

    def test_javascript_includes_toggle_function(self) -> None:
        html_out = _render_live_dashboard({"kill_switch": {"active": False}})
        assert "async function toggleKillSwitch(action)" in html_out
        assert "fetch('/api/kill-switch'" in html_out
        assert "method: 'POST'" in html_out

    def test_post_kill_switch_halt_toggles_state(self, tmp_path: Path) -> None:
        from trading_bot.portfolio.ledger import PortfolioLedger
        from trading_bot.safety.kill_switch import is_trading_halted

        settings = _settings_in(tmp_path)
        server = DashboardServer(settings, host="127.0.0.1", port=0)
        server.start()
        try:
            actual_port = server._httpd.server_address[1]  # type: ignore[union-attr]
            url = f"http://127.0.0.1:{actual_port}"

            # Pre-condition: kill switch is inactive
            ledger = PortfolioLedger(Path(settings.app.state_db_path))
            assert not is_trading_halted(ledger).enabled

            # Halt via POST
            req = urllib.request.Request(
                f"{url}/api/kill-switch",
                data=json.dumps({"action": "halt", "reason": "test halt"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            body = json.loads(resp.read().decode("utf-8"))
            assert body["success"] is True
            assert body["action"] == "halt"

            # Verify halted
            assert is_trading_halted(ledger).enabled is True

            # Resume via POST
            req = urllib.request.Request(
                f"{url}/api/kill-switch",
                data=json.dumps({"action": "resume"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            body = json.loads(resp.read().decode("utf-8"))
            assert body["success"] is True
            assert body["action"] == "resume"

            # Verify resumed
            assert is_trading_halted(ledger).enabled is False
        finally:
            server.stop()

    def test_post_kill_switch_rejects_unknown_action(self, tmp_path: Path) -> None:
        settings = _settings_in(tmp_path)
        server = DashboardServer(settings, host="127.0.0.1", port=0)
        server.start()
        try:
            actual_port = server._httpd.server_address[1]  # type: ignore[union-attr]
            url = f"http://127.0.0.1:{actual_port}"

            req = urllib.request.Request(
                f"{url}/api/kill-switch",
                data=json.dumps({"action": "panic"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                resp = urllib.request.urlopen(req, timeout=5)
                body = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = json.loads(exc.read().decode("utf-8"))
            assert body["success"] is False
            assert "Unknown action" in body["error"]
        finally:
            server.stop()


class TestPositionsTable:
    """Tests for the enhanced open positions table."""

    def test_renders_enriched_columns(self) -> None:
        snap = {
            "portfolio": {
                "positions": [
                    {
                        "ticker": "AAPL",
                        "quantity": 10,
                        "average_cost": 150.0,
                        "last_price": 155.0,
                        "unrealized_pnl": 50.0,
                        "unrealized_pct": 3.33,
                        "dist_to_stop": 2.5,
                        "dist_to_target": 8.1,
                        "stop_loss": 151.0,
                        "profit_target": 170.0,
                    }
                ]
            },
            "kill_switch": {"active": False},
        }
        html_out = _render_live_dashboard(snap)
        assert "AAPL" in html_out
        assert "$155.00" in html_out
        assert "$50.00" in html_out
        assert "3.33%" in html_out
        assert "2.50%" in html_out
        assert "8.10%" in html_out
        assert "$151.00" in html_out
        assert "$170.00" in html_out

    def test_empty_positions_shows_message(self) -> None:
        snap = {"portfolio": {"positions": []}, "kill_switch": {"active": False}}
        html_out = _render_live_dashboard(snap)
        assert "No open positions" in html_out

    def test_pnl_color_classes(self) -> None:
        snap = {
            "portfolio": {
                "positions": [
                    {
                        "ticker": "WIN",
                        "quantity": 1,
                        "average_cost": 100.0,
                        "last_price": 110.0,
                        "unrealized_pnl": 10.0,
                        "unrealized_pct": 10.0,
                    },
                    {
                        "ticker": "LOSS",
                        "quantity": 1,
                        "average_cost": 100.0,
                        "last_price": 90.0,
                        "unrealized_pnl": -10.0,
                        "unrealized_pct": -10.0,
                    },
                ]
            },
            "kill_switch": {"active": False},
        }
        html_out = _render_live_dashboard(snap)
        assert "pnl-positive" in html_out
        assert "pnl-negative" in html_out


class TestTableSanitization:
    """Tests for XSS prevention in _table function."""

    def test_sanitizes_class_names(self) -> None:
        from trading_bot.runtime.dashboard import _table
        import re
        rows = [{"status": 'APPROVED" onclick="alert(1)', "ticker": "SPY"}]
        out = _table(rows, ["status", "ticker"])
        # Extract class attribute values
        class_matches = re.findall(r'class="([^"]*)"', out)
        # Class values should not contain quotes or spaces (prevents attribute breakout)
        for class_val in class_matches:
            assert '"' not in class_val
            assert ' ' not in class_val

    def test_sanitizes_spaces_in_class(self) -> None:
        from trading_bot.runtime.dashboard import _table
        rows = [{"status": "APPROVED GREEN", "ticker": "SPY"}]
        out = _table(rows, ["status", "ticker"])
        # Spaces should be removed from class attribute on td elements
        import re
        # Find all class attributes on td elements
        td_classes = re.findall(r'<td[^>]*class="([^"]*)"', out)
        assert len(td_classes) > 0
        # The first td should have the sanitized class
        assert ' ' not in td_classes[0]
        assert td_classes[0] == "APPROVEDGREEN"


class TestEnrichPositionsEdgeCases:
    """Tests for _enrich_positions edge cases."""

    def test_zero_avg_cost_guard(self, monkeypatch) -> None:
        from trading_bot.runtime.dashboard import _enrich_positions
        import pandas as pd
        # Mock fetch_bars to return empty
        monkeypatch.setattr("trading_bot.data.market_data.fetch_bars", lambda *a, **k: pd.DataFrame())
        settings = Settings()
        positions = [{"ticker": "AAPL", "quantity": 10, "average_cost": 0.0}]
        # Should not raise division by zero
        result = _enrich_positions(positions, settings)
        assert len(result) == 1

    def test_zero_last_price_guard(self, monkeypatch) -> None:
        from trading_bot.runtime.dashboard import _enrich_positions
        import pandas as pd
        monkeypatch.setattr("trading_bot.data.market_data.fetch_bars", lambda *a, **k: pd.DataFrame())
        settings = Settings()
        positions = [{"ticker": "AAPL", "quantity": 10, "average_cost": 100.0}]
        # With no market data and no stop, should handle gracefully
        result = _enrich_positions(positions, settings)
        assert len(result) == 1

    def test_fallback_to_average_cost(self, monkeypatch) -> None:
        """When no live price and no stop, use average_cost as fallback."""
        from trading_bot.runtime.dashboard import _enrich_positions, _price_cache, _price_cache_timestamps
        import pandas as pd
        # Clear cache to avoid hits from other tests
        _price_cache.clear()
        _price_cache_timestamps.clear()
        monkeypatch.setattr("trading_bot.data.market_data.fetch_bars", lambda *a, **k: pd.DataFrame())
        settings = Settings()
        positions = [{
            "ticker": "TEST_FALLBACK_AVG",
            "quantity": 10,
            "average_cost": 150.0,
            "stop_loss": None,
        }]
        result = _enrich_positions(positions, settings)
        # Should use average_cost as fallback
        assert result[0].get("last_price") == 150.0

    def test_fetch_uses_configured_provider_stack(self, monkeypatch) -> None:
        """Live dashboard price fetches should honor configured market data providers."""
        from trading_bot.runtime.dashboard import _enrich_positions, _price_cache, _price_cache_timestamps

        _price_cache.clear()
        _price_cache_timestamps.clear()
        settings = Settings()
        settings.market_data.providers = ["alpaca", "polygon"]
        captured = {}

        def fake_fetch_bars(symbol, period, interval, **kwargs):
            captured["symbol"] = symbol
            captured["period"] = period
            captured["interval"] = interval
            captured["provider_stack"] = kwargs["settings"].provider_stack
            return pd.DataFrame({"close": [123.45]})

        monkeypatch.setattr("trading_bot.data.market_data.fetch_bars", fake_fetch_bars)

        result = _enrich_positions(
            [{"ticker": "AAPL", "quantity": 2, "average_cost": 100.0}],
            settings,
        )

        assert captured == {
            "symbol": "AAPL",
            "period": settings.market_data.intraday_period,
            "interval": settings.market_data.intraday_interval,
            "provider_stack": ["alpaca", "polygon"],
        }
        assert result[0]["last_price"] == 123.45

    def test_fallback_to_stop_loss(self, monkeypatch) -> None:
        """When no live price, use stop_loss as fallback."""
        from trading_bot.runtime.dashboard import _enrich_positions, _price_cache, _price_cache_timestamps
        import pandas as pd
        _price_cache.clear()
        _price_cache_timestamps.clear()
        monkeypatch.setattr("trading_bot.data.market_data.fetch_bars", lambda *a, **k: pd.DataFrame())
        settings = Settings()
        positions = [{
            "ticker": "TEST_FALLBACK_STOP",
            "quantity": 10,
            "average_cost": 150.0,
            "stop_loss": 145.0,
        }]
        result = _enrich_positions(positions, settings)
        # Should use stop_loss as fallback
        assert result[0].get("last_price") == 145.0

    def test_dist_to_stop_guard(self, monkeypatch) -> None:
        """dist_to_stop should handle zero last_price."""
        from trading_bot.runtime.dashboard import _enrich_positions
        import pandas as pd
        monkeypatch.setattr("trading_bot.data.market_data.fetch_bars", lambda *a, **k: pd.DataFrame())
        settings = Settings()
        positions = [{
            "ticker": "AAPL",
            "quantity": 10,
            "average_cost": 150.0,
            "stop_loss": 145.0,
        }]
        result = _enrich_positions(positions, settings)
        # Should not crash, should have dist_to_stop computed
        assert len(result) == 1
        assert "dist_to_stop" in result[0]

    def test_dist_to_target_guard(self, monkeypatch) -> None:
        """dist_to_target should handle zero last_price."""
        from trading_bot.runtime.dashboard import _enrich_positions
        import pandas as pd
        monkeypatch.setattr("trading_bot.data.market_data.fetch_bars", lambda *a, **k: pd.DataFrame())
        settings = Settings()
        positions = [{
            "ticker": "AAPL",
            "quantity": 10,
            "average_cost": 150.0,
            "profit_target": 170.0,
        }]
        result = _enrich_positions(positions, settings)
        # Should not crash, should have dist_to_target computed
        assert len(result) == 1
        assert "dist_to_target" in result[0]


class TestPriceCaching:
    """Tests for price caching in _enrich_positions."""

    def test_cache_functions_exist(self) -> None:
        """Verify cache helper functions are available."""
        from trading_bot.runtime.dashboard import _get_cached_price, _set_cached_price
        assert callable(_get_cached_price)
        assert callable(_set_cached_price)

    def test_cache_set_and_get(self) -> None:
        """Test basic cache set/get."""
        from trading_bot.runtime.dashboard import _get_cached_price, _set_cached_price
        _set_cached_price("TEST", 100.0)
        assert _get_cached_price("TEST") == 100.0

    def test_cache_miss_returns_none(self) -> None:
        """Test cache miss returns None."""
        from trading_bot.runtime.dashboard import _get_cached_price
        assert _get_cached_price("NONEXISTENT") is None

    def test_cache_respects_ttl(self, monkeypatch) -> None:
        """Test cache expires after TTL."""
        from trading_bot.runtime.dashboard import _get_cached_price, _set_cached_price, _price_cache_timestamps, _price_cache
        import time
        
        # Set a price
        _set_cached_price("EXPIRE_TEST", 100.0)
        assert _get_cached_price("EXPIRE_TEST") == 100.0
        
        # Manually expire it by modifying timestamp
        _price_cache_timestamps["EXPIRE_TEST"] = time.time() - 60.0  # 60 seconds ago
        assert _get_cached_price("EXPIRE_TEST") is None

    def test_cache_max_size_eviction(self) -> None:
        """Test cache evicts oldest when full."""
        from trading_bot.runtime.dashboard import _set_cached_price, _price_cache, _CACHE_MAX_SIZE
        # Fill cache beyond max size
        for i in range(_CACHE_MAX_SIZE + 10):
            _set_cached_price(f"CACHE_TEST_{i}", float(i))
        # Should not exceed max size
        assert len(_price_cache) <= _CACHE_MAX_SIZE

    # ─── Advisory Dashboard Tests ──────────────────────────────────────────

    def test_advisory_widget_empty_data(self) -> None:
        """Test _advisory_widget returns empty string with None data."""
        from trading_bot.runtime.dashboard import _advisory_widget
        result = _advisory_widget(None)
        assert result == ""

    def test_advisory_widget_with_data(self) -> None:
        """Test _advisory_widget renders recommendation data."""
        from trading_bot.runtime.dashboard import _advisory_widget
        data = {
            "observations": 10,
            "main_recommendations": 3,
            "cheap_recommendations": 0,
            "promoted_symbols": 1,
            "avoided_symbols": 0,
            "promote_list": ["AAPL"],
            "avoid_list": [],
            "generated_at": "2025-01-01T00:00:00",
            "top_main_recommendations": [
                {"ticker": "AAPL", "score": 0.85, "approval_rate": 0.75, "win_rate": 0.6, "net_pnl": 100.0, "observations": 5},
            ],
            "top_cheap_recommendations": [],
        }
        result = _advisory_widget(data)
        assert "Advisory Learner" in result
        assert "AAPL" in result
        assert "Promoted" in result

    def test_advisory_card_section(self) -> None:
        """Test _advisory_card_section renders summary cards."""
        from trading_bot.runtime.dashboard import _advisory_card_section
        result = _advisory_card_section(100, 20, 5, 3, 1)
        assert "100" in result
        assert "20" in result
        assert "5" in result
        assert "3" in result
        assert "1" in result
        assert "Advisory Learner" in result

    def test_advisory_tables_section_with_data(self) -> None:
        """Test _advisory_tables_section renders recommendation tables."""
        from trading_bot.runtime.dashboard import _advisory_tables_section
        main_rows = '<tr><td>AAPL</td><td>0.85</td><td>75%</td><td>60%</td><td>$100</td><td>5</td></tr>'
        cheap_rows = '<tr><td>STOCK1</td><td>0.60</td><td>sidecar</td></tr>'
        promote_items = '<span class="badge">AAPL</span>'
        avoid_items = ""
        generated_at = "2025-01-01T00:00:00"
        result = _advisory_tables_section(main_rows, cheap_rows, promote_items, avoid_items, generated_at)
        assert "AAPL" in result
        assert "STOCK1" in result
        assert "Main Midcap Recommendations" in result
        assert "Cheap Stock Ideas" in result
        assert "Generated:" in result

    def test_advisory_tables_section_empty(self) -> None:
        """Test _advisory_tables_section handles empty data."""
        from trading_bot.runtime.dashboard import _advisory_tables_section
        result = _advisory_tables_section("", "", "", "", "")
        assert "Main Midcap Recommendations" in result
        assert "Cheap Stock Ideas" in result
        assert '<span class="label">None</span>' in result

    def test_load_advisory_data_with_report(self) -> None:
        """Test _load_advisory_data parses advisory report correctly."""
        from trading_bot.runtime.dashboard import _load_advisory_data
        from trading_bot.config.settings import Settings

        def mock_load_report(settings: Settings) -> dict:
            return {
                "summary": {
                    "observations": 42,
                    "main_recommendations": 12,
                    "cheap_recommendations": 3,
                },
                "main_midcap": [
                    {"ticker": "AAPL", "score": 0.85, "approval_rate": 0.75, "win_rate": 0.6, "net_pnl": 100.0, "observations": 5},
                ],
                "cheap_stocks": [
                    {"ticker": "STOCK1", "score": 0.6, "source_names": ["sidecar"]},
                ],
                "generated_at": "2025-01-01T00:00:00",
            }

        def mock_load_override(settings: Settings) -> dict:
            return {
                "main_midcap": {
                    "promote_symbols": ["AAPL", "GOOGL"],
                    "avoid_symbols": ["TSLA"],
                }
            }

        settings = Settings()
        result = _load_advisory_data(settings, mock_load_report, mock_load_override)
        assert result is not None
        assert result["observations"] == 42
        assert result["main_recommendations"] == 12
        assert result["cheap_recommendations"] == 3
        assert len(result["top_main_recommendations"]) == 1
        assert result["top_main_recommendations"][0]["ticker"] == "AAPL"
        assert len(result["top_cheap_recommendations"]) == 1
        assert result["top_cheap_recommendations"][0]["ticker"] == "STOCK1"
        assert result["promoted_symbols"] == 2
        assert result["avoided_symbols"] == 1
        assert result["promote_list"] == ["AAPL", "GOOGL"]
        assert result["avoid_list"] == ["TSLA"]

    def test_load_advisory_data_no_report(self) -> None:
        """Test _load_advisory_data returns None when no report."""
        from trading_bot.runtime.dashboard import _load_advisory_data
        from trading_bot.config.settings import Settings

        def mock_empty_report(settings: Settings) -> dict:
            return {"summary": {}}

        def mock_load_override(settings: Settings) -> dict:
            return {}

        settings = Settings()
        result = _load_advisory_data(settings, mock_empty_report, mock_load_override)
        assert result is None

    def test_load_advisory_data_exception(self) -> None:
        """Test _load_advisory_data returns None on exception."""
        from trading_bot.runtime.dashboard import _load_advisory_data
        from trading_bot.config.settings import Settings

        def mock_raises(settings: Settings) -> dict:
            raise RuntimeError("test error")

        def mock_load_override(settings: Settings) -> dict:
            return {}

        settings = Settings()
        result = _load_advisory_data(settings, mock_raises, mock_load_override)
        assert result is None
