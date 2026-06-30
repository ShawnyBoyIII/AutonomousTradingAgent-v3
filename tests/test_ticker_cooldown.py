from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

from trading_bot.config.settings import RiskSettings, Settings
from trading_bot.models.portfolio import PortfolioState
from trading_bot.runtime.orchestrator import _recently_exited, run_paper_trade


class TestRecentlyExited:
    def test_no_entry_returns_false(self):
        state = PortfolioState(cash=10000.0, equity=10000.0)
        assert _recently_exited("AAPL", state, cooldown_minutes=30) is False

    def test_old_entry_returns_false(self):
        state = PortfolioState(cash=10000.0, equity=10000.0)
        state.last_exited_at["AAPL"] = "2020-01-01T12:00:00"
        assert _recently_exited("AAPL", state, cooldown_minutes=30) is False

    def test_recent_entry_returns_true(self):
        state = PortfolioState(cash=10000.0, equity=10000.0)
        state.last_exited_at["AAPL"] = datetime.datetime.now().isoformat()
        assert _recently_exited("AAPL", state, cooldown_minutes=30) is True

    def test_recent_aware_entry_returns_true(self):
        state = PortfolioState(cash=10000.0, equity=10000.0)
        state.last_exited_at["AAPL"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        assert _recently_exited("AAPL", state, cooldown_minutes=30) is True

    def test_different_ticker_unaffected(self):
        state = PortfolioState(cash=10000.0, equity=10000.0)
        state.last_exited_at["AAPL"] = datetime.datetime.now().isoformat()
        assert _recently_exited("MSFT", state, cooldown_minutes=30) is False

    def test_invalid_timestamp_returns_false(self):
        state = PortfolioState(cash=10000.0, equity=10000.0)
        state.last_exited_at["AAPL"] = "not-a-valid-timestamp"
        assert _recently_exited("AAPL", state, cooldown_minutes=30) is False


class TestReentryCooldownInPaperTrade:
    def test_reentry_blocked_within_cooldown(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=30)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(
            cash=10000.0,
            equity=10000.0,
            positions={},
            last_exited_at={"AAPL": datetime.datetime.now().isoformat()},
        )

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ):
            with patch("trading_bot.runtime.orchestrator.PortfolioLedger") as mock_ledger_cls:
                mock_ledger = MagicMock()
                mock_ledger.ensure_portfolio_state.return_value = state
                mock_ledger_cls.return_value = mock_ledger

                results = run_paper_trade(["AAPL"], settings)

        assert any("ticker re-entry cooldown" in r for r in results)

    def test_reentry_allowed_after_cooldown(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=30)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "decision-log.jsonl").write_text("")

        old_ts = (datetime.datetime.now() - datetime.timedelta(minutes=60)).isoformat()
        state = PortfolioState(
            cash=10000.0,
            equity=10000.0,
            positions={},
            last_exited_at={"AAPL": old_ts},
        )

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ):
            with patch("trading_bot.runtime.orchestrator.PortfolioLedger") as mock_ledger_cls:
                mock_ledger = MagicMock()
                mock_ledger.ensure_portfolio_state.return_value = state
                mock_ledger_cls.return_value = mock_ledger

                results = run_paper_trade(["AAPL"], settings)

        assert not any("ticker re-entry cooldown" in r for r in results)

    def test_cooldown_does_not_block_different_ticker(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=30)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(
            cash=10000.0,
            equity=10000.0,
            positions={},
            last_exited_at={"AAPL": datetime.datetime.now().isoformat()},
        )

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ):
            with patch("trading_bot.runtime.orchestrator.PortfolioLedger") as mock_ledger_cls:
                mock_ledger = MagicMock()
                mock_ledger.ensure_portfolio_state.return_value = state
                mock_ledger_cls.return_value = mock_ledger

                results = run_paper_trade(["MSFT"], settings)

        assert not any("ticker re-entry cooldown" in r for r in results)

    def test_zero_cooldown_disables_feature(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(
            cash=10000.0,
            equity=10000.0,
            positions={},
            last_exited_at={"AAPL": datetime.datetime.now().isoformat()},
        )

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ):
            with patch("trading_bot.runtime.orchestrator.PortfolioLedger") as mock_ledger_cls:
                mock_ledger = MagicMock()
                mock_ledger.ensure_portfolio_state.return_value = state
                mock_ledger_cls.return_value = mock_ledger

                results = run_paper_trade(["AAPL"], settings)

        assert not any("ticker re-entry cooldown" in r for r in results)

    def test_open_ticker_still_blocked_even_without_cooldown(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "decision-log.jsonl").write_text("")

        from trading_bot.models.portfolio import Position

        state = PortfolioState(
            cash=10000.0,
            equity=10000.0,
            positions={"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0)},
        )

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ):
            with patch("trading_bot.runtime.orchestrator.PortfolioLedger") as mock_ledger_cls:
                mock_ledger = MagicMock()
                mock_ledger.ensure_portfolio_state.return_value = state
                mock_ledger_cls.return_value = mock_ledger

                results = run_paper_trade(["AAPL"], settings)

        assert any("duplicate open ticker" in r for r in results)

    def test_default_cooldown_is_30_minutes(self):
        settings = Settings()
        assert settings.risk.ticker_reentry_cooldown_minutes == 30

    def test_ledger_fallback_appends_cooldown_check(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=30)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(
            cash=10000.0,
            equity=10000.0,
            positions={},
            last_exited_at={"AAPL": datetime.datetime.now().isoformat()},
        )

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ):
            with patch("trading_bot.runtime.orchestrator.PortfolioLedger") as mock_ledger_cls:
                mock_ledger = MagicMock()
                mock_ledger.ensure_portfolio_state.return_value = state
                mock_ledger_cls.return_value = mock_ledger

                results = run_paper_trade(["AAPL"], settings)

        assert any("ticker re-entry cooldown" in r for r in results)
