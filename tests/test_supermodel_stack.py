from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from typer.testing import CliRunner

from trading_bot.cli.app import (
    _format_scan_summary,
    _scan_row_supermodel,
    _trade_consensus,
    _trade_stack_decision,
    _trade_swarm_decision,
    app,
)
from trading_bot.config.loader import load_settings
from trading_bot.config.settings import Settings
from trading_bot.db.repositories import (
    get_scan_results,
    get_trades,
    update_trade_exit,
    upsert_scan_result,
    upsert_trade,
)
from trading_bot.db.session import get_session, init_db, make_session_factory
from trading_bot.models.order import FillResult
from trading_bot.models.risk import RiskDecision
from trading_bot.models.signal import TradeSignal
from trading_bot.portfolio.ledger import PortfolioLedger, PortfolioState
from trading_bot.runtime.orchestrator import run_paper_trade, run_scan
from trading_bot.runtime.orchestrator import _format_scan_details
from trading_bot.runtime.orchestrator import (
    _persist_trade_to_db,
    _scan_row_details_for_persistence,
    _trade_strategy_tag,
)
from trading_bot.strategy.supermodel import build_stacked_signal
from trading_bot.swarm.results import CommitteeDecision, SignalVote


def test_supermodel_supports_when_strategy_v3_and_rl_agree() -> None:
    signal = SimpleNamespace(confidence=0.82)
    details = {"v3_total_score": 9.5, "rl_action": 1, "rl_confidence": 0.78}

    stacked = build_stacked_signal("AAPL", signal, details)

    assert stacked.decision == "support"
    assert stacked.score >= 0.72
    assert stacked.to_details()["supermodel_layers"] == (
        "setup:support:0.82,v3:support:0.79,rl:support:0.78"
    )


def test_supermodel_blocks_when_rl_says_sell() -> None:
    signal = SimpleNamespace(confidence=0.9)
    details = {"v3_total_score": 10.0, "rl_action": 2, "rl_confidence": 0.9}

    stacked = build_stacked_signal("AAPL", signal, details)

    assert stacked.decision == "block"
    assert any(layer.name == "rl" and layer.verdict == "block" for layer in stacked.layers)


def test_supermodel_reports_no_signal_without_local_setup() -> None:
    stacked = build_stacked_signal("AAPL", None, {})

    assert stacked.decision == "no_signal"
    assert stacked.to_details()["supermodel_score"] == 0.0


def test_supermodel_scan_details_and_summary_are_visible() -> None:
    details = {
        "supermodel_decision": "support",
        "supermodel_score": 0.8,
        "supermodel_layers": "setup:support:0.82,v3:support:0.79",
        "swarm_decision": "APPROVE",
        "swarm_confidence": 0.75,
        "swarm_handoff": "risk_manager handoff: technical=BUY fundamental=HOLD",
    }
    summary = {
        "symbols": 1,
        "approved": 1,
        "green": 1,
        "yellow": 0,
        "rejected": 0,
        "no_signal": 0,
        "errors": 0,
        "supermodel_support": 1,
        "supermodel_caution": 0,
        "supermodel_block": 0,
        "supermodel_no_signal": 0,
        "swarm_enabled": True,
        "swarm_approved": 1,
        "swarm_rejected": 0,
        "swarm_hold": 0,
        "parallel_buy": 1,
        "parallel_sell": 0,
        "parallel_no_trade": 0,
    }

    assert "supermodel=support:0.8" in _format_scan_details(details)
    assert "swarm=APPROVE:0.75" in _format_scan_details(details)
    assert (
        "swarm_handoff=risk_manager_handoff:_technical=BUY_fundamental=HOLD"
        in _format_scan_details(details)
    )
    assert "supermodel_support=1" in _format_scan_summary(summary)
    assert "swarm_approved=1" in _format_scan_summary(summary)
    assert "parallel_buy=1" in _format_scan_summary(summary)


