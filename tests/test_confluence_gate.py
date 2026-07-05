from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from trading_bot.config.settings import AppSettings, RiskSettings, Settings
from trading_bot.models.portfolio import PortfolioState
from trading_bot.strategy.setup_rules import compute_v25_confluence_score


class TestComputeV25ConfluenceScore:
    def test_strong_signal_scores_high(self):
        details = {
            "volume_ratio": 2.5,
            "intraday_close": 105.0,
            "range_high": 100.0,
            "daily_close": 102.0,
            "ema_20": 98.0,
            "sma_50": 95.0,
        }
        score = compute_v25_confluence_score(details)
        assert score >= 8.0

    def test_weak_signal_scores_low(self):
        details = {
            "volume_ratio": 0.3,
            "intraday_close": 99.0,
            "range_high": 100.0,
            "daily_close": 90.0,
            "ema_20": 95.0,
            "sma_50": 98.0,
        }
        score = compute_v25_confluence_score(details)
        assert score < 4.0

    def test_volume_score_scaling(self):
        details = {"volume_ratio": 0.5}
        s0 = compute_v25_confluence_score({**details, "intraday_close": 0, "range_high": 100})
        details["volume_ratio"] = 2.0
        s1 = compute_v25_confluence_score({**details, "intraday_close": 0, "range_high": 100})
        assert s1 > s0

    def test_regime_bullish_scores_higher(self):
        base = {"volume_ratio": 1.0, "intraday_close": 100.0, "range_high": 99.0}
        bearish = compute_v25_confluence_score({**base, "daily_close": 85.0, "ema_20": 95.0, "sma_50": 100.0})
        bullish = compute_v25_confluence_score({**base, "daily_close": 105.0, "ema_20": 100.0, "sma_50": 95.0})
        assert bullish > bearish

    def test_breakout_strength_affects_score(self):
        base = {"volume_ratio": 1.0, "daily_close": 100.0, "ema_20": 95.0, "sma_50": 90.0}
        tight = compute_v25_confluence_score({**base, "intraday_close": 100.1, "range_high": 100.0})
        strong = compute_v25_confluence_score({**base, "intraday_close": 103.0, "range_high": 100.0})
        assert strong > tight

    def test_missing_details_scores_zero(self):
        assert compute_v25_confluence_score({}) == 0.0


class TestConfluenceGateInPaperTrade:
    def test_low_confluence_rejected(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(min_entry_confluence_score=6.0)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)

        from trading_bot.models.signal import TradeSignal
        signal = TradeSignal(
            ticker="AAPL",
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
        details = {
            "intraday_close": 100.1,
            "range_high": 100.0,
            "volume_ratio": 1.2,
            "daily_close": 90.0,
            "ema_20": 95.0,
            "sma_50": 98.0,
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

        assert any("low confluence" in r for r in results)

    def test_high_confluence_accepted(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(min_entry_confluence_score=6.0)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)

        from trading_bot.models.signal import TradeSignal
        signal = TradeSignal(
            ticker="AAPL",
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
        details = {
            "intraday_close": 103.0,
            "range_high": 100.0,
            "volume_ratio": 2.0,
            "daily_close": 105.0,
            "ema_20": 100.0,
            "sma_50": 95.0,
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

        assert not any("low confluence" in r for r in results)

    def test_zero_threshold_disables_gate(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(min_entry_confluence_score=0.0)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)

        from trading_bot.models.signal import TradeSignal
        signal = TradeSignal(
            ticker="AAPL",
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
        details = {
            "intraday_close": 100.1,
            "range_high": 100.0,
            "volume_ratio": 0.5,
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

        assert not any("low confluence" in r for r in results)

    def test_default_threshold_is_4(self):
        assert AppSettings().min_entry_confluence_score == 4.0

    def test_v3_signal_bypasses_confluence_gate(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(min_entry_confluence_score=6.0)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)

        from trading_bot.models.signal import TradeSignal
        signal = TradeSignal(
            ticker="AAPL",
            timeframe="intraday",
            action="BUY",
            entry_price=50.0,
            stop_loss=48.0,
            profit_target=54.0,
            timestamp=datetime.now(timezone.utc),
            confidence=0.75,
            strategy_tag="v3-trend_following",
            risk_reward_ratio=2.0,
        )
        details = {
            "intraday_close": 50.5,
            "range_high": 50.0,
            "volume_ratio": 1.2,
            "daily_close": 90.0,
            "ema_20": 95.0,
            "sma_50": 98.0,
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

        assert not any("low confluence" in r for r in results)

    def test_mean_reversion_bypasses_confluence_gate(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(min_entry_confluence_score=6.0, allow_yellow_mean_reversion=True)
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(yellow_allocation_pct=0.5, ticker_reentry_cooldown_minutes=0)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)

        from trading_bot.models.signal import TradeSignal
        signal = TradeSignal(
            ticker="AAPL",
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
        details = {
            "intraday_close": 46.5,
            "range_high": 47.0,
            "volume_ratio": 0.5,
            "is_mean_reversion": True,
            "daily_close": 90.0,
            "ema_20": 95.0,
            "sma_50": 98.0,
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

        assert not any("low confluence" in r for r in results)
