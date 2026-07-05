from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from trading_bot.config.settings import AppSettings, CounterThesisSettings, RiskSettings, Settings
from trading_bot.models.portfolio import PortfolioState


def _trade_signal(ticker="AAPL", tag="v3-mean_reversion"):
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
        strategy_tag=tag,
        risk_reward_ratio=2.0,
    )


def _mr_intraday_frame():
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
    return pd.DataFrame(data, index=pd.date_range("2025-06-01 10:00", periods=n, freq="5min"))


class TestV3IsMeanReversionFlag:
    def test_v3_signal_setups_is_mean_reversion_flag_when_mr_strategy(self):
        daily = pd.DataFrame({
            "open": [100.0] * 30, "high": [101.0] * 30,
            "low": [99.0] * 30, "close": [100.5] * 30,
            "volume": [1000.0] * 30,
            "ema_20": [99.0] * 30, "sma_50": [98.0] * 30,
        }, index=pd.date_range("2025-01-01", periods=30, freq="1d"))
        intraday = _mr_intraday_frame()

        settings = Settings()
        settings.app = AppSettings(allow_yellow_mean_reversion=True)
        settings.risk = RiskSettings(atr_period=14)
        settings.counter_thesis = CounterThesisSettings(enabled=False)

        from trading_bot.data import indicators as _ind

        with patch("trading_bot.runtime.orchestrator.market_data.fetch_and_validate_bars") as m_fetch:
            from trading_bot.data.validation import ValidationResult
            m_fetch.side_effect = [
                (daily, ValidationResult(valid=True, reason="ok")),
                (intraday, ValidationResult(valid=True, reason="ok")),
            ]

            with patch("trading_bot.strategy.strategy_selector.StrategySelector") as m_selector:
                from trading_bot.strategy.strategy_selector import StrategySelection, SignalScore
                selection = StrategySelection(
                    should_trade=True,
                    strategy_type="mean_reversion",
                    setup_name="oversold bounce",
                    signal_score=SignalScore(
                        total_score=7.5, confidence="high",
                        technical_score=1.5, volume_score=1.5,
                        trend_score=1.0, momentum_score=1.0,
                        regime_alignment=2.0, factor_score=0.5,
                        recommended_position_size_pct=0.1,
                    ),
                    regime=None,
                    reason="test",
                    entry_price=46.5,
                    stop_loss=45.0,
                    profit_target=49.5,
                )
                m_selector.return_value.select_strategy.return_value = selection

                from trading_bot.runtime.orchestrator import _build_v3_signal_result
                signal, reason, details = _build_v3_signal_result("AAPL", settings)

                assert signal is not None
                assert details.get("is_mean_reversion") is True, f"Expected is_mean_reversion=True, got {details}"

    def test_v3_signal_does_not_set_is_mean_reversion_for_trend(self):
        settings = Settings()
        settings.app = AppSettings(allow_yellow_mean_reversion=True)
        settings.risk = RiskSettings(atr_period=14)
        settings.counter_thesis = CounterThesisSettings(enabled=False)

        daily = pd.DataFrame({
            "open": [100.0] * 30, "high": [101.0] * 30,
            "low": [99.0] * 30, "close": [100.5] * 30,
            "volume": [1000.0] * 30,
            "ema_20": [99.0] * 30, "sma_50": [98.0] * 30,
        }, index=pd.date_range("2025-01-01", periods=30, freq="1d"))
        intraday = _mr_intraday_frame()
        intraday.loc[intraday.index[-1], "volume"] = 1300.0

        with patch("trading_bot.runtime.orchestrator.market_data.fetch_and_validate_bars") as m_fetch:
            from trading_bot.data.validation import ValidationResult
            m_fetch.side_effect = [
                (daily, ValidationResult(valid=True, reason="ok")),
                (intraday, ValidationResult(valid=True, reason="ok")),
            ]

            with patch("trading_bot.strategy.strategy_selector.StrategySelector") as m_selector:
                from trading_bot.strategy.strategy_selector import StrategySelection, SignalScore
                selection = StrategySelection(
                    should_trade=True,
                    strategy_type="trend_following",
                    setup_name="intraday breakout",
                    signal_score=SignalScore(
                        total_score=7.5, confidence="high",
                        technical_score=1.5, volume_score=1.5,
                        trend_score=1.0, momentum_score=1.0,
                        regime_alignment=2.0, factor_score=0.5,
                        recommended_position_size_pct=0.1,
                    ),
                    regime=None,
                    reason="test",
                    entry_price=50.5,
                    stop_loss=49.0,
                    profit_target=53.5,
                )
                m_selector.return_value.select_strategy.return_value = selection

                from trading_bot.runtime.orchestrator import _build_v3_signal_result
                signal, reason, details = _build_v3_signal_result("AAPL", settings)

                assert signal is not None
                assert details.get("is_mean_reversion") is False


class TestV3YellowMeanReversion:
    def test_v3_yellow_accepted_when_is_mean_reversion_set(self, tmp_path):
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

    def test_v3_yellow_rejected_when_no_is_mean_reversion(self, tmp_path):
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

    def test_yellow_mean_reversion_tags_position(self, tmp_path):
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