def test_format_scan_summary_includes_swarm_sentiment_counts() -> None:
    summary = {
        "symbols": 2,
        "approved": 1,
        "green": 1,
        "yellow": 0,
        "rejected": 1,
        "no_signal": 0,
        "errors": 0,
        "swarm_enabled": True,
        "swarm_approved": 1,
        "swarm_rejected": 1,
        "swarm_hold": 0,
        "swarm_sentiment_evidence": 2,
        "swarm_sentiment_bullish": 1,
        "swarm_sentiment_bearish": 1,
    }

    formatted = _format_scan_summary(summary)

    assert "swarm_sentiment_evidence=2" in formatted
    assert "swarm_sentiment_bullish=1" in formatted
    assert "swarm_sentiment_bearish=1" in formatted


def test_compact_scan_persistence_keeps_swarm_evidence() -> None:
    details = _scan_row_details_for_persistence(
        {
            "supermodel_decision": "support",
            "supermodel_score": 0.8,
            "swarm_decision": "APPROVE",
            "swarm_confidence": 0.75,
            "swarm_rationale": "technical_analyst: uptrend confirmed",
            "swarm_handoff": "risk_manager handoff: technical=BUY fundamental=HOLD",
        }
    )

    assert details == {
        "supermodel_decision": "support",
        "supermodel_score": 0.8,
        "swarm_decision": "APPROVE",
        "swarm_confidence": 0.75,
        "swarm_rationale": "technical_analyst: uptrend confirmed",
        "swarm_handoff": "risk_manager handoff: technical=BUY fundamental=HOLD",
    }


