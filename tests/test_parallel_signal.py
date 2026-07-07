from __future__ import annotations

import builtins

from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

from trading_bot.config.settings import AppSettings, CounterThesisSettings, RiskSettings, Settings
from trading_bot.models.portfolio import PortfolioState, Position


def _trade_signal(ticker="AAPL", action="BUY", confidence=0.75, tag="v3-trend_following"):
    from trading_bot.models.signal import TradeSignal
    return TradeSignal(
        ticker=ticker,
        timeframe="intraday",
        action=action,
        entry_price=50.0,
        stop_loss=48.0,
        profit_target=54.0,
        timestamp=datetime(2026, 7, 2, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        confidence=confidence,
        strategy_tag=tag,
        risk_reward_ratio=2.0,
    )


class TestParallelModePositionSizing:
    def test_sector_concentration_uses_local_sector_map_not_yfinance(self, tmp_path):
        settings = Settings()
        settings.app = AppSettings(signal_mode="serial")
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path)
        settings.counter_thesis = CounterThesisSettings(enabled=False)
        settings.risk = RiskSettings(
            ticker_reentry_cooldown_minutes=0,
            max_sector_concentration_pct=0.20,
            use_atr_sizing=False,
        )
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(
            cash=7000.0,
            equity=10000.0,
            positions={"MSFT": Position(ticker="MSFT", quantity=60, average_cost=50.0)},
        )
        original_import = builtins.__import__

        def forbid_yfinance(name, *args, **kwargs):
            if name == "yfinance":
                raise AssertionError("paper-trade sector check must not import yfinance")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", forbid_yfinance), patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ), patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
            m_ledger = MagicMock()
            m_ledger.ensure_portfolio_state.return_value = state
            m_ledger_cls.return_value = m_ledger

            with patch(
                "trading_bot.runtime.orchestrator._build_signal_result",
                return_value=(
                    _trade_signal("AAPL"),
                    "",
                    {"intraday_close": 51.0, "range_high": 50.0, "volume_ratio": 1.5},
                ),
            ), patch("trading_bot.runtime.orchestrator.evaluate_signal") as m_risk:
                from trading_bot.models.risk import RiskDecision
                m_risk.return_value = RiskDecision(
                    approved=True, reason="ok", position_size=100, dollar_risk=50.0,
                )

                from trading_bot.runtime.orchestrator import run_paper_trade
                results = run_paper_trade(["AAPL"], settings)

        assert any("REJECTED sector concentration" in row for row in results)
        m_ledger.record_fill.assert_not_called()

    def test_sector_concentration_blocks_projected_exposure(self, tmp_path):
        settings = Settings()
        settings.app = AppSettings(signal_mode="serial")
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path)
        settings.counter_thesis = CounterThesisSettings(enabled=False)
        settings.risk = RiskSettings(
            ticker_reentry_cooldown_minutes=0,
            max_sector_concentration_pct=0.20,
            use_atr_sizing=False,
        )
        (tmp_path / "decision-log.jsonl").write_text("")
        state = PortfolioState(
            cash=10_000.0,
            equity=10_000.0,
            positions={"MSFT": Position(ticker="MSFT", quantity=38, average_cost=50.0)},
        )

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ), patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
            m_ledger = MagicMock()
            m_ledger.ensure_portfolio_state.return_value = state
            m_ledger_cls.return_value = m_ledger

            with patch(
                "trading_bot.runtime.orchestrator._build_signal_result",
                return_value=(
                    _trade_signal("AAPL"),
                    "",
                    {"intraday_close": 51.0, "range_high": 50.0, "volume_ratio": 1.5},
                ),
            ), patch("trading_bot.runtime.orchestrator.evaluate_signal") as m_risk:
                from trading_bot.models.risk import RiskDecision
                m_risk.return_value = RiskDecision(
                    approved=True, reason="ok", position_size=10, dollar_risk=50.0,
                )

                from trading_bot.runtime.orchestrator import run_paper_trade
                results = run_paper_trade(["AAPL"], settings)

        assert any("projected 24% > 20%" in row for row in results)
        m_ledger.record_fill.assert_not_called()

    def test_serial_mode_still_works(self, tmp_path):
        settings = Settings()
        settings.app = AppSettings(signal_mode="serial")
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path)
        settings.counter_thesis = CounterThesisSettings(enabled=False)
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
        (tmp_path / "state.db").write_text("")
        (tmp_path / "decision-log.jsonl").write_text("")

        with patch("trading_bot.safety.kill_switch.check_kill_switch_before_trade", return_value=(True, "")), \
             patch("trading_bot.safety.circuit_breaker.check_circuit_breakers", return_value=(True, "")), \
             patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
            m_ledger = MagicMock()
            m_ledger.ensure_portfolio_state.return_value = PortfolioState(cash=10000, equity=10000)
            m_ledger_cls.return_value = m_ledger

            from trading_bot.runtime.orchestrator import _build_signal_result
            signal, reason, details = _build_signal_result("AAPL", settings)

        assert details.get("signal_mode") != "parallel"


class TestDefaultConfigValues:
    def test_signal_mode_default_is_serial(self):
        assert AppSettings().signal_mode == "serial"

    def test_swarm_weight_default_is_03(self):
        from trading_bot.config.settings import SwarmSettings
        assert SwarmSettings().swarm_weight == 0.3
