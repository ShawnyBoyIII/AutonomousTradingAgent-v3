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
    }

    assert "supermodel=support:0.8" in _format_scan_details(details)
    assert "supermodel_support=1" in _format_scan_summary(summary)




def test_compact_scan_persistence_keeps_swarm_evidence() -> None:
    details = _scan_row_details_for_persistence(
        {
            "supermodel_decision": "support",
            "supermodel_score": 0.8,
        }
    )

    assert details == {
        "supermodel_decision": "support",
        "supermodel_score": 0.8,
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
            strategy_tag="test|stack:support|consensus:buy",
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
            strategy_tag="test|stack:support|consensus:buy",
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
    assert "PARALLEL CONSENSUS" in result.stdout
    assert "consensus=BUY stack=support count=1" in result.stdout
    assert "consensus=SELL stack=block count=1" in result.stdout
    assert "closed_stack_trades=2 open_stack_trades=1" in result.stdout
    assert "support closed=1 net_pnl=10.00 avg_pnl=10.00 win_rate=1.00 wins=1 losses=0 open=1" in result.stdout
    assert "block closed=1 net_pnl=-5.00 avg_pnl=-5.00 win_rate=0.00 wins=0 losses=1 open=0" in result.stdout
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
            strategy_tag="test|stack:support|consensus:buy",
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


def test_trade_strategy_tag_stays_column_safe() -> None:
    signal = SimpleNamespace(strategy_tag="very_long_strategy_name_that_should_be_truncated")

    tag = _trade_strategy_tag(
        signal,
        {
            "supermodel_decision": "support",
        },
    )

    assert tag is not None
    assert len(tag) <= 200
    assert tag.endswith("|stack:support")


def test_trade_strategy_tag_preserves_consensus_when_suffix_is_long() -> None:
    tag = _trade_strategy_tag(
        SimpleNamespace(strategy_tag="very_long_strategy_name_that_should_be_truncated"),
        {
            "supermodel_decision": "STACK LABEL THAT IS TOO LONG",
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
        {"supermodel_decision": "|||"},
    )

    assert tag == "test|stack:unknown"


def test_trade_strategy_tag_clamps_worst_case_suffix() -> None:
    tag = _trade_strategy_tag(
        SimpleNamespace(strategy_tag="long_base"),
        {
            "supermodel_decision": "STACK LABEL THAT IS TOO LONG",
        },
    )

    assert tag is not None
    assert len(tag) <= 200
    assert "stack:stack_label_that" in tag


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