def test_supermodel_tracks_no_signal_rows(monkeypatch, tmp_path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    settings = load_settings(config_file)

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(
        orchestrator,
        "_build_signal_result",
        lambda symbol, settings: (None, "daily regime not bullish", {"daily_close": 100.0}),
    )

    result = run_scan(["AAPL"], settings, include_details=True)

    assert result["summary"]["supermodel_no_signal"] == 1
    assert result["candidates"][0]["supermodel_decision"] == "no_signal"
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        stored = get_scan_results(session, ticker="AAPL", limit=1)[0]
        details = json.loads(stored.details)
        assert details["supermodel_decision"] == "no_signal"
    finally:
        session.close()
        engine.dispose()


def test_no_signal_scan_details_include_swarm_handoff(monkeypatch, tmp_path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    settings = Settings(
        app={"state_db_path": str(tmp_path / "state.db")},
        swarm={"enabled": True},
    )

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(
        orchestrator,
        "_build_signal_result",
        lambda symbol, settings: (None, "daily regime not bullish", {"daily_close": 100.0}),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_swarm_overlay",
        lambda symbols, settings, portfolio_state=None: {
            "AAPL": SimpleNamespace(
                decision="HOLD_FOR_MORE_INFO",
                confidence=0.5,
                key_rationale="No worker verdicts",
                risk_factors=[
                    "risk_manager handoff: technical=BUY fundamental=HOLD",
                ],
            )
        },
    )

    result = run_scan(["AAPL"], settings, include_details=True)

    assert "swarm=HOLD_FOR_MORE_INFO:0.5" in result["lines"][0]
    assert (
        "swarm_handoff=risk_manager_handoff:_technical=BUY_fundamental=HOLD"
        in result["lines"][0]
    )
    assert result["summary"]["swarm_hold"] == 1


def test_scan_error_keeps_swarm_evidence(monkeypatch, tmp_path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    log_dir = tmp_path / "logs"
    settings = Settings(
        app={"state_db_path": str(tmp_path / "state.db"), "log_dir": str(log_dir)},
        swarm={"enabled": True},
    )

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(
        orchestrator,
        "_build_signal_result",
        lambda symbol, settings: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_swarm_overlay",
        lambda symbols, settings, portfolio_state=None: {
            "AAPL": SimpleNamespace(
                decision="APPROVE",
                confidence=0.75,
                key_rationale="bullish",
                risk_factors=[
                    "risk_manager handoff: technical=BUY fundamental=BUY",
                ],
            )
        },
    )

    result = run_scan(["AAPL"], settings, include_details=True)

    row = result["candidates"][0]
    assert row["swarm_decision"] == "APPROVE"
    assert row["swarm_confidence"] == 0.75
    assert row["swarm_handoff"] == "risk_manager handoff: technical=BUY fundamental=BUY"
    event = json.loads((log_dir / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["status"] == "ERROR"
    assert event["swarm_decision"] == "APPROVE"
    assert event["swarm_handoff"] == "risk_manager handoff: technical=BUY fundamental=BUY"


def test_supermodel_persists_without_why(monkeypatch, tmp_path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    PortfolioLedger(db_path).save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))
    settings = Settings(app={"state_db_path": str(db_path), "log_dir": str(log_dir)})
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(timezone.utc),
    )

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(orchestrator, "_fetch_atr", lambda symbol, settings: None)
    monkeypatch.setattr(
        orchestrator,
        "_build_signal_result",
        lambda symbol, settings: (signal, "test", {"v3_total_score": 12.0}),
    )
    monkeypatch.setattr(
        orchestrator,
        "evaluate_signal",
        lambda **kwargs: RiskDecision(approved=True, reason="approved", position_size=1, dollar_risk=5.0),
    )

    result = run_scan(["AAPL"], settings, include_details=False)

    row = result["candidates"][0]
    assert "details" not in row
    assert row["supermodel_decision"] == "support"
    event = json.loads((log_dir / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["status"] == "APPROVED"
    assert event["supermodel_decision"] == "support"

    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        stored = get_scan_results(session, ticker="AAPL", limit=1)[0]
        details = json.loads(stored.details)
        assert details["supermodel_decision"] == "support"
        assert details["supermodel_score"] == 0.9
    finally:
        session.close()
        engine.dispose()


def test_scan_reject_event_keeps_stack_evidence(monkeypatch, tmp_path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    PortfolioLedger(db_path).save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))
    settings = Settings(app={"state_db_path": str(db_path), "log_dir": str(log_dir)})
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(timezone.utc),
    )

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(orchestrator, "_fetch_atr", lambda symbol, settings: None)
    monkeypatch.setattr(
        orchestrator,
        "_build_signal_result",
        lambda symbol, settings: (signal, "test", {"v3_total_score": 12.0}),
    )
    monkeypatch.setattr(
        orchestrator,
        "evaluate_signal",
        lambda **kwargs: RiskDecision(approved=False, reason="too much heat", position_size=0, dollar_risk=0.0),
    )

    result = run_scan(["AAPL"], settings, include_details=False)

    assert result["candidates"][0]["supermodel_decision"] == "support"
    event = json.loads((log_dir / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["status"] == "REJECTED"
    assert event["reason"] == "too much heat"
    assert event["supermodel_decision"] == "support"


def test_stale_scan_row_keeps_swarm_evidence(monkeypatch, tmp_path) -> None:
    """Stale check removed — test normal approval with swarm evidence still works."""
    import trading_bot.runtime.orchestrator as orchestrator

    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    PortfolioLedger(db_path).save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))
    settings = Settings(
        app={"state_db_path": str(db_path), "log_dir": str(log_dir)},
        swarm={"enabled": True},
    )
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(timezone.utc),
    )

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(orchestrator, "_fetch_atr", lambda symbol, settings: None)
    monkeypatch.setattr(orchestrator, "_market_data_age", lambda timestamp: "99m")
    monkeypatch.setattr(
        orchestrator,
        "_build_signal_result",
        lambda symbol, settings: (signal, "test", {"v3_total_score": 12.0}),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_swarm_overlay",
        lambda symbols, settings, portfolio_state=None: {
            "AAPL": SimpleNamespace(
                decision="APPROVE",
                confidence=0.75,
                key_rationale="bullish",
                risk_factors=["risk_manager handoff: technical=BUY fundamental=BUY"],
            )
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "evaluate_signal",
        lambda **kwargs: RiskDecision(approved=True, reason="approved", position_size=1, dollar_risk=5.0),
    )

    result = run_scan(["AAPL"], settings, include_details=False)

    row = result["candidates"][0]
    assert row["status"] == "APPROVED"
    assert row["swarm_decision"] == "APPROVE"
    assert row["swarm_confidence"] == 0.75
    assert row["swarm_handoff"] == "risk_manager handoff: technical=BUY fundamental=BUY"
    event = json.loads((log_dir / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["swarm_decision"] == "APPROVE"


def test_scan_row_supermodel_handles_malformed_details() -> None:
    assert _scan_row_supermodel(SimpleNamespace(details="not-json")) == (None, None)


def test_trade_stack_decision_parses_strategy_tag() -> None:
    trade = SimpleNamespace(strategy_tag="v3-trend|stack:support")

    assert _trade_stack_decision(trade) == "support"


def test_trade_consensus_parses_strategy_tag() -> None:
    trade = SimpleNamespace(strategy_tag="v3-trend|stack:support|consensus:buy")

    assert _trade_consensus(trade) == "buy"


def test_supermodel_report_reads_persisted_scan_history(tmp_path) -> None:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db_path}\n",
        encoding="utf-8",
    )
    settings = load_settings(config_file)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        upsert_scan_result(
            session,
            ticker="AAPL",
            action="BUY",
            confidence=0.8,
            details={
                "supermodel_decision": "support",
                "supermodel_score": 0.9,
                "swarm_decision": "APPROVE",
                "swarm_handoff": "risk_manager handoff: technical=BUY fundamental=BUY",
                "signal_mode": "parallel",
                "consensus": "BUY",
            },
        )
        upsert_scan_result(
            session,
            ticker="MSFT",
            action="HOLD",
            confidence=0.0,
            reasons=["stale market data"],
            details={
                "supermodel_decision": "block",
                "supermodel_score": 0.2,
                "swarm_decision": "REJECT",
                "signal_mode": "parallel",
                "consensus": "SELL",
            },
        )
        winning = upsert_trade(
            session,
            ticker="AAPL",
            side="BUY",
            order_type="market",
            quantity=1,
            entry_price=100.0,
            strategy_tag="test|stack:support|swarm:approve|consensus:buy",
        )
        update_trade_exit(session, winning.id, exit_price=110.0, pnl=10.0)
        losing = upsert_trade(
            session,
            ticker="MSFT",
            side="BUY",
            order_type="market",
            quantity=1,
            entry_price=100.0,
            strategy_tag="test|stack:block|swarm:reject|consensus:sell",
        )
        update_trade_exit(session, losing.id, exit_price=95.0, pnl=-5.0)
        upsert_trade(
            session,
            ticker="NVDA",
            side="BUY",
            order_type="market",
            quantity=1,
            entry_price=100.0,
            strategy_tag="test|stack:support|swarm:approve|consensus:buy",
        )
    finally:
        session.close()
        engine.dispose()

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "supermodel-report"])

    assert result.exit_code == 0
    assert "scan_rows=2 stack_rows=2 missing_stack=0" in result.stdout
    assert "support count=1 buy=1 hold=0 avg_score=0.90" in result.stdout
    assert "block count=1 buy=0 hold=1 avg_score=0.20" in result.stdout
    assert "SCAN HOLD REASONS" in result.stdout
    assert "block reason=stale market data count=1" in result.stdout
    assert "SWARM ALIGNMENT" in result.stdout
    assert "swarm_handoff_rows=1" in result.stdout
    assert "swarm=approve stack=support count=1" in result.stdout
    assert "swarm=reject stack=block count=1" in result.stdout
    assert "PARALLEL CONSENSUS" in result.stdout
    assert "consensus=BUY stack=support count=1" in result.stdout
    assert "consensus=SELL stack=block count=1" in result.stdout
    assert "closed_stack_trades=2 open_stack_trades=1" in result.stdout
    assert "support closed=1 net_pnl=10.00 avg_pnl=10.00 win_rate=1.00 wins=1 losses=0 open=1" in result.stdout
    assert "block closed=1 net_pnl=-5.00 avg_pnl=-5.00 win_rate=0.00 wins=0 losses=1 open=0" in result.stdout
    assert "swarm=approve stack=support closed=1 net_pnl=10.00 win_rate=1.00 wins=1 losses=0 open=1" in result.stdout
    assert "swarm=reject stack=block closed=1 net_pnl=-5.00 win_rate=0.00 wins=0 losses=1 open=0" in result.stdout
    assert "consensus=buy stack=support closed=1 net_pnl=10.00 win_rate=1.00 wins=1 losses=0 open=1" in result.stdout
    assert "consensus=sell stack=block closed=1 net_pnl=-5.00 win_rate=0.00 wins=0 losses=1 open=0" in result.stdout


def test_supermodel_report_warns_when_only_open_stack_trades(tmp_path) -> None:
    from trading_bot.cli.app import app
    from trading_bot.config.loader import load_settings
    from trading_bot.db.repositories import upsert_trade
    from trading_bot.db.session import get_session, init_db, make_session_factory

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state.db'}\n"
        f"  log_dir: {tmp_path / 'logs'}\n",
        encoding="utf-8",
    )
    settings = load_settings(config_file)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        upsert_trade(
            session,
            ticker="NVDA",
            side="BUY",
            order_type="market",
            quantity=1,
            entry_price=100.0,
            strategy_tag="test|stack:support|swarm:approve|consensus:buy",
        )
    finally:
        session.close()
        engine.dispose()

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "supermodel-report"])

    assert result.exit_code == 0
    assert "closed_stack_trades=0 open_stack_trades=1" in result.stdout
    assert "outcome_confidence=INSUFFICIENT reason=no_closed_stack_trades" in result.stdout


