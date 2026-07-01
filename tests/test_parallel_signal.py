from __future__ import annotations

import builtins
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from trading_bot.config.settings import AppSettings, CounterThesisSettings, RiskSettings, Settings, StrategySettings, RLSettings
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
        timestamp=datetime.now(timezone.utc),
        confidence=confidence,
        strategy_tag=tag,
        risk_reward_ratio=2.0,
    )


def _settings_parallel():
    s = Settings()
    s.app = AppSettings(signal_mode="parallel")
    s.risk = RiskSettings(ticker_reentry_cooldown_minutes=0)
    s.counter_thesis = CounterThesisSettings(enabled=False)
    s.strategy = StrategySettings(use_v3_signals=True)
    s.rl = RLSettings(enabled=False)
    return s


class TestParallelSignalConsensus:
    def test_two_sources_buy_returns_full_size(self, tmp_path):
        settings = _settings_parallel()
        settings.rl.enabled = True
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "state.db").write_text("")
        (tmp_path / "decision-log.jsonl").write_text("")

        rl_sig = _trade_signal(confidence=0.6, tag="rl_PPO")
        v3_sig = _trade_signal(confidence=0.9, tag="v3-trend_following")

        with patch(
            "trading_bot.runtime.orchestrator._build_rl_signal_result",
            return_value=(rl_sig, "rl ok", {}),
        ), patch(
            "trading_bot.runtime.orchestrator._build_v3_signal_result",
            return_value=(v3_sig, "v3 ok", {}),
        ):
            with patch("trading_bot.safety.kill_switch.check_kill_switch_before_trade", return_value=(True, "")), \
                 patch("trading_bot.safety.circuit_breaker.check_circuit_breakers", return_value=(True, "")), \
                 patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
                m_ledger = MagicMock()
                m_ledger.ensure_portfolio_state.return_value = PortfolioState(cash=10000, equity=10000)
                m_ledger_cls.return_value = m_ledger

                from trading_bot.runtime.orchestrator import _build_signal_result
                signal, reason, details = _build_signal_result("AAPL", settings)

        assert signal is not None
        assert details["consensus"] == "BUY"
        assert details["consensus_count"] == 2
        assert details.get("is_full_size") is True

    def test_single_source_buy_returns_half_size(self, tmp_path):
        settings = _settings_parallel()
        settings.strategy.use_v3_signals = True
        settings.rl.enabled = False
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "state.db").write_text("")

        v3_sig = _trade_signal(confidence=0.8)

        with patch(
            "trading_bot.runtime.orchestrator._build_v3_signal_result",
            return_value=(v3_sig, "v3 ok", {}),
        ):
            with patch("trading_bot.safety.kill_switch.check_kill_switch_before_trade", return_value=(True, "")), \
                 patch("trading_bot.safety.circuit_breaker.check_circuit_breakers", return_value=(True, "")), \
                 patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
                m_ledger = MagicMock()
                m_ledger.ensure_portfolio_state.return_value = PortfolioState(cash=10000, equity=10000)
                m_ledger_cls.return_value = m_ledger

                from trading_bot.runtime.orchestrator import _build_signal_result
                signal, reason, details = _build_signal_result("AAPL", settings)

        assert signal is not None
        assert details["consensus"] == "BUY"
        assert details["consensus_count"] == 1
        assert details.get("is_half_size") is True

    def test_sell_signal_vetoes(self, tmp_path):
        settings = _settings_parallel()
        settings.rl.enabled = True
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "state.db").write_text("")

        rl_sig = _trade_signal(action="SELL", confidence=0.5, tag="rl_PPO")
        v3_sig = _trade_signal(action="BUY", confidence=0.9)

        with patch(
            "trading_bot.runtime.orchestrator._build_rl_signal_result",
            return_value=(rl_sig, "rl sell", {}),
        ), patch(
            "trading_bot.runtime.orchestrator._build_v3_signal_result",
            return_value=(v3_sig, "v3 buy", {}),
        ):
            with patch("trading_bot.safety.kill_switch.check_kill_switch_before_trade", return_value=(True, "")), \
                 patch("trading_bot.safety.circuit_breaker.check_circuit_breakers", return_value=(True, "")), \
                 patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
                m_ledger = MagicMock()
                m_ledger.ensure_portfolio_state.return_value = PortfolioState(cash=10000, equity=10000)
                m_ledger_cls.return_value = m_ledger

                from trading_bot.runtime.orchestrator import _build_signal_result
                signal, reason, details = _build_signal_result("AAPL", settings)

        assert signal is None
        assert details["consensus"] == "SELL"

    def test_rl_sell_details_veto_when_rl_returns_no_trade_signal(self, tmp_path):
        settings = _settings_parallel()
        settings.rl.enabled = True
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "state.db").write_text("")

        v3_sig = _trade_signal(action="BUY", confidence=0.9)

        with patch(
            "trading_bot.runtime.orchestrator._build_rl_signal_result",
            return_value=(
                None,
                "RL agent predicts SELL (confidence=0.82)",
                {"rl_action": 2, "rl_confidence": 0.82},
            ),
        ), patch(
            "trading_bot.runtime.orchestrator._build_v3_signal_result",
            return_value=(v3_sig, "v3 buy", {}),
        ):
            from trading_bot.runtime.orchestrator import _build_signal_result
            signal, reason, details = _build_signal_result("AAPL", settings)

        assert signal is None
        assert reason == "parallel veto: SELL from rl"
        assert details["consensus"] == "SELL"
        assert details["vote_rl"] == "SELL:0.82"

    def test_no_buy_votes_returns_no_trade(self, tmp_path):
        settings = _settings_parallel()
        settings.strategy.use_v3_signals = True
        settings.rl.enabled = False
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "state.db").write_text("")

        with patch(
            "trading_bot.runtime.orchestrator._build_v3_signal_result",
            return_value=(None, "no signal", {}),
        ):
            with patch("trading_bot.safety.kill_switch.check_kill_switch_before_trade", return_value=(True, "")), \
                 patch("trading_bot.safety.circuit_breaker.check_circuit_breakers", return_value=(True, "")), \
                 patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
                m_ledger = MagicMock()
                m_ledger.ensure_portfolio_state.return_value = PortfolioState(cash=10000, equity=10000)
                m_ledger_cls.return_value = m_ledger

                from trading_bot.runtime.orchestrator import _build_signal_result
                signal, reason, details = _build_signal_result("AAPL", settings)

        assert signal is None
        assert details["consensus"] == "NO_TRADE"

    def test_source_votes_recorded_in_details(self, tmp_path):
        settings = _settings_parallel()
        settings.rl.enabled = True
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "state.db").write_text("")

        rl_sig = _trade_signal(confidence=0.7, tag="rl_PPO")

        with patch(
            "trading_bot.runtime.orchestrator._build_rl_signal_result",
            return_value=(rl_sig, "rl ok", {}),
        ):
            with patch("trading_bot.safety.kill_switch.check_kill_switch_before_trade", return_value=(True, "")), \
                 patch("trading_bot.safety.circuit_breaker.check_circuit_breakers", return_value=(True, "")), \
                 patch("trading_bot.runtime.orchestrator.PortfolioLedger") as m_ledger_cls:
                m_ledger = MagicMock()
                m_ledger.ensure_portfolio_state.return_value = PortfolioState(cash=10000, equity=10000)
                m_ledger_cls.return_value = m_ledger

                from trading_bot.runtime.orchestrator import _build_signal_result
                signal, reason, details = _build_signal_result("AAPL", settings)

        assert "source_votes" in details
        votes = details["source_votes"]
        assert any(v["source"] == "rl" and v["action"] == "BUY" for v in votes)
        json.dumps(details)

    def test_source_details_are_preserved_for_stack_scoring(self, tmp_path):
        settings = _settings_parallel()
        settings.rl.enabled = True
        settings.app.state_db_path = str(tmp_path / "state.db")
        settings.app.log_dir = str(tmp_path)
        (tmp_path / "state.db").write_text("")

        rl_sig = _trade_signal(confidence=0.7, tag="rl_PPO")
        v3_sig = _trade_signal(confidence=0.9, tag="v3-trend_following")

        with patch(
            "trading_bot.runtime.orchestrator._build_rl_signal_result",
            return_value=(rl_sig, "rl ok", {"rl_action": 1, "rl_confidence": 0.7}),
        ), patch(
            "trading_bot.runtime.orchestrator._build_v3_signal_result",
            return_value=(v3_sig, "v3 ok", {"v3_total_score": 9.6}),
        ):
            from trading_bot.runtime.orchestrator import _build_signal_result
            signal, reason, details = _build_signal_result("AAPL", settings)

        assert signal is not None
        assert details["rl_action"] == 1
        assert details["rl_confidence"] == 0.7
        assert details["v3_total_score"] == 9.6
        from trading_bot.strategy.supermodel import build_stacked_signal
        stacked = build_stacked_signal("AAPL", signal, details)
        assert "v3:support:0.80" in stacked.to_details()["supermodel_layers"]
        assert "rl:caution:0.70" in stacked.to_details()["supermodel_layers"]

    def test_scan_summary_counts_parallel_consensus_without_details(self, tmp_path):
        state_db = tmp_path / "state.db"
        settings = _settings_parallel()
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk.use_atr_sizing = False
        signal = _trade_signal()
        details = {
            "signal_mode": "parallel",
            "consensus": "BUY",
            "intraday_close": 51.0,
            "range_high": 50.0,
            "volume_ratio": 1.5,
            "daily_close": 105.0,
            "ema_20": 100.0,
            "sma_50": 95.0,
        }

        with patch("trading_bot.safety.kill_switch.check_kill_switch_before_trade", return_value=(True, "")), \
             patch("trading_bot.safety.circuit_breaker.check_circuit_breakers", return_value=(True, "")), \
             patch("trading_bot.runtime.orchestrator._build_signal_result", return_value=(signal, "", details)), \
             patch("trading_bot.runtime.orchestrator._market_data_status", return_value="fresh"), \
             patch("trading_bot.runtime.orchestrator._market_data_age", return_value="0m"), \
             patch("trading_bot.runtime.orchestrator.evaluate_signal") as m_risk:
            from trading_bot.models.risk import RiskDecision
            from trading_bot.runtime.orchestrator import run_scan

            m_risk.return_value = RiskDecision(
                approved=True, reason="ok", position_size=10, dollar_risk=20.0,
            )
            result = run_scan(["AAPL"], settings, include_details=False)

        assert result["summary"]["parallel_buy"] == 1
        assert result["summary"]["parallel_sell"] == 0
        assert result["summary"]["parallel_no_trade"] == 0


