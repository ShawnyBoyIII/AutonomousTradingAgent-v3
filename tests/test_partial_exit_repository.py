"""TDD: partial exit P&L is accumulated on the open trade row and added on final exit."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_bot.config.settings import AppSettings, Settings
from trading_bot.db.models import Trade
from trading_bot.db.repositories.trades import (
    accumulate_partial_exit,
    update_trade_exit,
    upsert_trade,
)
from trading_bot.db.session import get_session, init_db, make_session_factory


def _settings(tmp_path) -> Settings:
    return Settings(
        app=AppSettings(state_db_path=str(tmp_path / "ignored.db"), log_dir=str(tmp_path / "logs"))
    )


def _seed_trade(session) -> Trade:
    return upsert_trade(
        session,
        ticker="AAPL",
        side="BUY",
        order_type="market",
        quantity=10,
        entry_price=100.0,
        strategy_tag="v3-trend",
    )


def test_accumulate_partial_exit_adds_pnl(tmp_path) -> None:
    settings = _settings(tmp_path)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        trade = _seed_trade(session)
        trade_id = trade.id
    finally:
        session.close()
        engine.dispose()

    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        accumulate_partial_exit(session, trade_id=trade_id, partial_pnl=50.0)
        accumulate_partial_exit(session, trade_id=trade_id, partial_pnl=25.5)
        updated = session.get(Trade, trade_id)
        assert updated.partial_pnl_accumulated == 75.5
        assert updated.partial_exit_count == 2
        assert updated.status == "FILLED"
    finally:
        session.close()
        engine.dispose()


def test_update_trade_exit_includes_accumulated_partial_pnl(tmp_path) -> None:
    settings = _settings(tmp_path)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        trade = _seed_trade(session)
        trade_id = trade.id
    finally:
        session.close()
        engine.dispose()

    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        accumulate_partial_exit(session, trade_id=trade_id, partial_pnl=60.0)
        update_trade_exit(
            session=session,
            trade_id=trade_id,
            exit_price=110.0,
            exit_fees=1.0,
            pnl=99.0,
        )
        updated = session.get(Trade, trade_id)
        assert updated.status == "CLOSED"
        assert updated.pnl == pytest.approx(159.0)
    finally:
        session.close()
        engine.dispose()


def test_update_trade_exit_handles_no_accumulated_partials(tmp_path) -> None:
    settings = _settings(tmp_path)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        trade = _seed_trade(session)
        trade_id = trade.id
    finally:
        session.close()
        engine.dispose()

    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        update_trade_exit(
            session=session,
            trade_id=trade_id,
            exit_price=110.0,
            exit_fees=1.0,
            pnl=99.0,
        )
        updated = session.get(Trade, trade_id)
        assert updated.status == "CLOSED"
        assert updated.pnl == pytest.approx(99.0)
    finally:
        session.close()
        engine.dispose()


def test_accumulate_partial_exit_raises_for_missing_trade(tmp_path) -> None:
    settings = _settings(tmp_path)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        with pytest.raises(ValueError):
            accumulate_partial_exit(session, trade_id=9999, partial_pnl=10.0)
    finally:
        session.close()
        engine.dispose()