def test_trade_strategy_tag_includes_supermodel_decision() -> None:
    signal = SimpleNamespace(strategy_tag="v3-trend_following")

    assert _trade_strategy_tag(signal, {"supermodel_decision": "support"}) == (
        "v3-trend_following|stack:support"
    )


def test_trade_strategy_tag_includes_swarm_decision() -> None:
    signal = SimpleNamespace(strategy_tag="test")

    tag = _trade_strategy_tag(
        signal,
        {"supermodel_decision": "support", "swarm_decision": "APPROVE", "consensus": "BUY"},
    )

    assert tag == "test|stack:support|swarm:approve|consensus:buy"
    trade = SimpleNamespace(strategy_tag=tag)
    assert _trade_stack_decision(trade) == "support"
    assert _trade_swarm_decision(trade) == "approve"
    assert _trade_consensus(trade) == "buy"


def test_trade_swarm_decision_normalizes_legacy_hold_labels() -> None:
    assert _trade_swarm_decision(SimpleNamespace(strategy_tag="x|swarm:hold_for_more_in")) == "hold"
    assert _trade_swarm_decision(SimpleNamespace(strategy_tag="x|swarm:hold_for_more_info")) == "hold"


def test_trade_strategy_tag_stays_column_safe() -> None:
    signal = SimpleNamespace(strategy_tag="very_long_strategy_name_that_should_be_truncated")

    tag = _trade_strategy_tag(
        signal,
        {
            "supermodel_decision": "support",
            "swarm_decision": "UNKNOWN|stack:block DECISION NAME THAT IS TOO LONG",
        },
    )

    assert tag is not None
    assert len(tag) <= 200
    assert "|swarm:" in tag
    assert tag.endswith("|stack:support|swarm:unknown_stack_block")
    assert _trade_swarm_decision(SimpleNamespace(strategy_tag=tag)) == "unknown_stack_block"


