from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from trading_bot.config.settings import AppSettings, RiskSettings, Settings
from trading_bot.models.portfolio import PortfolioState
from trading_bot.strategy.setup_rules import is_valid_mean_reversion_setup


def _trade_signal(ticker="AAPL"):
    from trading_bot.models.signal import TradeSignal
    return TradeSignal(
        ticker=ticker,
        timeframe="intraday",
        action="BUY",
        entry_price=50.0,
        stop_loss=48.0,
        profit_target=54.0,
        timestamp=datetime.now(timezone.utc),
        confidence=0.75,
        strategy_tag="intraday-signal-engine",
        risk_reward_ratio=2.0,
    )


def _mean_reversion_frame():
    n = 20
    close = [50.0] * 10 + [47.0, 46.0, 45.0, 44.0, 43.5, 43.0, 43.5, 44.0, 45.5, 46.5]
    data = {
        "open": [c * 0.999 for c in close],
        "high": [c * 1.02 for c in close],
        "low": [c * 0.98 for c in close],
        "close": close,
        "volume": [1000.0] * n,
        "volume_avg_5": [1000.0] * n,
        "bb_lower": [40.0] * n,
        "bb_upper": [55.0] * n,
        "rsi_14": [50.0] * 10 + [40.0, 38.0, 36.0, 34.0, 33.0, 32.0, 31.0, 30.5, 30.0, 30.0],
        "vwap": [47.0] * n,
        "atr_14": [1.0] * n,
    }
    frame = pd.DataFrame(data)
    frame.index = pd.date_range("2025-06-01 10:00", periods=n, freq="5min")
    return frame


def _no_mr_frame():
    n = 20
    close = [50.0] * n
    data = {
        "open": [c * 0.999 for c in close],
        "high": [c * 1.01 for c in close],
        "low": [c * 0.99 for c in close],
        "close": close,
        "volume": [1000.0] * n,
        "volume_avg_5": [1000.0] * n,
        "bb_lower": [45.0] * n,
        "bb_upper": [55.0] * n,
        "rsi_14": [50.0] * n,
        "vwap": [50.0] * n,
        "atr_14": [1.0] * n,
    }
    frame = pd.DataFrame(data)
    frame.index = pd.date_range("2025-06-01 10:00", periods=n, freq="5min")
    return frame


class TestIsValidMeanReversionSetup:
    def test_detects_vwap_reversion(self):
        assert is_valid_mean_reversion_setup(_mean_reversion_frame()) is True

    def test_rejects_no_mean_reversion_frame(self):
        assert is_valid_mean_reversion_setup(_no_mr_frame()) is False

    def test_rejects_empty_frame(self):
        assert is_valid_mean_reversion_setup(pd.DataFrame()) is False

    def test_rejects_frame_without_required_columns(self):
        assert is_valid_mean_reversion_setup(pd.DataFrame({"close": [50.0]})) is False


class TestYellowMeanReversionInPaperTrade:
    def test_yellow_allowed_when_feature_enabled(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(allow_yellow_mean_reversion=True)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(yellow_allocation_pct=0.5, ticker_reentry_cooldown_minutes=0)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)
        signal = _trade_signal()
        details = {
            "intraday_close": 46.5,
            "range_high": 47.0,
            "volume_ratio": 0.8,
            "is_mean_reversion": True,
        }

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ):
            with patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
                m_ledger = MagicMock()
                m_ledger.ensure_portfolio_state.return_value = state
                m_ledger_cls.return_value = m_ledger

                with patch("trading_bot.runtime.orchestrator._build_signal_result") as m_sig:
                    m_sig.return_value = (signal, "", details)

                    with patch("trading_bot.runtime.orchestrator.evaluate_signal") as m_risk:
                        from trading_bot.models.risk import RiskDecision
                        m_risk.return_value = RiskDecision(
                            approved=True, reason="ok", position_size=100, dollar_risk=50.0,
                        )

                        from trading_bot.runtime.orchestrator import run_paper_trade
                        results = run_paper_trade(["AAPL"], settings)

        assert not any("yellow signal" in r for r in results)

    def test_yellow_rejected_when_feature_disabled(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(allow_yellow_mean_reversion=False)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)
        signal = _trade_signal()
        details = {
            "intraday_close": 46.5,
            "range_high": 47.0,
            "volume_ratio": 0.8,
            "is_mean_reversion": True,
        }

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ):
            with patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
                m_ledger = MagicMock()
                m_ledger.ensure_portfolio_state.return_value = state
                m_ledger_cls.return_value = m_ledger

                with patch("trading_bot.runtime.orchestrator._build_signal_result") as m_sig:
                    m_sig.return_value = (signal, "", details)

                    from trading_bot.runtime.orchestrator import run_paper_trade
                    results = run_paper_trade(["AAPL"], settings)

        assert any("yellow signal" in r for r in results)

    def test_yellow_rejected_when_no_mean_reversion_setup(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(allow_yellow_mean_reversion=True)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)
        signal = _trade_signal()
        details = {
            "intraday_close": 46.5,
            "range_high": 47.0,
            "volume_ratio": 0.8,
        }

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ):
            with patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
                m_ledger = MagicMock()
                m_ledger.ensure_portfolio_state.return_value = state
                m_ledger_cls.return_value = m_ledger

                with patch("trading_bot.runtime.orchestrator._build_signal_result") as m_sig:
                    m_sig.return_value = (signal, "", details)

                    from trading_bot.runtime.orchestrator import run_paper_trade
                    results = run_paper_trade(["AAPL"], settings)

        assert any("yellow signal" in r for r in results)

    def test_green_signal_not_affected(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(allow_yellow_mean_reversion=True)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)
        signal = _trade_signal()
        details = {
            "intraday_close": 50.5,
            "range_high": 50.0,
            "volume_ratio": 1.2,
        }

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ):
            with patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
                m_ledger = MagicMock()
                m_ledger.ensure_portfolio_state.return_value = state
                m_ledger_cls.return_value = m_ledger

                with patch("trading_bot.runtime.orchestrator._build_signal_result") as m_sig:
                    m_sig.return_value = (signal, "", details)

                    with patch("trading_bot.runtime.orchestrator.evaluate_signal") as m_risk:
                        from trading_bot.models.risk import RiskDecision
                        m_risk.return_value = RiskDecision(
                            approved=True, reason="ok", position_size=100, dollar_risk=50.0,
                        )

                        from trading_bot.runtime.orchestrator import run_paper_trade
                        results = run_paper_trade(["AAPL"], settings)

        assert not any("yellow" in r.lower() and "rejected" in r.lower() for r in results)


class TestDefaultConfigValues:
    def test_yellow_allocation_default_is_half(self):
        assert RiskSettings().yellow_allocation_pct == 0.5

    def test_allow_yellow_feature_off_by_default(self):
        assert AppSettings().allow_yellow_mean_reversion is False
