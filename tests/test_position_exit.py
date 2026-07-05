from __future__ import annotations

from datetime import datetime, timezone
import importlib
from pathlib import Path

from trading_bot.config.settings import AppSettings, Settings
from trading_bot.db.repositories.trades import upsert_trade
from trading_bot.db.repositories.positions import upsert_position
from trading_bot.db.session import get_session, init_db, make_session_factory
from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.runtime.position_exit import (
    fill_partial_take_profit_position,
    fill_sell_position,
)


class _BrokerStub:
    def __init__(self, *, fill_price: float, fill_quantity: int, filled_at: datetime, cash: float, positions: dict[str, int]):
        self._fill_price = fill_price
        self._fill_quantity = fill_quantity
        self._filled_at = filled_at
        self.cash = cash
        self.positions = positions

    def submit_order(self, order, market_price: float) -> FillResult:
        return FillResult(
            order_id=f"sell-{order.ticker.lower()}",
            ticker=order.ticker,
            quantity=self._fill_quantity,
            fill_price=self._fill_price,
            fees=1.0,
            filled_at=self._filled_at,
        )


class _LedgerSpy:
    def __init__(self) -> None:
        self.saved_states: list[PortfolioState] = []
        self.snapshot_timestamps: list[datetime] = []
        self.recorded_fills: list[tuple[str, float]] = []

    def record_fill(self, fill: FillResult, side: str, realized_pnl: float = 0.0, strategy_tag: str = "") -> None:
        self.recorded_fills.append((side, realized_pnl))

    def save_portfolio_state(self, state: PortfolioState) -> None:
        self.saved_states.append(state)

    def record_equity_snapshot(self, state: PortfolioState, timestamp: datetime) -> None:
        self.snapshot_timestamps.append(timestamp)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app=AppSettings(
            log_dir=str(tmp_path / "logs"),
            state_db_path=str(tmp_path / "state" / "ignored-by-init-db.db"),
        )
    )


def _position(quantity: int = 10) -> Position:
    return Position(
        ticker="AAPL",
        quantity=quantity,
        average_cost=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        entry_at=datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc),
        strategy_tag="v3-trend_following",
    )


def test_fill_sell_position_uses_supplied_settings_and_persists_exit(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        trade = upsert_trade(
            session,
            ticker="AAPL",
            side="BUY",
            order_type="market",
            quantity=10,
            entry_price=100.0,
            strategy_tag="v3-trend_following",
        )
        upsert_position(
            session,
            ticker="AAPL",
            quantity=10,
            average_cost=100.0,
            stop_loss=95.0,
            profit_target=110.0,
            strategy_tag="v3-trend_following",
        )
        trade_id = trade.id
    finally:
        session.close()
        engine.dispose()

    cli_app = importlib.import_module("trading_bot.cli.app")

    def _unexpected_load_settings():
        raise AssertionError("fill_sell_position should use the supplied settings")

    monkeypatch.setattr(cli_app, "load_settings", _unexpected_load_settings)

    ledger = _LedgerSpy()
    broker = _BrokerStub(
        fill_price=110.0,
        fill_quantity=10,
        filled_at=datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc),
        cash=1099.0,
        positions={},
    )
    state = PortfolioState(cash=0.0, equity=1000.0, positions={"AAPL": _position()})

    fill_sell_position(
        ticker="AAPL",
        position=_position(),
        reason="profit_target",
        submitted_at=datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc),
        last_price=110.0,
        broker=broker,
        ledger=ledger,
        state=state,
        log_path=tmp_path / "logs" / "decision-log.jsonl",
        settings=settings,
    )

    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        persisted = session.get(type(trade), trade_id)
        assert persisted is not None
        assert persisted.status == "CLOSED"
        assert persisted.exit_price == 110.0
        assert persisted.pnl == 99.0
    finally:
        session.close()
        engine.dispose()


def test_partial_take_profit_keeps_trade_open_and_records_single_snapshot(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        trade = upsert_trade(
            session,
            ticker="AAPL",
            side="BUY",
            order_type="market",
            quantity=10,
            entry_price=100.0,
            strategy_tag="v3-trend_following",
        )
    finally:
        session.close()
        engine.dispose()

    ledger = _LedgerSpy()
    broker = _BrokerStub(
        fill_price=110.0,
        fill_quantity=5,
        filled_at=datetime(2026, 7, 4, 14, 5, tzinfo=timezone.utc),
        cash=549.0,
        positions={"AAPL": 5},
    )
    state = PortfolioState(cash=0.0, equity=1000.0, positions={"AAPL": _position()})

    new_state, _, _ = fill_partial_take_profit_position(
        ticker="AAPL",
        position=_position(),
        submitted_at=datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc),
        last_price=110.0,
        broker=broker,
        ledger=ledger,
        state=state,
        log_path=tmp_path / "logs" / "decision-log.jsonl",
        settings=settings,
    )

    engine = init_db(settings)
    session = get_session(make_session_factory(engine))
    try:
        persisted = session.get(type(trade), trade.id)
        assert persisted is not None
        assert persisted.status == "FILLED"
        assert persisted.exit_price is None
    finally:
        session.close()
        engine.dispose()

    assert new_state.positions["AAPL"].quantity == 5
    assert new_state.positions["AAPL"].partial_profit_taken is True
    assert ledger.snapshot_timestamps == [datetime(2026, 7, 4, 14, 5, tzinfo=timezone.utc)]
