"""Tests for swarm overlay integration in scanner."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from trading_bot.config.settings import Settings
from trading_bot.models.risk import RiskDecision
from trading_bot.models.signal import TradeSignal
from trading_bot.models.portfolio import Position
from trading_bot.portfolio.ledger import PortfolioLedger, PortfolioState
from trading_bot.swarm.results import CommitteeDecision, SignalVote


def _make_frame(n=252, start_price=100.0):
    """Create a mock OHLCV frame."""
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    prices = [start_price * (1 + 0.001 * i + 0.0001 * i**2) for i in range(n)]
    return pd.DataFrame({
        "timestamp": dates,
        "open": [p * 0.999 for p in prices],
        "high": [p * 1.001 for p in prices],
        "low": [p * 0.998 for p in prices],
        "close": prices,
        "volume": [1_000_000 for _ in range(n)],
    })


def _make_signal(ticker: str = "AAPL") -> TradeSignal:
    """Create a valid BUY TradeSignal."""
    return TradeSignal(
        ticker=ticker,
        timeframe="intraday",
        action="BUY",
        entry_price=150.0,
        stop_loss=145.0,
        profit_target=160.0,
        risk_reward_ratio=2.0,
        confidence=0.75,
        reasons=["test signal"],
        strategy_tag="test",
        timestamp=datetime.now(timezone.utc),
    )


def _make_decision(approved: bool = True, reason: str = "approved") -> RiskDecision:
    """Create a RiskDecision."""
    return RiskDecision(
        approved=approved,
        reason=reason,
        position_size=100,
        dollar_risk=500.0,
    )


class TestSwarmOverlayIntegration:
    """Tests for swarm overlay in scanner (Phase 1)."""

    def test_swarm_disabled_by_default(self, tmp_path: Path) -> None:
        """Test that swarm is disabled by default."""
        settings = Settings()
        assert settings.swarm.enabled is False

    def test_swarm_overlay_not_run_when_disabled(self, tmp_path: Path) -> None:
        """Test that swarm overlay is not run when disabled."""
        db_path = tmp_path / "state.db"
        ledger = PortfolioLedger(db_path)
        ledger.save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))

        settings = Settings(app={"state_db_path": str(db_path)})

        with patch("trading_bot.runtime.orchestrator._run_swarm_overlay") as mock_swarm:
            with patch(
                "trading_bot.runtime.orchestrator._build_signal_result",
                return_value=(_make_signal(), "test", {}),
            ):
                with patch(
                    "trading_bot.runtime.orchestrator.evaluate_signal",
                    return_value=_make_decision(),
                ):
                    from trading_bot.runtime.orchestrator import run_scan

                    result = run_scan(["AAPL"], settings, include_details=True)

                    # Swarm should not be called when disabled
                    mock_swarm.assert_not_called()
                    # Results should still be generated
                    assert result["summary"]["symbols"] == 1

    def test_swarm_overlay_run_when_enabled(self, tmp_path: Path) -> None:
        """Test that swarm overlay is run when enabled."""
        db_path = tmp_path / "state.db"
        ledger = PortfolioLedger(db_path)
        ledger.save_portfolio_state(
            PortfolioState(
                cash=10_000.0,
                equity=10_000.0,
                positions={"MSFT": Position(ticker="MSFT", quantity=10, average_cost=100.0)},
            )
        )

        settings = Settings(
            app={"state_db_path": str(db_path)},
            swarm={"enabled": True, "preset": "investment_committee"},
        )

        with patch("trading_bot.runtime.orchestrator._run_swarm_overlay") as mock_swarm:
            mock_swarm.return_value = {
                "AAPL": MagicMock(decision="APPROVE", confidence=0.8, key_rationale="bullish"),
            }
            with patch(
                "trading_bot.runtime.orchestrator._build_signal_result",
                return_value=(_make_signal(), "test", {}),
            ):
                with patch(
                    "trading_bot.runtime.orchestrator.evaluate_signal",
                    return_value=_make_decision(),
                ):
                    from trading_bot.runtime.orchestrator import run_scan

                    result = run_scan(["AAPL"], settings, include_details=True)

                    # Swarm should be called when enabled
                    mock_swarm.assert_called_once()
                    assert mock_swarm.call_args.kwargs["portfolio_state"]["cash"] == 10_000.0
                    assert mock_swarm.call_args.kwargs["portfolio_state"]["positions"]["MSFT"]["quantity"] == 10
                    # Results should include swarm info
                    assert "swarm_enabled" in result["summary"]
                    assert result["summary"]["swarm_enabled"] is True

    def test_swarm_results_in_candidate_rows(self, tmp_path: Path) -> None:
        """Test that swarm results are included in candidate rows."""
        db_path = tmp_path / "state.db"
        ledger = PortfolioLedger(db_path)
        ledger.save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))

        settings = Settings(
            app={"state_db_path": str(db_path)},
            swarm={"enabled": True, "preset": "investment_committee"},
        )

        with patch("trading_bot.runtime.orchestrator._run_swarm_overlay") as mock_swarm:
            mock_swarm.return_value = {
                "AAPL": MagicMock(decision="APPROVE", confidence=0.8, key_rationale="bullish"),
            }
            with patch(
                "trading_bot.runtime.orchestrator._build_signal_result",
                return_value=(_make_signal(), "test", {}),
            ):
                with patch(
                    "trading_bot.runtime.orchestrator.evaluate_signal",
                    return_value=_make_decision(),
                ):
                    from trading_bot.runtime.orchestrator import run_scan

                    result = run_scan(["AAPL"], settings, include_details=True)

                    # Candidate rows should include swarm info
                    candidates = result["candidates"]
                    assert len(candidates) > 0
                    approved = [c for c in candidates if c["status"] == "APPROVED"]
                    if approved:
                        assert "swarm_decision" in approved[0]
                        assert approved[0]["swarm_decision"] == "APPROVE"
                        assert "swarm:support" in approved[0]["details"]["supermodel_layers"]

    def test_swarm_results_surface_sentiment_fields(self, tmp_path: Path) -> None:
        """Sentiment analyst evidence should be exposed in scan candidates/details."""
        db_path = tmp_path / "state.db"
        ledger = PortfolioLedger(db_path)
        ledger.save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))

        settings = Settings(
            app={"state_db_path": str(db_path)},
            swarm={"enabled": True, "preset": "investment_committee"},
        )

        sentiment_vote = SignalVote(
            ticker="AAPL",
            action="BUY",
            confidence=0.72,
            worker_name="sentiment_analyst",
            preset="investment_committee",
            reasons=["news:upgrade"],
            metadata={
                "sentiment_score": 0.42,
                "news_count": 2,
                "source": "rss",
            },
        )
        committee = CommitteeDecision(
            decision="APPROVE",
            ticker="AAPL",
            action="BUY",
            confidence=0.8,
            supporting_signals=[sentiment_vote],
            key_rationale="bullish",
        )

        with patch("trading_bot.runtime.orchestrator._run_swarm_overlay", return_value={"AAPL": committee}):
            with patch(
                "trading_bot.runtime.orchestrator._build_signal_result",
                return_value=(_make_signal(), "test", {}),
            ):
                with patch(
                    "trading_bot.runtime.orchestrator.evaluate_signal",
                    return_value=_make_decision(),
                ):
                    from trading_bot.runtime.orchestrator import run_scan

                    result = run_scan(["AAPL"], settings, include_details=True)

        approved = [c for c in result["candidates"] if c["status"] == "APPROVED"]
        assert len(approved) == 1
        row = approved[0]
        assert row["swarm_sentiment_action"] == "BUY"
        assert row["swarm_sentiment_confidence"] == 0.72
        assert row["swarm_sentiment_score"] == 0.42
        assert row["swarm_sentiment_news_count"] == 2
        assert row["swarm_sentiment_source"] == "rss"
        assert row["details"]["swarm_sentiment_score"] == 0.42

    def test_swarm_sentiment_influences_scan_order_and_summary(self, tmp_path: Path) -> None:
        """Bullish sentiment evidence should break ties and appear in summary counts."""
        db_path = tmp_path / "state.db"
        ledger = PortfolioLedger(db_path)
        ledger.save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))

        settings = Settings(
            app={"state_db_path": str(db_path)},
            swarm={"enabled": True, "preset": "investment_committee"},
        )

        bullish_vote = SignalVote(
            ticker="AAPL",
            action="BUY",
            confidence=0.7,
            worker_name="sentiment_analyst",
            preset="investment_committee",
            reasons=["news:upgrade"],
            metadata={"sentiment_score": 0.45, "news_count": 1, "source": "rss"},
        )
        bearish_vote = SignalVote(
            ticker="MSFT",
            action="SELL",
            confidence=0.7,
            worker_name="sentiment_analyst",
            preset="investment_committee",
            reasons=["news:downgrade"],
            metadata={"sentiment_score": -0.4, "news_count": 1, "source": "rss"},
        )
        committee_results = {
            "AAPL": CommitteeDecision(
                decision="APPROVE",
                ticker="AAPL",
                action="BUY",
                confidence=0.7,
                supporting_signals=[bullish_vote],
                key_rationale="bullish",
            ),
            "MSFT": CommitteeDecision(
                decision="APPROVE",
                ticker="MSFT",
                action="BUY",
                confidence=0.7,
                opposing_signals=[bearish_vote],
                key_rationale="mixed",
            ),
        }

        with patch("trading_bot.runtime.orchestrator._run_swarm_overlay", return_value=committee_results):
            with patch(
                "trading_bot.runtime.orchestrator._build_signal_result",
                side_effect=[(_make_signal("AAPL"), "test", {}), (_make_signal("MSFT"), "test", {})],
            ):
                with patch(
                    "trading_bot.runtime.orchestrator.evaluate_signal",
                    return_value=_make_decision(),
                ):
                    from trading_bot.runtime.orchestrator import run_scan

                    result = run_scan(["MSFT", "AAPL"], settings, include_details=True)

        approved = [c for c in result["candidates"] if c["status"] == "APPROVED"]
        assert [row["ticker"] for row in approved] == ["AAPL", "MSFT"]
        assert result["summary"]["swarm_sentiment_evidence"] == 2
        assert result["summary"]["swarm_sentiment_bullish"] == 1
        assert result["summary"]["swarm_sentiment_bearish"] == 1

    def test_swarm_summary_includes_stats(self, tmp_path: Path) -> None:
        """Test that swarm summary includes approval/rejection counts."""
        db_path = tmp_path / "state.db"
        ledger = PortfolioLedger(db_path)
        ledger.save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))

        settings = Settings(
            app={"state_db_path": str(db_path)},
            swarm={"enabled": True, "preset": "investment_committee"},
        )

        with patch("trading_bot.runtime.orchestrator._run_swarm_overlay") as mock_swarm:
            mock_swarm.return_value = {
                "AAPL": MagicMock(decision="APPROVE", confidence=0.8, key_rationale="bullish"),
                "MSFT": MagicMock(decision="REJECT", confidence=0.7, key_rationale="bearish"),
            }
            with patch(
                "trading_bot.runtime.orchestrator._build_signal_result",
                side_effect=[
                    (_make_signal("AAPL"), "test", {}),
                    (_make_signal("MSFT"), "test", {}),
                ],
            ):
                with patch(
                    "trading_bot.runtime.orchestrator.evaluate_signal",
                    side_effect=[
                        _make_decision(True, "approved"),
                        _make_decision(False, "rejected"),
                    ],
                ):
                    from trading_bot.runtime.orchestrator import run_scan

                    result = run_scan(["AAPL", "MSFT"], settings)

                    # Summary should include swarm stats
                    summary = result["summary"]
                    assert summary.get("swarm_approved") == 1
                    assert summary.get("swarm_rejected") == 1

    def test_swarm_overlay_handles_errors_gracefully(self, tmp_path: Path) -> None:
        """Test that swarm overlay failures don't break scan."""
        db_path = tmp_path / "state.db"
        ledger = PortfolioLedger(db_path)
        ledger.save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))

        settings = Settings(
            app={"state_db_path": str(db_path)},
            swarm={"enabled": True, "preset": "investment_committee"},
        )

        with patch("trading_bot.runtime.orchestrator._run_swarm_overlay") as mock_swarm:
            mock_swarm.side_effect = Exception("Swarm failed")
            with patch(
                "trading_bot.runtime.orchestrator._build_signal_result",
                return_value=(_make_signal(), "test", {}),
            ):
                with patch(
                    "trading_bot.runtime.orchestrator.evaluate_signal",
                    return_value=_make_decision(),
                ):
                    from trading_bot.runtime.orchestrator import run_scan

                    result = run_scan(["AAPL"], settings)

                    # Scan should still complete
                    assert result["summary"]["symbols"] == 1
                    # Swarm was enabled but failed, so counts should be 0
                    assert result["summary"].get("swarm_enabled") is True
                    assert result["summary"].get("swarm_approved") == 0
                    assert result["summary"].get("swarm_rejected") == 0
                    assert result["summary"].get("swarm_hold") == 0

    def test_swarm_overlay_returns_empty_on_no_data(self, tmp_path: Path) -> None:
        """Test that swarm overlay returns empty dict when no data available."""
        db_path = tmp_path / "state.db"
        ledger = PortfolioLedger(db_path)
        ledger.save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))

        settings = Settings(
            app={"state_db_path": str(db_path)},
            swarm={"enabled": True, "preset": "investment_committee"},
        )

        import trading_bot.data.market_data as market_data
        with patch.object(market_data, "fetch_bars") as mock_fetch:
            mock_fetch.return_value = None
            from trading_bot.runtime.orchestrator import _run_swarm_overlay

            results = _run_swarm_overlay(["AAPL"], settings)
            assert results == {}
            assert mock_fetch.call_args.kwargs["period"] == settings.market_data.daily_period
            assert mock_fetch.call_args.kwargs["interval"] == "1d"

    def test_swarm_overlay_sets_up_workers(self, tmp_path: Path) -> None:
        """Test overlay runs real workers instead of aggregating an empty engine."""
        settings = Settings(
            app={"state_db_path": str(tmp_path / "state.db")},
            swarm={"enabled": True, "preset": "investment_committee"},
        )

        import trading_bot.data.market_data as market_data
        with patch.object(market_data, "fetch_bars", return_value=_make_frame()):
            from trading_bot.runtime.orchestrator import _run_swarm_overlay

            results = _run_swarm_overlay(["AAPL"], settings)

        assert "AAPL" in results
        assert results["AAPL"].total_workers > 0

    def test_swarm_overlay_enriches_raw_frames_for_technical_workers(self, tmp_path: Path) -> None:
        """Technical swarm workers need indicator columns, not raw OHLCV only."""
        settings = Settings(
            app={"state_db_path": str(tmp_path / "state.db")},
            swarm={"enabled": True, "preset": "technical_analysis_panel"},
        )
        captured: dict[str, pd.DataFrame] = {}

        def capture_run(self, symbols, market_data, portfolio_state=None, **kwargs):
            captured["symbols"] = symbols
            captured.update(market_data)
            captured["portfolio_state"] = portfolio_state
            captured["sentiment_context"] = kwargs.get("sentiment_context")
            captured["memory_store"] = kwargs.get("memory_store")
            return SimpleNamespace(decisions={})

        import trading_bot.data.market_data as market_data
        from trading_bot.swarm.engine import SwarmEngine

        with patch.object(market_data, "fetch_bars", return_value=_make_frame()):
            with patch.object(SwarmEngine, "run", capture_run):
                from trading_bot.runtime.orchestrator import _run_swarm_overlay

                _run_swarm_overlay(["AAPL"], settings, portfolio_state={"cash": 123.0})

        frame = captured["AAPL"]
        for column in ("ema_20", "sma_50", "rsi_14", "bb_lower", "bb_upper", "volume_avg_5"):
            assert column in frame.columns
        assert captured["portfolio_state"] == {"cash": 123.0}
        assert captured["symbols"] == ["AAPL"]
        assert captured["sentiment_context"]["vix"] > 0
        assert captured["memory_store"] is None