def test_trade_strategy_tag_preserves_consensus_when_suffix_is_long() -> None:
    tag = _trade_strategy_tag(
        SimpleNamespace(strategy_tag="very_long_strategy_name_that_should_be_truncated"),
        {
            "supermodel_decision": "STACK LABEL THAT IS TOO LONG",
            "swarm_decision": "UNKNOWN|stack:block DECISION NAME THAT IS TOO LONG",
            "consensus": "BUY",
        },
    )

    trade = SimpleNamespace(strategy_tag=tag)
    assert tag is not None
    assert len(tag) <= 200
    assert _trade_stack_decision(trade) == "stack_label_that"
    assert _trade_consensus(trade) == "buy"


def test_trade_strategy_tag_handles_empty_sanitized_tokens() -> None:
    signal = SimpleNamespace(strategy_tag="test")

    tag = _trade_strategy_tag(
        signal,
        {"supermodel_decision": "|||", "swarm_decision": "   "},
    )

    assert tag == "test|stack:unknown|swarm:unknown"


def test_trade_strategy_tag_clamps_worst_case_suffix() -> None:
    tag = _trade_strategy_tag(
        SimpleNamespace(strategy_tag="long_base"),
        {
            "supermodel_decision": "STACK LABEL THAT IS TOO LONG",
            "swarm_decision": "SWARM LABEL THAT IS TOO LONG",
        },
    )

    assert tag is not None
    assert len(tag) <= 200
    assert "|swarm:" in tag