class TestParallelModePositionSizing:
    def test_half_size_applied_in_paper_trade(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(signal_mode="parallel")
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(
            ticker_reentry_cooldown_minutes=0,
            max_ticker_allocation_pct=0.5,
            use_atr_sizing=False,
        )
        settings.counter_thesis = CounterThesisSettings(enabled=False)
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)
        signal = _trade_signal()
        signal.stop_loss = 49.0
        details = {
            "intraday_close": 51.0,
            "range_high": 50.0,
            "volume_ratio": 1.5,
            "daily_close": 105.0,
            "ema_20": 100.0,
            "sma_50": 95.0,
            "is_half_size": True,
            "consensus": "BUY",
            "consensus_count": 1,
            "supermodel_decision": "support",
            "swarm_decision": "APPROVE",
            "source_votes": [{"source": "v3", "action": "BUY", "confidence": 0.8}],
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

        assert any("FILLED" in r for r in results), f"Expected FILLED: {results}"
        fill = m_ledger.record_fill.call_args.args[0]
        assert fill.quantity == 50
        assert m_ledger.record_fill.call_args.kwargs["strategy_tag"] == (
            "v3-trend|stack:caution|swarm:approve|consensus:buy"
        )
        event = json.loads((tmp_path / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert event["consensus"] == "BUY"
        assert event["consensus_count"] == 1
        assert event["source_votes"] == [{"source": "v3", "action": "BUY", "confidence": 0.8}]

    def test_swarm_approve_boosts_reduced_position_size(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(signal_mode="parallel")
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(
            ticker_reentry_cooldown_minutes=0,
            max_ticker_allocation_pct=0.5,
            use_atr_sizing=False,
        )
        settings.counter_thesis = CounterThesisSettings(enabled=False)
        settings.swarm.enabled = True
        settings.swarm.swarm_weight = 0.3
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)
        signal = _trade_signal()
        signal.stop_loss = 49.0
        details = {
            "intraday_close": 51.0,
            "range_high": 50.0,
            "volume_ratio": 1.5,
            "is_half_size": True,
            "consensus": "BUY",
            "consensus_count": 1,
        }

        from trading_bot.swarm.results import CommitteeDecision
        swarm_decision = CommitteeDecision(
            ticker="AAPL",
            decision="APPROVE",
            confidence=0.75,
            action="BUY",
            votes_for=3,
            votes_against=0,
            votes_abstain=1,
            total_workers=4,
        )

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ), patch(
            "trading_bot.runtime.orchestrator._run_swarm_overlay",
            return_value={"AAPL": swarm_decision},
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

        assert any("FILLED" in r for r in results), f"Expected FILLED: {results}"
        fill = m_ledger.record_fill.call_args.args[0]
        assert fill.quantity == 61

    def test_swarm_approve_does_not_exceed_risk_approved_size(self, tmp_path):
        state_db = tmp_path / "state.db"
        state_db.write_text("")
        settings = Settings()
        settings.app = AppSettings(signal_mode="parallel")
        settings.app.state_db_path = str(state_db)
        settings.app.log_dir = str(tmp_path)
        settings.risk = RiskSettings(
            ticker_reentry_cooldown_minutes=0,
            max_ticker_allocation_pct=0.5,
            use_atr_sizing=False,
        )
        settings.counter_thesis = CounterThesisSettings(enabled=False)
        settings.swarm.enabled = True
        settings.swarm.swarm_weight = 0.3
        (tmp_path / "decision-log.jsonl").write_text("")

        state = PortfolioState(cash=10000.0, equity=10000.0)
        signal = _trade_signal()
        signal.stop_loss = 49.0
        details = {
            "intraday_close": 51.0,
            "range_high": 50.0,
            "volume_ratio": 1.5,
            "is_full_size": True,
            "consensus": "BUY",
            "consensus_count": 2,
        }

        from trading_bot.swarm.results import CommitteeDecision
        swarm_decision = CommitteeDecision(
            ticker="AAPL",
            decision="APPROVE",
            confidence=0.75,
            action="BUY",
            votes_for=3,
            votes_against=0,
            votes_abstain=1,
            total_workers=4,
        )

        with patch(
            "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
            return_value=(True, ""),
        ), patch(
            "trading_bot.safety.circuit_breaker.check_circuit_breakers",
            return_value=(True, ""),
        ), patch(
            "trading_bot.runtime.orchestrator._run_swarm_overlay",
            return_value={"AAPL": swarm_decision},
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

        assert any("FILLED" in r for r in results), f"Expected FILLED: {results}"
        fill = m_ledger.record_fill.call_args.args[0]
        assert fill.quantity == 100
        assert details["position_size_capped"] == "risk_approved"
        event = json.loads((tmp_path / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert event["position_size_capped"] == "risk_approved"

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