def test_persisted_trade_carries_supermodel_stack_tag(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    settings = Settings(app={"state_db_path": str(db_path)})
    signal = SimpleNamespace(
        strategy_tag="test",
        stop_loss=95.0,
        profit_target=110.0,
    )
    fill = SimpleNamespace(
        ticker="AAPL",
        quantity=1,
        fill_price=100.0,
        fees=0.0,
    )

    _persist_trade_to_db(fill, signal, settings, {"supermodel_decision": "support"})

    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        trade = get_trades(session, ticker="AAPL", limit=1)[0]
        assert trade.strategy_tag == "test|stack:support"
    finally:
        session.close()
        engine.dispose()


def test_paper_trade_stack_uses_swarm_decision(monkeypatch, tmp_path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    PortfolioLedger(db_path).save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))
    settings = Settings(
        app={"state_db_path": str(db_path), "log_dir": str(log_dir)},
        swarm={"enabled": True, "preset": "investment_committee"},
    )
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(timezone.utc),
    )
    fill = FillResult(
        order_id="order-1",
        ticker="AAPL",
        quantity=1,
        fill_price=100.0,
        fees=0.0,
        filled_at=datetime.now(timezone.utc),
    )

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(orchestrator, "_daily_loss_limit_hit", lambda state, settings: False)
    monkeypatch.setattr(orchestrator, "_daily_order_limit_hit", lambda ledger, settings: False)
    monkeypatch.setattr(orchestrator, "_fetch_atr", lambda symbol, settings: None)
    monkeypatch.setattr(orchestrator, "_scan_quality", lambda details: "GREEN")
    monkeypatch.setattr(orchestrator, "_build_signal_result", lambda symbol, settings: (signal, "test", {}))
    import trading_bot.strategy.setup_rules as setup_rules
    monkeypatch.setattr(setup_rules, "compute_v25_confluence_score", lambda details: 8.0)
    monkeypatch.setattr(
        orchestrator,
        "_run_swarm_overlay",
        lambda symbols, settings, portfolio_state=None: {
            "AAPL": MagicMock(
                decision="APPROVE",
                confidence=0.8,
                key_rationale="bullish",
                risk_factors=[
                    "risk_manager handoff: technical=BUY fundamental=BUY",
                ],
            )
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "evaluate_signal",
        lambda **kwargs: RiskDecision(approved=True, reason="approved", position_size=1, dollar_risk=5.0),
    )
    monkeypatch.setattr(orchestrator, "submit_signal_as_order", lambda **kwargs: fill)

    result = run_paper_trade(["AAPL"], settings)

    assert result == ["AAPL FILLED qty=1 price=100.00 cash=10000.00"]
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        trade = get_trades(session, ticker="AAPL", limit=1)[0]
        assert trade.strategy_tag == "test|stack:support|swarm:approve"
    finally:
        session.close()
        engine.dispose()

    events = [
        json.loads(line)
        for line in (log_dir / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    filled = [event for event in events if event.get("status") == "FILLED"][0]
    assert filled["supermodel_decision"] == "support"
    assert filled["swarm_decision"] == "APPROVE"
    assert filled["swarm_handoff"] == "risk_manager handoff: technical=BUY fundamental=BUY"


def test_paper_trade_swarm_sentiment_trims_position_size(monkeypatch, tmp_path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    PortfolioLedger(db_path).save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))
    settings = Settings(
        app={"state_db_path": str(db_path), "log_dir": str(log_dir)},
        swarm={"enabled": True, "preset": "investment_committee", "swarm_weight": 0.0},
    )
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(timezone.utc),
    )
    fill = FillResult(
        order_id="order-1",
        ticker="AAPL",
        quantity=17,
        fill_price=100.0,
        fees=0.0,
        filled_at=datetime.now(timezone.utc),
    )
    position_sizes: list[int] = []

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(orchestrator, "_daily_loss_limit_hit", lambda state, settings: False)
    monkeypatch.setattr(orchestrator, "_daily_order_limit_hit", lambda ledger, settings: False)
    monkeypatch.setattr(orchestrator, "_fetch_atr", lambda symbol, settings: None)
    monkeypatch.setattr(orchestrator, "_scan_quality", lambda details: "GREEN")
    monkeypatch.setattr(orchestrator, "_build_signal_result", lambda symbol, settings: (signal, "test", {}))
    import trading_bot.strategy.setup_rules as setup_rules
    monkeypatch.setattr(setup_rules, "compute_v25_confluence_score", lambda details: 8.0)
    monkeypatch.setattr(
        orchestrator,
        "_run_swarm_overlay",
        lambda symbols, settings, portfolio_state=None: {
            "AAPL": CommitteeDecision(
                decision="APPROVE",
                ticker="AAPL",
                action="BUY",
                confidence=0.8,
                key_rationale="mixed",
                supporting_signals=[
                    SignalVote(
                        ticker="AAPL",
                        action="SELL",
                        confidence=0.7,
                        worker_name="sentiment_analyst",
                        preset="investment_committee",
                        reasons=["news:downgrade"],
                        metadata={"sentiment_score": -1.0, "news_count": 1, "source": "rss"},
                    )
                ],
            )
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "evaluate_signal",
        lambda **kwargs: RiskDecision(approved=True, reason="approved", position_size=20, dollar_risk=100.0),
    )
    monkeypatch.setattr(
        orchestrator,
        "submit_signal_as_order",
        lambda **kwargs: position_sizes.append(kwargs["position_size_override"]) or fill,
    )

    result = run_paper_trade(["AAPL"], settings)

    assert result == [
        "AAPL FILLED qty=17 price=100.00 cash=10000.00 sent_mult=0.85 sent_action=SELL sent_score=-1.00"
    ]
    assert position_sizes == [17]
    events = [
        json.loads(line)
        for line in (log_dir / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    filled = [event for event in events if event.get("status") == "FILLED"][0]
    assert filled["swarm_sentiment_size_multiplier"] == 0.85
    assert "sent_mult=0.85" in result[0]


def test_paper_trade_swarm_sentiment_can_recover_from_half_size(monkeypatch, tmp_path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    PortfolioLedger(db_path).save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))
    settings = Settings(
        app={"state_db_path": str(db_path), "log_dir": str(log_dir)},
        swarm={"enabled": True, "preset": "investment_committee", "swarm_weight": 0.0},
    )
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(timezone.utc),
    )
    fill = FillResult(
        order_id="order-1",
        ticker="AAPL",
        quantity=11,
        fill_price=100.0,
        fees=0.0,
        filled_at=datetime.now(timezone.utc),
    )
    position_sizes: list[int] = []

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(orchestrator, "_daily_loss_limit_hit", lambda state, settings: False)
    monkeypatch.setattr(orchestrator, "_daily_order_limit_hit", lambda ledger, settings: False)
    monkeypatch.setattr(orchestrator, "_fetch_atr", lambda symbol, settings: None)
    monkeypatch.setattr(orchestrator, "_scan_quality", lambda details: "GREEN")
    monkeypatch.setattr(
        orchestrator,
        "_build_signal_result",
        lambda symbol, settings: (signal, "test", {"is_half_size": True}),
    )
    import trading_bot.strategy.setup_rules as setup_rules
    monkeypatch.setattr(setup_rules, "compute_v25_confluence_score", lambda details: 8.0)
    monkeypatch.setattr(
        orchestrator,
        "_run_swarm_overlay",
        lambda symbols, settings, portfolio_state=None: {
            "AAPL": CommitteeDecision(
                decision="APPROVE",
                ticker="AAPL",
                action="BUY",
                confidence=0.8,
                key_rationale="bullish",
                supporting_signals=[
                    SignalVote(
                        ticker="AAPL",
                        action="BUY",
                        confidence=0.7,
                        worker_name="sentiment_analyst",
                        preset="investment_committee",
                        reasons=["news:upgrade"],
                        metadata={"sentiment_score": 1.0, "news_count": 1, "source": "rss"},
                    )
                ],
            )
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "evaluate_signal",
        lambda **kwargs: RiskDecision(approved=True, reason="approved", position_size=20, dollar_risk=100.0),
    )
    monkeypatch.setattr(
        orchestrator,
        "submit_signal_as_order",
        lambda **kwargs: position_sizes.append(kwargs["position_size_override"]) or fill,
    )

    result = run_paper_trade(["AAPL"], settings)

    assert result == [
        "AAPL FILLED qty=11 price=100.00 cash=10000.00 sent_mult=1.15 sent_action=BUY sent_score=1.00"
    ]
    assert position_sizes == [11]
    events = [
        json.loads(line)
        for line in (log_dir / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    filled = [event for event in events if event.get("status") == "FILLED"][0]
    assert filled["swarm_sentiment_size_multiplier"] == 1.15
    assert "sent_mult=1.15" in result[0]


def test_paper_trade_reject_keeps_stack_evidence(monkeypatch, tmp_path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    PortfolioLedger(db_path).save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))
    settings = Settings(
        app={"state_db_path": str(db_path), "log_dir": str(log_dir)},
        swarm={"enabled": True},
    )
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="test",
        timestamp=datetime.now(timezone.utc),
    )

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(orchestrator, "_daily_loss_limit_hit", lambda state, settings: False)
    monkeypatch.setattr(orchestrator, "_daily_order_limit_hit", lambda ledger, settings: False)
    monkeypatch.setattr(orchestrator, "_build_signal_result", lambda symbol, settings: (signal, "test", {}))
    monkeypatch.setattr(orchestrator, "_scan_quality", lambda details: "YELLOW")
    monkeypatch.setattr(
        orchestrator,
        "_run_swarm_overlay",
        lambda symbols, settings, portfolio_state=None: {
            "AAPL": MagicMock(decision="APPROVE", confidence=0.8, key_rationale="bullish", risk_factors=[])
        },
    )

    result = run_paper_trade(["AAPL"], settings)

    assert result == ["AAPL REJECTED yellow signal"]
    event = json.loads((log_dir / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["reason"] == "yellow signal"
    assert event["supermodel_decision"] == "support"
    assert event["swarm_decision"] == "APPROVE"


def test_paper_trade_stale_reject_keeps_stack_evidence(monkeypatch, tmp_path) -> None:
    """Stale check removed — test that swarm/supermodel evidence is still recorded on normal flow."""
    import trading_bot.runtime.orchestrator as orchestrator

    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    PortfolioLedger(db_path).save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))
    settings = Settings(
        app={"state_db_path": str(db_path), "log_dir": str(log_dir)},
        swarm={"enabled": True},
    )
    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["test"],
        strategy_tag="v3-trend_following",
        timestamp=datetime.now(timezone.utc),
    )

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(orchestrator, "_daily_loss_limit_hit", lambda state, settings: False)
    monkeypatch.setattr(orchestrator, "_daily_order_limit_hit", lambda ledger, settings: False)
    monkeypatch.setattr(orchestrator, "_build_signal_result", lambda symbol, settings: (signal, "test", {"quality": "GREEN"}))
    monkeypatch.setattr(
        orchestrator,
        "_run_swarm_overlay",
        lambda symbols, settings, portfolio_state=None: {
            "AAPL": MagicMock(
                decision="APPROVE",
                confidence=0.8,
                key_rationale="bullish",
                risk_factors=["risk_manager handoff: technical=BUY fundamental=BUY"],
            )
        },
    )

    result = run_paper_trade(["AAPL"], settings)

    # Signal goes through (GREEN quality)
    assert any("FILLED" in r or "APPROVED" in r for r in result)
    event = json.loads((log_dir / "decision-log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["supermodel_decision"] == "support"
    assert event["swarm_decision"] == "APPROVE"
    assert event["swarm_handoff"] == "risk_manager handoff: technical=BUY fundamental=BUY"
